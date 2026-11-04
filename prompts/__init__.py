"""
Editable categorisation prompts and structured-output tool schema.

Edit the .txt files in this folder to change prompt wording.
Python only loads, fills variables, and exposes the tool definition.
"""

import json
import re
from pathlib import Path

_DIR = Path(__file__).parent

TOOL_NAME = "submit_categorisation_result"

CATEGORISATION_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Submit the airfield categorisation result. "
        "Always call this tool with the final analysis fields."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Detailed explanation of why this category was chosen, "
                    "including criterion-level evidence and any caveats."
                ),
            },
            "final_category": {
                "type": "string",
                "description": (
                    "The category label decided from the criteria. "
                    "Free-form string — use whatever labels the criteria define."
                ),
            },
            "confidence_level": {
                "type": "string",
                "enum": ["HIGH", "MEDIUM", "LOW"],
                "description": "Confidence in the categorisation decision.",
            },
        },
        "required": ["reasoning", "final_category", "confidence_level"],
    },
}

TOOL_CHOICE = {"type": "tool", "name": TOOL_NAME}

EVIDENCE_TOOL_NAME = "submit_section_notes"

EVIDENCE_TOOL = {
    "name": EVIDENCE_TOOL_NAME,
    "description": (
        "Submit short evidence notes for this section of the AIP. "
        "Do not choose a category."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "string",
                "description": (
                    "One short line per criterion these pages can answer. "
                    "Include TRUE or FALSE, the evidence, and the page number."
                ),
            },
        },
        "required": ["notes"],
    },
}

EVIDENCE_TOOL_CHOICE = {"type": "tool", "name": EVIDENCE_TOOL_NAME}

CHECKLIST_TOOL_NAME = "submit_question_answers"

CHECKLIST_TOOL = {
    "name": CHECKLIST_TOOL_NAME,
    "description": "Submit one answer for every question id. Do not choose a category.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "description": "One item per question id.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question_id": {"type": "string"},
                        "answer": {
                            "type": "string",
                            "enum": ["yes", "no", "unknown"],
                        },
                        "evidence": {
                            "type": "string",
                            "description": "One short line from the document.",
                        },
                        "confidence_level": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                        },
                        "needs_manual": {
                            "type": "boolean",
                            "description": "True when a person should check this question.",
                        },
                        "page": {
                            "type": "integer",
                            "description": "Page number in the full document.",
                        },
                    },
                    "required": [
                        "question_id",
                        "answer",
                        "evidence",
                        "confidence_level",
                        "needs_manual",
                    ],
                },
            },
        },
        "required": ["answers"],
    },
}

CHECKLIST_TOOL_CHOICE = {"type": "tool", "name": CHECKLIST_TOOL_NAME}

# Words that can follow "category" in prose and are not a category code.
_NOT_A_CODE = {
    "OF", "THE", "AND", "TO", "FOR", "ALL", "NOT", "OR", "IF", "IN", "ON",
    "BY", "AS", "AN", "IS", "BE", "THAT", "WHICH", "WITH",
}
_CATEGORY_CODE = re.compile(r"(?i)\bcategory(?:\s+is)?\s+([A-Za-z]{1,4})\b")
_CATEGORY_VALUE = re.compile(r"(?i)^(?:category\s+)?([A-Za-z]{1,4})\.?$")


def _load(filename: str) -> str:
    return (_DIR / filename).read_text(encoding="utf-8").strip()


_ANALYSIS_ROLE = _load("analysis_role.txt")
_CHECKLIST_SYSTEM = _load("checklist_system.txt")
_CHECKLIST_USER = _load("checklist_user.txt")
_SECTION_SYSTEM = _load("section_system.txt")
_SECTION_USER = _load("section_user.txt")
_DECISION_SYSTEM = _load("decision_system.txt")
_DECISION_USER = _load("decision_user.txt")


def category_labels(template_content: str) -> list[str]:
    """
    Labels written in the criteria, in the order they first appear.

    "Category A" and "Category is BB" both count. An empty list means the
    criteria do not name labels we can check.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _CATEGORY_CODE.finditer(template_content or ""):
        code = match.group(1).upper()
        if code in _NOT_A_CODE or code in seen:
            continue
        seen.add(code)
        found.append(f"Category {code}")
    return found


def canonical_category(value: str | None, allowed: list[str]) -> str | None:
    """
    Map a model answer onto an allowed label.

    Accepts "A" and "Category A" as the same label. When the criteria name
    no labels, a non-empty answer is kept as written.
    """
    text = (value or "").strip()
    if not text:
        return None
    if not allowed:
        return text
    match = _CATEGORY_VALUE.match(text)
    if not match:
        return None
    code = match.group(1).upper()
    for label in allowed:
        if label.rsplit(" ", 1)[-1].upper() == code:
            return label
    return None


def decision_tool(allowed: list[str]) -> dict:
    """The result tool, restricted to the template's labels when we know them."""
    tool = json.loads(json.dumps(CATEGORISATION_TOOL))
    description = (
        "Exactly one of: " + ", ".join(allowed)
        if allowed
        else "The category label written in the criteria."
    )
    tool["input_schema"]["properties"]["final_category"]["description"] = description
    if allowed:
        tool["input_schema"]["properties"]["final_category"]["enum"] = allowed
    return tool


def build_checklist_system(
    page_start: int,
    page_end: int,
    *,
    retry_note: str = "",
) -> str:
    page_note = (
        f"The attached pages are pages {page_start}-{page_end} of the full document. "
        "Report page numbers from the full document."
    )
    return _CHECKLIST_SYSTEM.format(
        analysis_role=_ANALYSIS_ROLE,
        page_note=page_note,
        retry_note=retry_note,
    ).strip()


def build_checklist_user(
    notes: str,
    questions: list[dict],
    organisation_instructions: str = "",
) -> str:
    payload = {
        "notes": (notes or "").strip(),
        "questions": [
            {"id": row["id"], "category": row["category"], "text": row["text"]}
            for row in questions
        ],
    }
    instructions = (organisation_instructions or "").strip()
    if instructions:
        payload["organisation_instructions"] = instructions
    return _CHECKLIST_USER.format(
        questions_json=json.dumps(payload, indent=2, ensure_ascii=False),
    ).strip()


def build_section_system(page_start: int, page_end: int) -> str:
    """System prompt for one slice of a long AIP. Evidence only."""
    return _SECTION_SYSTEM.format(
        analysis_role=_ANALYSIS_ROLE,
        page_start=page_start,
        page_end=page_end,
    ).strip()


def build_section_user(template_content: str) -> str:
    return _SECTION_USER.format(template_content=template_content).strip()


def build_decision_system(allowed: list[str]) -> str:
    if allowed:
        category_rule = "exactly one of: " + ", ".join(allowed)
    else:
        category_rule = "a category label that is written in the criteria"
    return _DECISION_SYSTEM.format(
        analysis_role=_ANALYSIS_ROLE,
        category_rule=category_rule,
    ).strip()


def build_decision_user(
    template_content: str,
    *,
    evidence_block: str,
    retry_note: str = "",
) -> str:
    return _DECISION_USER.format(
        template_content=template_content,
        evidence_block=evidence_block,
        retry_note=retry_note,
    ).strip()
