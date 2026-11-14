"""
Categorisation service.

Downloads an AIP from S3, splits a long file into pieces, and asks the
model to answer every template question. The category is chosen here
from those answers: the first category whose questions are all yes.
"""

import asyncio
import base64
import io
import logging

import boto3
import httpx
from pypdf import PdfReader, PdfWriter

from config.settings import settings
from prompts import (
    CHECKLIST_TOOL,
    CHECKLIST_TOOL_CHOICE,
    CHECKLIST_TOOL_NAME,
    build_checklist_system,
    build_checklist_user,
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# Opus 4.6 list price, USD per million tokens. Batch is half.
# Cache rates apply only when a request uses prompt caching.
_PRICE_INPUT = 5.0
_PRICE_OUTPUT = 25.0
_PRICE_CACHE_WRITE = 6.25
_PRICE_CACHE_READ = 0.5

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "pdfs-2024-09-25",
    "content-type": "application/json",
}

# Anthropic allows up to 100 pages per document, but dense AIP pages often
# blow the 200k input token context window well before that. Split earlier
# so each chunk stays under the context limit with headroom for the prompt.
MAX_PAGES_WITHOUT_SPLIT = 60
CHUNK_PAGE_SIZE = 60

# One checklist can be many questions, so the reply needs more room than a paragraph.
ANSWER_MAX_TOKENS = 8000
CATEGORISATION_TEMPERATURE = 0
_CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
# Shared across jobs in this process so several airports cannot open
# an unlimited number of model calls at once.
MAX_INFLIGHT_MODEL_CALLS = 4
_model_slots: asyncio.Semaphore | None = None


def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def _anthropic_headers() -> dict:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    return {"x-api-key": settings.ANTHROPIC_API_KEY, **ANTHROPIC_HEADERS}


def download_pdf_bytes(s3_key: str) -> bytes:
    """Download PDF bytes from S3 into memory. Nothing is written to disk."""
    s3 = _s3_client()
    obj = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=s3_key)
    data = obj["Body"].read()
    logger.info(
        "Downloaded s3://%s/%s (%d bytes)",
        settings.S3_BUCKET_NAME,
        s3_key,
        len(data),
    )
    return data


