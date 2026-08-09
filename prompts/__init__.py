"""
Editable categorisation prompts and structured-output tool schema.

Edit the .txt files in this folder to change prompt wording.
Python only loads, fills variables, and exposes the tool definition.
"""

import json
from pathlib import Path

_DIR = Path(__file__).parent

TOOL_NAME = "submit_categorisation_result"

CATEGORISATION_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Submit the airfield categorization result. "
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
                "description": "Confidence in the categorization decision.",
            },
        },
        "required": ["reasoning", "final_category", "confidence_level"],
    },
}

TOOL_CHOICE = {"type": "tool", "name": TOOL_NAME}


def _load(filename: str) -> str:
    return (_DIR / filename).read_text(encoding="utf-8").strip()


_ANALYSIS_ROLE = _load("analysis_role.txt")
_ANALYSIS_SYSTEM = _load("analysis_system.txt")
_ANALYSIS_USER = _load("analysis_user.txt")
_CHUNK_NOTE = _load("chunk_note.txt")
_SYNTHESIS_SYSTEM = _load("synthesis_system.txt")
_SYNTHESIS_USER = _load("synthesis_user.txt")
_SYNTHESIS_SECTION = _load("synthesis_section.txt")


def build_analysis_system(
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> str:
    """System prompt for batch chunk analysis. Includes optional page-range note."""
    chunk_context = ""
    if page_start is not None and page_end is not None:
        chunk_context = _CHUNK_NOTE.format(
            page_start=page_start,
            page_end=page_end,
        )
    return _ANALYSIS_SYSTEM.format(
        analysis_role=_ANALYSIS_ROLE,
        chunk_context=chunk_context,
    ).strip()


def build_analysis_user(template_content: str) -> str:
    """User text for batch chunk analysis (paired with the PDF document block)."""
    return _ANALYSIS_USER.format(template_content=template_content).strip()


def build_synthesis_system() -> str:
    """System prompt for synthesis — includes the full analysis role for context."""
    return _SYNTHESIS_SYSTEM.format(analysis_role=_ANALYSIS_ROLE).strip()


def build_synthesis_user(template_content: str, chunk_results: list[dict]) -> str:
    """User prompt for synthesis — criteria + structured per-section results."""
    section_parts: list[str] = []
    for chunk in chunk_results:
        section_text = chunk.get("raw_text") or "(no analysis text)"
        # Prefer structured fields when present so synthesis sees the full result
        if chunk.get("final_category") or chunk.get("confidence_level"):
            section_text = json.dumps(
                {
                    "reasoning": chunk.get("raw_text") or "",
                    "final_category": chunk.get("final_category"),
                    "confidence_level": chunk.get("confidence_level"),
                },
                indent=2,
                ensure_ascii=False,
            )
        section_parts.append(
            _SYNTHESIS_SECTION.format(
                section_index=chunk.get("chunk_index", 0) + 1,
                page_start=chunk.get("page_start", "?"),
                page_end=chunk.get("page_end", "?"),
                section_text=section_text,
            )
        )

    return _SYNTHESIS_USER.format(
        chunk_count=len(chunk_results),
        template_content=template_content,
        chunk_sections="\n\n".join(section_parts),
    ).strip()
