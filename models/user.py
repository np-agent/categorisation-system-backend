from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel

from services.legal import has_accepted_current_eula


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
    # Set when the user was switched off by their organisation being
    # deactivated rather than individually. Only these are restored when the
    # organisation is reactivated.
    deactivated_by_org: bool
    invite_status: InviteStatus
    created_at: datetime
    eula_accepted: bool
    eula_accepted_at: Optional[datetime] = None

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
        deactivated_by_org=bool(doc.get("deactivated_by_org", False)),
        invite_status=doc.get("invite_status", "accepted"),
        created_at=doc["created_at"],
        eula_accepted=has_accepted_current_eula(doc),
        eula_accepted_at=doc.get("eula_accepted_at"),
    )