def split_pdf_into_chunks(pdf_bytes: bytes) -> list[dict]:
    """
    Split a PDF into chunks of CHUNK_PAGE_SIZE pages when over the limit.

    Returns a list of dicts:
      chunk_index, page_start (1-based), page_end (1-based inclusive), pdf_bytes
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise RuntimeError("PDF has zero pages")

    if total_pages <= MAX_PAGES_WITHOUT_SPLIT:
        return [{
            "chunk_index": 0,
            "page_start": 1,
            "page_end": total_pages,
            "pdf_bytes": pdf_bytes,
        }]

    chunks: list[dict] = []
    for start in range(0, total_pages, CHUNK_PAGE_SIZE):
        end = min(start + CHUNK_PAGE_SIZE, total_pages)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunks.append({
            "chunk_index": len(chunks),
            "page_start": start + 1,
            "page_end": end,
            "pdf_bytes": buf.getvalue(),
        })

    logger.info(
        "Split PDF (%d pages) into %d chunks of up to %d pages",
        total_pages,
        len(chunks),
        CHUNK_PAGE_SIZE,
    )
    return chunks


def _document_block_from_bytes(pdf_bytes: bytes) -> dict:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
    }

def _money(amount: float) -> str:
    return f"${amount:.4f}"


def _cost_usd(usage: dict, *, batch: bool) -> float:
    scale = 0.5 if batch else 1.0
    return scale * (
        usage["input_tokens"] * _PRICE_INPUT
        + usage["cache_creation_input_tokens"] * _PRICE_CACHE_WRITE
        + usage["cache_read_input_tokens"] * _PRICE_CACHE_READ
        + usage["output_tokens"] * _PRICE_OUTPUT
    ) / 1_000_000


class _CallUsage:
    """Token counts from the model calls in one job. Logged, not stored."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.pages: int | None = None

    def add(self, label: str, data: dict) -> None:
        raw = data.get("usage")
        if not isinstance(raw, dict):
            logger.warning("Model call %s returned no usage block", label)
            raw = {}
        row = {
            "label": label,
            "input_tokens": int(raw.get("input_tokens") or 0),
            "output_tokens": int(raw.get("output_tokens") or 0),
            "cache_read_input_tokens": int(raw.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        }
        self.calls.append(row)
        logger.info(
            "Model call %s input_tokens=%d output_tokens=%d cache_read=%d cache_write=%d live=%s batch=%s",
            label,
            row["input_tokens"],
            row["output_tokens"],
            row["cache_read_input_tokens"],
            row["cache_creation_input_tokens"],
            _money(_cost_usd(row, batch=False)),
            _money(_cost_usd(row, batch=True)),
        )

    def log_total(self, job_id: str) -> None:
        if not self.calls:
            return
        total = {
            "input_tokens": sum(row["input_tokens"] for row in self.calls),
            "output_tokens": sum(row["output_tokens"] for row in self.calls),
            "cache_read_input_tokens": sum(row["cache_read_input_tokens"] for row in self.calls),
            "cache_creation_input_tokens": sum(row["cache_creation_input_tokens"] for row in self.calls),
        }
        pages = f", {self.pages} pages" if self.pages else ""
        if settings.ANTHROPIC_MODEL == "claude-opus-4-6":
            rate_note = "claude-opus-4-6 list price"
        else:
            rate_note = (
                f"tokens from {settings.ANTHROPIC_MODEL}; "
                "dollars use claude-opus-4-6 list price"
            )
        logger.info(
            "Job %s cost%s: %d call(s), input_tokens=%d output_tokens=%d live=%s batch=%s (%s)",
            job_id,
            pages,
            len(self.calls),
            total["input_tokens"],
            total["output_tokens"],
            _money(_cost_usd(total, batch=False)),
            _money(_cost_usd(total, batch=True)),
            rate_note,
        )


def _model_call_slots() -> asyncio.Semaphore:
    global _model_slots
    if _model_slots is None:
        _model_slots = asyncio.Semaphore(MAX_INFLIGHT_MODEL_CALLS)
    return _model_slots


def _pdf_headers() -> dict:
    headers = _anthropic_headers()
    headers["anthropic-beta"] = "pdfs-2024-09-25"
    return headers


def _message_headers() -> dict:
    headers = _anthropic_headers()
    headers.pop("anthropic-beta", None)
    return headers


async def _post_messages(
    payload: dict,
    *,
    pdf: bool,
    timeout: float,
    label: str,
    usage: _CallUsage,
) -> dict:
    """One Messages API call. Rate limits are retried. The slot is released while waiting."""
    headers = _pdf_headers() if pdf else _message_headers()
    delay = 2.0
    last_error = "The model did not respond"
    for attempt in range(3):
        async with _model_call_slots():
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{ANTHROPIC_BASE_URL}/messages",
                    headers=headers,
                    json=payload,
                )
        if response.status_code in (429, 503, 529):
            last_error = f"The model is busy ({response.status_code})"
            logger.warning("Model call returned %s, retrying", response.status_code)
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if not response.is_success:
            logger.error("Model call failed %s: %s", response.status_code, response.text[:500])
            raise RuntimeError(f"The model call failed ({response.status_code})")
        data = response.json()
        usage.add(label, data)
        return data
    raise RuntimeError(last_error)


def _questions_from_template(template: dict) -> list[dict]:
    rows = []
    for cat in template.get("categories") or []:
        code = (cat.get("code") or "").strip()
        cat_id = cat.get("id") or ""
        for question in cat.get("questions") or []:
            text = (question.get("text") or "").strip()
            qid = question.get("id") or ""
            if code and text and qid:
                rows.append({
                    "id": qid,
                    "category_id": cat_id,
                    "category": code,
                    "text": text,
                })
    return rows


def _coerce_answer(value) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    text = str(value or "").strip().lower()
    if text in ("yes", "true"):
        return "yes"
    if text in ("no", "false"):
        return "no"
    return "unknown"


def _clean_model_answer(raw: dict, known_ids: set[str]) -> dict | None:
    qid = str(raw.get("question_id") or "").strip()
    if qid not in known_ids:
        return None
    answer = _coerce_answer(raw.get("answer"))
    confidence = str(raw.get("confidence_level") or "").strip().upper()
    if confidence not in _CONF_RANK:
        confidence = "LOW"
    needs_manual = bool(raw.get("needs_manual")) or answer == "unknown"
    evidence = " ".join(str(raw.get("evidence") or "").split())
    if len(evidence) > 400:
        evidence = evidence[:400].rstrip()
    page = raw.get("page")
    if isinstance(page, str) and page.strip().isdigit():
        page = int(page.strip())
    if isinstance(page, float) and page.is_integer():
        page = int(page)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        page = None
    return {
        "question_id": qid,
        "answer": answer,
        "evidence": evidence,
        "confidence_level": confidence,
        "needs_manual": needs_manual,
        "page": page,
    }


def _answers_from_content(content_blocks: list, known_ids: set[str]) -> dict:
    blocks = content_blocks or []
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == CHECKLIST_TOOL_NAME:
            raw_answers = (block.get("input") or {}).get("answers")
            if not isinstance(raw_answers, list):
                return {"answers": [], "error": "The model did not return a list of answers"}
            cleaned = []
            for item in raw_answers:
                if isinstance(item, dict):
                    parsed = _clean_model_answer(item, known_ids)
                    if parsed:
                        cleaned.append(parsed)
            return {"answers": cleaned, "error": None}
    return {"answers": [], "error": "The model did not return answers"}


