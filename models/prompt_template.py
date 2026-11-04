import re
from datetime import datetime
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel


_ID_RE = re.compile(r"^[A-Za-z0-9_]{4,40}$")


class TemplateQuestion(BaseModel):
    id: str
    text: str


class TemplateCategory(BaseModel):
    id: str
    code: str
    questions: list[TemplateQuestion]


class TemplateQuestionIn(BaseModel):
    id: Optional[str] = None
    text: str = ""


class TemplateCategoryIn(BaseModel):
    id: Optional[str] = None
    code: str = ""
    questions: list[TemplateQuestionIn] = []


class PromptTemplateCreate(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    notes: str = ""
    categories: list[TemplateCategoryIn]


class PromptTemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    categories: Optional[list[TemplateCategoryIn]] = None
    is_active: Optional[bool] = None


class PromptTemplateOut(BaseModel):
    id: str
    name: str
    category: Optional[str]
    description: Optional[str]
    notes: str = ""
    categories: list[TemplateCategory] = []
    content: str
    is_active: bool
    created_by_user_id: Optional[str]
    created_by_name: Optional[str]
    created_by_email: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        populate_by_name = True


class PromptTemplateSummary(BaseModel):
    """Lightweight version for dropdowns. No questions."""
    id: str
    name: str
    category: Optional[str]
    description: Optional[str]


def _keep_id(value: str | None, prefix: str) -> str:
    text = (value or "").strip()
    if _ID_RE.match(text):
        return text
    return f"{prefix}_{uuid4().hex[:12]}"


def normalise_template_structure(notes: str | None, categories: list[dict]) -> dict:
    """
    Check the form and assign stable ids.

    A category is met only when every question under it is yes. The list
    order is the order those categories are tested.
    """
    cleaned_notes = (notes or "").strip()
    if not cleaned_notes:
        raise ValueError("Add general notes")
    if len(cleaned_notes) > 8000:
        raise ValueError("General notes are too long")
    if not categories:
        raise ValueError("Add at least one category")

    seen_codes: set[str] = set()
    seen_ids: set[str] = set()
    stored: list[dict] = []
    lines = [cleaned_notes] if cleaned_notes else []

    for raw in categories:
        code = (raw.get("code") or "").strip()
        if not code:
            raise ValueError("Each category needs a name")
        if len(code) > 40:
            raise ValueError("A category name is too long")
        key = code.casefold()
        if key in seen_codes:
            raise ValueError(f"Category {code} is listed twice")
        seen_codes.add(key)

        questions_in = raw.get("questions") or []
        if not questions_in:
            raise ValueError(f"Add at least one question under {code}")

        cat_id = _keep_id(raw.get("id"), "c")
        while cat_id in seen_ids:
            cat_id = _keep_id(None, "c")
        seen_ids.add(cat_id)

        questions: list[dict] = []
        lines.append("")
        lines.append(code)
        for item in questions_in:
            text = (item.get("text") or "").strip()
            if not text:
                raise ValueError(f"A question under {code} is empty")
            if len(text) > 2000:
                raise ValueError(f"A question under {code} is too long")
            qid = _keep_id(item.get("id"), "q")
            while qid in seen_ids:
                qid = _keep_id(None, "q")
            seen_ids.add(qid)
            questions.append({"id": qid, "text": text})
            lines.append(f"- {text}")

        stored.append({"id": cat_id, "code": code, "questions": questions})

    return {
        "notes": cleaned_notes,
        "categories": stored,
        "content": "\n".join(lines).strip(),
    }


def serialize_template(doc: dict, user_map: dict | None = None) -> PromptTemplateOut:
    creator_id = doc.get("created_by_user_id")
    creator_key = str(creator_id) if creator_id else ""
    creator = (user_map or {}).get(creator_key) or {}
    categories = []
    for raw in doc.get("categories") or []:
        questions = [
            TemplateQuestion(id=q.get("id") or "", text=q.get("text") or "")
            for q in raw.get("questions") or []
            if (q.get("text") or "").strip()
        ]
        code = (raw.get("code") or "").strip()
        if code and questions:
            categories.append(TemplateCategory(
                id=raw.get("id") or "",
                code=code,
                questions=questions,
            ))
    return PromptTemplateOut(
        id=str(doc["_id"]),
        name=doc["name"],
        category=doc.get("category"),
        description=doc.get("description"),
        notes=doc.get("notes") or "",
        categories=categories,
        content=doc.get("content") or "",
        is_active=doc.get("is_active", True),
        created_by_user_id=creator_key or None,
        created_by_name=creator.get("full_name") or None,
        created_by_email=creator.get("email"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def serialize_template_summary(doc: dict) -> PromptTemplateSummary:
    return PromptTemplateSummary(
        id=str(doc["_id"]),
        name=doc["name"],
        category=doc.get("category"),
        description=doc.get("description"),
    )
