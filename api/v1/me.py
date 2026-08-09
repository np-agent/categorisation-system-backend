"""
/api/v1/me — user profile endpoint.

Called by the frontend immediately after a successful SuperTokens login.
On first call for a new user, creates their profile in the users collection.

Bootstrap mode: if no users exist yet in the DB, the first signup gets
super-admin role automatically. Disable BOOTSTRAP_SUPER_ADMIN in .env
once the first super-admin account is set up.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument
from supertokens_python.asyncio import get_user as st_get_user
from supertokens_python.recipe.session.framework.fastapi import verify_session
from supertokens_python.recipe.session import SessionContainer

from database.session import get_database
from models.user import UserOut, serialize_user
from config.settings import settings

router = APIRouter()


@router.get("", response_model=UserOut)
async def get_me(session: SessionContainer = Depends(verify_session())):
    """
    Return the current user's profile.
    Creates the profile record on first call (post-signup).
    """
    supertokens_user_id = session.get_user_id()
    db = await get_database()

    user = await db["users"].find_one({"supertokens_user_id": supertokens_user_id})
    if user:
        # Flip invite_status to accepted on first login after being invited
        if user.get("invite_status") == "pending":
            user = await db["users"].find_one_and_update(
                {"_id": user["_id"]},
                {"$set": {"invite_status": "accepted"}},
                return_document=ReturnDocument.AFTER,
            )
        # Block deactivated users — SuperTokens account is kept intact so
        # access can be restored simply by setting is_active back to true.
        if not user.get("is_active", True):
            raise HTTPException(status_code=403, detail="Your account has been deactivated.")
        return serialize_user(user)

    # First time — create the profile.
    # Determine role: super-admin if bootstrap mode and no users exist yet.
    user_count = await db["users"].count_documents({})
    if settings.BOOTSTRAP_SUPER_ADMIN and user_count == 0:
        role = "super-admin"
        print(f"Bootstrap: granting super-admin to first user {supertokens_user_id}")
    else:
        role = "user"

    now = datetime.now(timezone.utc)

    # Ensure SelfBrief Aero org exists for bootstrap / default assignment
    default_org = await db["organizations"].find_one({"slug": "selfbrief-aero"})
    org_needs_creator = False
    if not default_org:
        if role != "super-admin":
            raise HTTPException(
                status_code=403,
                detail="No organisation assigned. Ask a super admin to invite you.",
            )
        insert = await db["organizations"].insert_one({
            "name": "SelfBrief Aero",
            "slug": "selfbrief-aero",
            "is_active": True,
            "templates": [],
            "created_at": now,
            "created_by_user_id": None,
        })
        default_org = await db["organizations"].find_one({"_id": insert.inserted_id})
        org_needs_creator = True
        print(f"Bootstrap: created SelfBrief Aero org {insert.inserted_id}")

    st_user = await st_get_user(supertokens_user_id)
    email = ""
    if st_user and st_user.emails:
        email = st_user.emails[0]

    new_user = {
        "supertokens_user_id": supertokens_user_id,
        "email": email,
        "full_name": email.split("@")[0] if email else "User",
        "organization_id": default_org["_id"],
        "role": role,
        "is_active": True,
        "invite_status": "accepted",
        "created_at": now,
        "created_by_user_id": None,  # set to self after insert
    }

    result = await db["users"].insert_one(new_user)
    await db["users"].update_one(
        {"_id": result.inserted_id},
        {"$set": {"created_by_user_id": result.inserted_id}},
    )
    if org_needs_creator:
        await db["organizations"].update_one(
            {"_id": default_org["_id"]},
            {"$set": {"created_by_user_id": result.inserted_id}},
        )
    created = await db["users"].find_one({"_id": result.inserted_id})
    return serialize_user(created)


@router.patch("", response_model=UserOut)
async def update_me(
    full_name: str,
    session: SessionContainer = Depends(verify_session()),
):
    """Update the current user's display name."""
    supertokens_user_id = session.get_user_id()
    db = await get_database()

    user = await db["users"].find_one_and_update(
        {"supertokens_user_id": supertokens_user_id},
        {"$set": {"full_name": full_name}},
        return_document=True,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User profile not found")
    return serialize_user(user)
