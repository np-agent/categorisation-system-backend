"""
Categorisation service.

Handles:
1. Downloading an AIP PDF from S3 into memory (never written to disk)
2. Splitting PDFs over the page threshold into chunks
3. Submitting a batch job to the Anthropic Message Batches API (base64 PDF)
4. Polling batch status and parsing structured tool results
5. Running a sync synthesis call when multiple chunks succeed
"""

import base64
import io
import json
import logging
from typing import Optional

import boto3
import httpx
from pypdf import PdfReader, PdfWriter

from config.settings import settings
from prompts import (
    CATEGORISATION_TOOL,
    TOOL_CHOICE,
    TOOL_NAME,
    build_analysis_system,
    build_analysis_user,
    build_synthesis_system,
    build_synthesis_user,
)

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "message-batches-2024-09-24,pdfs-2024-09-25",
    "content-type": "application/json",
}

# Anthropic allows up to 100 pages per document, but dense AIP pages often
# blow the 200k input token context window well before that. Split earlier
# so each chunk stays under the context limit with headroom for the prompt.
MAX_PAGES_WITHOUT_SPLIT = 60
CHUNK_PAGE_SIZE = 60


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


def _parse_tool_result(content_blocks: list) -> dict:
    """
    Extract structured fields from a message content list.

    Prefers the forced tool_use block. Falls back to JSON text if needed.
    """
    if not content_blocks:
        return {
            "reasoning": "",
            "final_category": None,
            "confidence_level": None,
            "error": "Empty model response content",
        }

    for block in content_blocks:
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            inp = block.get("input") or {}
            reasoning = (inp.get("reasoning") or "").strip()
            final_category = inp.get("final_category")
            confidence_level = inp.get("confidence_level")
            if isinstance(final_category, str):
                final_category = final_category.strip() or None
            if isinstance(confidence_level, str):
                confidence_level = confidence_level.strip().upper() or None
            if not reasoning and not final_category:
                return {
                    "reasoning": "",
                    "final_category": None,
                    "confidence_level": None,
                    "error": "Tool returned empty categorisation fields",
                }
            return {
                "reasoning": reasoning,
                "final_category": final_category,
                "confidence_level": confidence_level,
                "error": None,
            }

    # Fallback: model returned text JSON instead of tool_use
    for block in content_blocks:
        if block.get("type") != "text":
            continue
        text = (block.get("text") or "").strip()
        if not text:
            continue
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                parsed = json.loads(text[start : end + 1])
                return {
                    "reasoning": (parsed.get("reasoning") or "").strip(),
                    "final_category": parsed.get("final_category"),
                    "confidence_level": (
                        str(parsed["confidence_level"]).strip().upper()
                        if parsed.get("confidence_level")
                        else None
                    ),
                    "error": None,
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return {
        "reasoning": "",
        "final_category": None,
        "confidence_level": None,
        "error": "No submit_categorisation_result tool_use found in response",
    }


def _build_chunk_request(
    job_id: str,
    chunk: dict,
    template_content: str,
    *,
    is_partial: bool,
) -> dict:
    custom_id = f"classify-{job_id}-chunk-{chunk['chunk_index']}"
    if is_partial:
        system_prompt = build_analysis_system(
            page_start=chunk["page_start"],
            page_end=chunk["page_end"],
        )
    else:
        system_prompt = build_analysis_system()

    user_text = build_analysis_user(template_content)

    return {
        "custom_id": custom_id,
        "params": {
            "model": settings.ANTHROPIC_MODEL,
            "max_tokens": settings.ANTHROPIC_MAX_TOKENS,
            "temperature": settings.ANTHROPIC_TEMPERATURE,
            "system": system_prompt,
            "tools": [CATEGORISATION_TOOL],
            "tool_choice": TOOL_CHOICE,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        _document_block_from_bytes(chunk["pdf_bytes"]),
                        {
                            "type": "text",
                            "text": user_text,
                        },
                    ],
                }
            ],
        },
    }


async def submit_batch_job(
    airport_s3_key: str,
    template_content: str,
    job_id: str,
) -> dict:
    """
    Download the AIP from S3, split if needed, submit one Anthropic batch.

    Returns:
        {
          batch_id: str,
          chunk_count: int,
          chunks: [{ chunk_index, page_start, page_end, custom_id }, ...]
        }
    """
    pdf_bytes = download_pdf_bytes(airport_s3_key)
    chunks = split_pdf_into_chunks(pdf_bytes)
    del pdf_bytes

    is_partial = len(chunks) > 1
    requests = [
        _build_chunk_request(
            job_id, chunk, template_content, is_partial=is_partial
        )
        for chunk in chunks
    ]

    batch_payload = {"requests": requests}

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{ANTHROPIC_BASE_URL}/messages/batches",
            headers=_anthropic_headers(),
            json=batch_payload,
        )
        if not response.is_success:
            logger.error(
                "Anthropic batch submit failed %s: %s",
                response.status_code,
                response.text,
            )
        response.raise_for_status()
        data = response.json()

    batch_id = data.get("id")
    if not batch_id:
        raise RuntimeError("No batch ID returned from Anthropic")

    chunks_meta = [
        {
            "chunk_index": c["chunk_index"],
            "page_start": c["page_start"],
            "page_end": c["page_end"],
            "custom_id": f"classify-{job_id}-chunk-{c['chunk_index']}",
            "status": None,
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": None,
        }
        for c in chunks
    ]

    logger.info(
        "Batch submitted: %s for job %s (%d chunk(s))",
        batch_id,
        job_id,
        len(chunks_meta),
    )
    return {
        "batch_id": batch_id,
        "chunk_count": len(chunks_meta),
        "chunks": chunks_meta,
    }


