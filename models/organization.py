from datetime import datetime
from typing import Optional
from bson import ObjectId
from pydantic import BaseModel


class PyObjectId(str):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v, _info=None):
        if isinstance(v, ObjectId):
            return str(v)
        if ObjectId.is_valid(v):
            return str(v)
        raise ValueError(f"Invalid ObjectId: {v}")


class OrganizationCreate(BaseModel):
    name: str
    slug: Optional[str] = None  # derived from name if not provided


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    templates: Optional[list[str]] = None  # list of template_id strings


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str
    is_active: bool
    templates: list[str]
    created_at: datetime

    class Config:
        populate_by_name = True


def serialize_organization(doc: dict) -> OrganizationOut:
    return OrganizationOut(
        id=str(doc["_id"]),
        name=doc["name"],
        slug=doc["slug"],
        is_active=doc.get("is_active", True),
        templates=[str(t) for t in doc.get("templates", [])],
        created_at=doc["created_at"],
    )