def _merge_answer(current: dict | None, incoming: dict) -> dict:
    if current is None:
        return incoming
    if current["answer"] == "unknown" and incoming["answer"] != "unknown":
        return incoming
    if incoming["answer"] == "unknown":
        return current
    if current["answer"] != incoming["answer"]:
        return {
            **current,
            "answer": "unknown",
            "needs_manual": True,
            "confidence_level": "LOW",
            "evidence": "Different parts of the document disagree.",
            "page": None,
        }
    if _CONF_RANK[incoming["confidence_level"]] > _CONF_RANK[current["confidence_level"]]:
        return incoming
    return current


def _blank_answer(row: dict) -> dict:
    return {
        "category_id": row["category_id"],
        "category_code": row["category"],
        "question_id": row["id"],
        "question_text": row["text"],
        "answer": "unknown",
        "evidence": "Not found in the document.",
        "confidence_level": "LOW",
        "needs_manual": True,
        "page": None,
        "user_answer": None,
        "user_evidence": None,
    }


def _record_answer(row: dict, found: dict | None) -> dict:
    record = _blank_answer(row)
    if not found:
        return record
    record.update({
        "answer": found["answer"],
        "evidence": found["evidence"] or record["evidence"],
        "confidence_level": found["confidence_level"],
        "needs_manual": found["needs_manual"],
        "page": found["page"],
    })
    if record["answer"] == "unknown":
        record["needs_manual"] = True
        if not found["evidence"]:
            record["evidence"] = "Not found in the document."
    return record


def _effective_answer(record: dict) -> tuple[str | None, bool]:
    """A person's yes or no replaces the model. Otherwise the model answer stands."""
    user = record.get("user_answer")
    if user in ("yes", "no"):
        return user, False
    answer = record.get("answer")
    if answer not in ("yes", "no", "unknown"):
        answer = None
    return answer, bool(record.get("needs_manual"))


def choose_category_from_answers(answers: list[dict]) -> tuple[str | None, str | None]:
    """First category whose questions are all yes, in the order the answers are stored."""
    groups: list[tuple[str, list[dict]]] = []
    for row in answers:
        code = row.get("category_code") or ""
        if not groups or groups[-1][0] != code:
            groups.append((code, []))
        groups[-1][1].append(row)
    for code, rows in groups:
        if not code or not rows:
            continue
        met = True
        levels = []
        for row in rows:
            answer, needs_manual = _effective_answer(row)
            if answer != "yes" or needs_manual:
                met = False
                break
            level = row.get("confidence_level")
            if level in _CONF_RANK:
                levels.append(level)
        if met:
            confidence = min(levels, key=lambda level: _CONF_RANK[level]) if levels else None
            return code, confidence
    return None, None


def _choose_category(template: dict, by_id: dict[str, dict]) -> tuple[str | None, str | None]:
    ordered = []
    for cat in template.get("categories") or []:
        for question in cat.get("questions") or []:
            record = by_id.get(question.get("id"))
            if record:
                ordered.append(record)
    return choose_category_from_answers(ordered)


def _result_text(category: str | None, confidence: str | None, answers: list[dict]) -> str:
    lines = [f"Category: {category or 'No category met'}"]
    if confidence:
        lines.append(f"Confidence: {confidence}")
    current = None
    for row in answers:
        if row["category_code"] != current:
            current = row["category_code"]
            lines.append("")
            lines.append(current)
        shown, needs_manual = _effective_answer(row)
        bits = [shown or "unknown"]
        if row.get("user_answer"):
            bits.append("entered by a person")
        elif row.get("confidence_level"):
            bits.append(row["confidence_level"].lower())
        if row.get("page"):
            bits.append(f"page {row['page']}")
        if needs_manual:
            bits.append("needs a person")
        evidence = (row.get("user_evidence") or row.get("evidence") or "").strip()
        lines.append(f"- {row['question_text']}")
        lines.append(f"  {', '.join(bits)}. {evidence}".rstrip())
    return "\n".join(lines).strip()


