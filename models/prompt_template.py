from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class PromptTemplateCreate(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    content: str


class PromptTemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    is_active: Optional[bool] = None


class PromptTemplateOut(BaseModel):
    id: str
    name: str
    category: Optional[str]
    description: Optional[str]
    content: str
    is_active: bool
    created_by_user_id: Optional[str]
    created_by_email: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        populate_by_name = True


class PromptTemplateSummary(BaseModel):
    """Lightweight version for dropdowns — no content field."""
    id: str
    name: str
    category: Optional[str]
    description: Optional[str]


def serialize_template(doc: dict, user_map: dict | None = None) -> PromptTemplateOut:
    creator_id = doc.get("created_by_user_id")
    creator_key = str(creator_id) if creator_id else ""
    creator = (user_map or {}).get(creator_key) or {}
    return PromptTemplateOut(
        id=str(doc["_id"]),
        name=doc["name"],
        category=doc.get("category"),
        description=doc.get("description"),
        content=doc["content"],
        is_active=doc.get("is_active", True),
        created_by_user_id=creator_key or None,
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
