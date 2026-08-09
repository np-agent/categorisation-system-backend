from datetime import datetime
from typing import Literal
from pydantic import BaseModel


AppRole = Literal["super-admin", "admin", "user"]
InviteStatus = Literal["accepted", "pending"]


class UserOut(BaseModel):
    id: str
    supertokens_user_id: str
    email: str
    full_name: str
    organization_id: str
    role: AppRole
    is_active: bool
    invite_status: InviteStatus
    created_at: datetime

    class Config:
        populate_by_name = True


def serialize_user(doc: dict) -> UserOut:
    return UserOut(
        id=str(doc["_id"]),
        supertokens_user_id=doc["supertokens_user_id"],
        email=doc["email"],
        full_name=doc.get("full_name", ""),
        organization_id=str(doc["organization_id"]),
        role=doc.get("role", "user"),
        is_active=doc.get("is_active", True),
        invite_status=doc.get("invite_status", "accepted"),
        created_at=doc["created_at"],
    )
