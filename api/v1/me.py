"""
/api/v1/me — user profile endpoint.

Called by the frontend after a successful CMS SSO login.
The first authenticated call creates or updates the local users document
from the CMS profile.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from api.deps import get_current_db_user, get_current_db_user_pre_eula
from database.session import get_database
from models.user import UserOut, serialize_user
from services.legal import current_eula_version, has_accepted_current_eula

router = APIRouter()


@router.get("", response_model=UserOut)
async def get_me(caller: dict = Depends(get_current_db_user_pre_eula)):
    return serialize_user(caller)


@router.post("/eula", response_model=UserOut)
async def accept_eula(caller: dict = Depends(get_current_db_user_pre_eula)):
    """
    Record that the current user accepted the current EULA version.
    Each acceptance is appended to eula_acceptances so we keep a timestamp log.
    """
    db = await get_database()
    now = datetime.now(timezone.utc)
    version = current_eula_version()

    if has_accepted_current_eula(caller):
        return serialize_user(caller)

    user = await db["users"].find_one_and_update(
        {"_id": caller["_id"]},
        {
            "$set": {
                "eula_accepted_version": version,
                "eula_accepted_at": now,
            },
            "$push": {
                "eula_acceptances": {
                    "version": version,
                    "accepted_at": now,
                }
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User profile not found")
    return serialize_user(user)


@router.patch("", response_model=UserOut)
async def update_me(
    full_name: str,
    caller: dict = Depends(get_current_db_user),
):
    db = await get_database()

    user = await db["users"].find_one_and_update(
        {"_id": caller["_id"]},
        {"$set": {"full_name": full_name}},
        return_document=ReturnDocument.AFTER,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User profile not found")
    return serialize_user(user)