def _parse_chunk_result_line(line_obj: dict) -> dict:
    """Parse one JSONL result line into a normalized chunk outcome."""
    custom_id = line_obj.get("custom_id", "")
    result_obj = line_obj.get("result", {})
    result_type = result_obj.get("type")

    if result_type == "succeeded":
        content = result_obj.get("message", {}).get("content") or []
        parsed = _parse_tool_result(content)
        return {
            "custom_id": custom_id,
            "status": "succeeded" if not parsed.get("error") else "errored",
            "raw_text": parsed.get("reasoning") or "",
            "final_category": parsed.get("final_category"),
            "confidence_level": parsed.get("confidence_level"),
            "error": parsed.get("error"),
        }

    if result_type == "errored":
        error_detail = result_obj.get("error", {})
        inner = error_detail.get("error", error_detail)
        error_msg = (
            inner.get("message")
            or error_detail.get("message")
            or str(error_detail)
            or "Unknown batch error"
        )
        return {
            "custom_id": custom_id,
            "status": "errored",
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": error_msg,
        }

    if result_type == "expired":
        return {
            "custom_id": custom_id,
            "status": "expired",
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": "Request expired before Anthropic could process it",
        }

    if result_type == "canceled":
        return {
            "custom_id": custom_id,
            "status": "canceled",
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": "Request was canceled",
        }

    return {
        "custom_id": custom_id,
        "status": "errored",
        "raw_text": "",
        "final_category": None,
        "confidence_level": None,
        "error": f"Unexpected result type: {result_type}",
    }


async def fetch_batch_result(batch_id: str) -> Optional[dict]:
    """
    Check Anthropic batch status. If complete, parse all chunk results.

    Returns:
        None — still in progress
        {
          chunks: [{ custom_id, status, raw_text, final_category, confidence_level, error }, ...],
          batch_error: str | None
        }
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ANTHROPIC_BASE_URL}/messages/batches/{batch_id}",
            headers=_anthropic_headers(),
        )
        response.raise_for_status()
        status_data = response.json()

    processing_status = status_data.get("processing_status")
    logger.info("Batch %s status: %s", batch_id, processing_status)

    if processing_status == "in_progress":
        return None

    if processing_status == "canceling":
        return None

    if processing_status == "errored":
        return {
            "chunks": [],
            "batch_error": "Batch processing failed on Anthropic side",
        }

    results_url = status_data.get("results_url")
    if not results_url:
        return {
            "chunks": [],
            "batch_error": "No results_url returned by Anthropic",
        }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            results_url,
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        response.raise_for_status()

    logger.info("Batch %s JSONL fetched, %d bytes", batch_id, len(response.text))

    lines = [l for l in response.text.strip().split("\n") if l.strip()]
    if not lines:
        return {
            "chunks": [],
            "batch_error": "Empty JSONL results from Anthropic",
        }

    chunks = [_parse_chunk_result_line(json.loads(line)) for line in lines]
    return {"chunks": chunks, "batch_error": None}


async def run_synthesis(template_content: str, chunk_results: list[dict]) -> dict:
    """
    Sync Messages API call that combines multi-chunk analyses into one result.

    Returns:
        {
          prompt_sent: str,
          raw_response: str,          # reasoning text
          final_category: str | None,
          confidence_level: str | None,
          error: str | None,
        }
    """
    system_prompt = build_synthesis_system()
    prompt_sent = build_synthesis_user(template_content, chunk_results)

    payload = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": settings.ANTHROPIC_MAX_TOKENS,
        "temperature": settings.ANTHROPIC_TEMPERATURE,
        "system": system_prompt,
        "tools": [CATEGORISATION_TOOL],
        "tool_choice": TOOL_CHOICE,
        "messages": [
            {
                "role": "user",
                "content": prompt_sent,
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{ANTHROPIC_BASE_URL}/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            if not response.is_success:
                logger.error(
                    "Synthesis call failed %s: %s",
                    response.status_code,
                    response.text,
                )
                return {
                    "prompt_sent": prompt_sent,
                    "raw_response": "",
                    "final_category": None,
                    "confidence_level": None,
                    "error": f"Synthesis API error {response.status_code}: {response.text}",
                }
            data = response.json()
    except Exception as exc:
        logger.error("Synthesis call exception: %s", exc)
        return {
            "prompt_sent": prompt_sent,
            "raw_response": "",
            "final_category": None,
            "confidence_level": None,
            "error": str(exc),
        }

    parsed = _parse_tool_result(data.get("content") or [])
    return {
        "prompt_sent": prompt_sent,
        "raw_response": parsed.get("reasoning") or "",
        "final_category": parsed.get("final_category"),
        "confidence_level": parsed.get("confidence_level"),
        "error": parsed.get("error"),
    }


def merge_chunk_outcomes(
    chunks_meta: list[dict],
    batch_chunk_results: list[dict],
) -> list[dict]:
    """Merge stored chunk metadata with Anthropic JSONL outcomes by custom_id."""
    by_id = {r["custom_id"]: r for r in batch_chunk_results}
    merged: list[dict] = []
    for meta in chunks_meta:
        outcome = by_id.get(meta["custom_id"])
        if not outcome:
            merged.append({
                **meta,
                "status": "errored",
                "raw_text": "",
                "final_category": None,
                "confidence_level": None,
                "error": "No result returned for this chunk",
            })
            continue
        merged.append({
            **meta,
            "status": outcome["status"],
            "raw_text": outcome.get("raw_text", ""),
            "final_category": outcome.get("final_category"),
            "confidence_level": outcome.get("confidence_level"),
            "error": outcome.get("error"),
        })
    return merged