async def _answer_piece(
    template: dict,
    questions: list[dict],
    chunk: dict,
    usage: _CallUsage,
    *,
    require_all: bool,
    organisation_instructions: str = "",
) -> dict:
    known_ids = {row["id"] for row in questions}
    retry_note = ""
    last_error = "The model did not return answers"
    answers: list[dict] = []
    for attempt in range(2):
        system_prompt = build_checklist_system(
            chunk["page_start"],
            chunk["page_end"],
            retry_note=retry_note,
        )
        prompt = build_checklist_user(
            template.get("notes") or "",
            questions,
            organisation_instructions,
        )
        label = f"questions pages {chunk['page_start']}-{chunk['page_end']}"
        if attempt:
            label = f"{label} retry"
        data = await _post_messages(
            {
                "model": settings.ANTHROPIC_MODEL,
                "max_tokens": ANSWER_MAX_TOKENS,
                "temperature": CATEGORISATION_TEMPERATURE,
                "system": system_prompt,
                "tools": [CHECKLIST_TOOL],
                "tool_choice": CHECKLIST_TOOL_CHOICE,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            _document_block_from_bytes(chunk["pdf_bytes"]),
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
            pdf=True,
            timeout=180.0,
            label=label,
            usage=usage,
        )
        if data.get("stop_reason") == "max_tokens":
            last_error = "The answer was cut off before every question was covered"
            retry_note = "The previous reply was cut off. Keep each evidence line to one short sentence."
            continue
        parsed = _answers_from_content(data.get("content") or [], known_ids)
        if parsed.get("error"):
            last_error = parsed["error"]
            retry_note = "Call submit_question_answers with one item for every question id."
            continue
        answers = parsed["answers"]
        if not answers:
            last_error = "The model returned no answers"
            retry_note = "Call submit_question_answers with one item for every question id."
            continue
        returned = {item["question_id"] for item in answers}
        missing = [row["id"] for row in questions if row["id"] not in returned]
        if missing and require_all and attempt == 0:
            shown = ", ".join(missing[:12])
            last_error = "Some questions were not answered"
            retry_note = f"Also answer these question ids: {shown}."
            continue
        return {
            "chunk_index": chunk["chunk_index"],
            "page_start": chunk["page_start"],
            "page_end": chunk["page_end"],
            "custom_id": f"section-{chunk['chunk_index']}",
            "status": "succeeded",
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": None,
            "answers": answers,
        }
    return {
        "chunk_index": chunk["chunk_index"],
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
        "custom_id": f"section-{chunk['chunk_index']}",
        "status": "errored",
        "raw_text": "",
        "final_category": None,
        "confidence_level": None,
        "error": last_error,
        "answers": [],
    }


def _assemble(template: dict, questions: list[dict], found: dict[str, dict]) -> dict:
    answers = [_record_answer(row, found.get(row["id"])) for row in questions]
    by_id = {row["question_id"]: row for row in answers}
    category, confidence = _choose_category(template, by_id)
    return {
        "raw_text": _result_text(category, confidence, answers),
        "final_category": category,
        "confidence_level": confidence,
        "error": None,
        "answers": answers,
    }


async def categorise_document(
    s3_key: str,
    template: dict,
    job_id: str,
    organisation_instructions: str = "",
) -> dict:
    """
    Answer every question on the template, then pick the category.

    A long AIP is split and each piece sees the full question list. Answers
    are merged. The first category whose questions are all yes is the result.
    """
    usage = _CallUsage()
    try:
        return await _categorise(
            s3_key,
            template,
            job_id,
            usage,
            organisation_instructions=organisation_instructions,
        )
    finally:
        usage.log_total(job_id)


async def _categorise(
    s3_key: str,
    template: dict,
    job_id: str,
    usage: _CallUsage,
    organisation_instructions: str = "",
) -> dict:
    questions = _questions_from_template(template)
    if not questions:
        raise RuntimeError("This template has no questions. Add categories and questions, then run the job again.")

    pdf_bytes = download_pdf_bytes(s3_key)
    pieces = split_pdf_into_chunks(pdf_bytes)
    usage.pages = pieces[-1]["page_end"]
    logger.info(
        "Categorising job %s: %d piece(s), %d question(s)",
        job_id,
        len(pieces),
        len(questions),
    )

    require_all = len(pieces) == 1
    sections = await asyncio.gather(*[
        _answer_piece(
            template,
            questions,
            piece,
            usage,
            require_all=require_all,
            organisation_instructions=organisation_instructions,
        )
        for piece in pieces
    ])
    failed = [section for section in sections if section.get("error")]
    if failed:
        message = "; ".join(section["error"] for section in failed if section.get("error"))
        chunks = [{k: v for k, v in section.items() if k != "answers"} for section in sections]
        return {
            "chunks": chunks,
            "synthesis": None,
            "final_result": {
                "raw_text": "",
                "final_category": None,
                "confidence_level": None,
                "error": message or "One or more sections could not be read",
                "answers": [],
            },
        }

    found: dict[str, dict] = {}
    for section in sections:
        for item in section.get("answers") or []:
            found[item["question_id"]] = _merge_answer(found.get(item["question_id"]), item)

    final_result = _assemble(template, questions, found)
    chunks = [{k: v for k, v in section.items() if k != "answers"} for section in sections]
    return {
        "chunks": chunks,
        "synthesis": None,
        "final_result": final_result,
    }
