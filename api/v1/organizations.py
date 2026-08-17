import re
import secrets
from datetime import datetime, timezone
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from pymongo import ReturnDocument

from api.deps import get_current_db_user
from database.session import get_database
from models.organization import (
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    serialize_organization,
)
from models.user import UserOut, serialize_user

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper: derive slug from name
# ---------------------------------------------------------------------------
def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


# ---------------------------------------------------------------------------
# Pydantic request bodies for sub-resources
# ---------------------------------------------------------------------------
class InviteUserRequest(BaseModel):
    email: EmailStr
    role: Literal["super-admin", "admin", "user"]
    full_name: Optional[str] = None


class UpdateRoleRequest(BaseModel):
    role: Literal["super-admin", "admin", "user"]


class UpdateTemplatesRequest(BaseModel):
    template_ids: list[str]


# ---------------------------------------------------------------------------
# Organisation CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[OrganizationOut])
async def list_organizations():
    db = await get_database()
    cursor = db["organizations"].find().sort("name", 1)
    docs = await cursor.to_list(length=200)
    return [serialize_organization(d) for d in docs]


@router.get("/{org_id}", response_model=OrganizationOut)
async def get_organization(org_id: str):
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    doc = await db["organizations"].find_one({"_id": ObjectId(org_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Organization not found")
    return serialize_organization(doc)


@router.post("", response_model=OrganizationOut, status_code=201)
async def create_organization(
    data: OrganizationCreate,
    caller: dict = Depends(get_current_db_user),
):
    db = await get_database()
    slug = data.slug or _slugify(data.name)
    existing = await db["organizations"].find_one({"slug": slug})
    if existing:
        raise HTTPException(status_code=400, detail="Organization with this slug already exists")

    now = datetime.now(timezone.utc)
    doc = {
        "name": data.name,
        "slug": slug,
        "is_active": True,
        "templates": [],
        "created_at": now,
        "created_by_user_id": caller["_id"],
    }
    result = await db["organizations"].insert_one(doc)
    created = await db["organizations"].find_one({"_id": result.inserted_id})
    return serialize_organization(created)


@router.patch("/{org_id}", response_model=OrganizationOut)
async def update_organization(org_id: str, data: OrganizationUpdate):
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    updates: dict = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.is_active is not None:
        updates["is_active"] = data.is_active
    if data.templates is not None:
        template_ids = []
        for tid in data.templates:
            if not ObjectId.is_valid(tid):
                raise HTTPException(status_code=400, detail=f"Invalid template ID: {tid}")
            template_ids.append(ObjectId(tid))
        updates["templates"] = template_ids

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    doc = await db["organizations"].find_one_and_update(
        {"_id": ObjectId(org_id)},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Organization not found")
    return serialize_organization(doc)


# ---------------------------------------------------------------------------
# Templates for an org
# ---------------------------------------------------------------------------

@router.get("/{org_id}/templates/summary")
async def get_org_templates(org_id: str):
    """Return the template summaries available to a specific organization."""
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    template_ids = org.get("templates", [])
    if not template_ids:
        return []

    cursor = db["prompt_templates"].find(
        {"_id": {"$in": template_ids}, "is_active": True},
        {"content": 0},
    ).sort("name", 1)
    docs = await cursor.to_list(length=100)
    return [
        {
            "id": str(d["_id"]),
            "name": d["name"],
            "category": d.get("category"),
            "description": d.get("description"),
        }
        for d in docs
    ]


@router.put("/{org_id}/templates")
async def set_org_templates(org_id: str, body: UpdateTemplatesRequest):
    """Replace the full list of templates assigned to this org. Active templates only."""
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    template_oids = []
    for tid in body.template_ids:
        if not ObjectId.is_valid(tid):
            raise HTTPException(status_code=400, detail=f"Invalid template ID: {tid}")
        template_oids.append(ObjectId(tid))

    if template_oids:
        active_count = await db["prompt_templates"].count_documents({
            "_id": {"$in": template_oids},
            "is_active": True,
        })
        if active_count != len(template_oids):
            raise HTTPException(
                status_code=400,
                detail="One or more templates are inactive or do not exist",
            )

    doc = await db["organizations"].find_one_and_update(
        {"_id": ObjectId(org_id)},
        {"$set": {"templates": template_oids}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Organization not found")
    return serialize_organization(doc)


# ---------------------------------------------------------------------------
# Users in an org
# ---------------------------------------------------------------------------

@router.get("/{org_id}/users", response_model=list[UserOut])
async def list_org_users(org_id: str):
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    cursor = db["users"].find({"organization_id": ObjectId(org_id)}).sort("created_at", 1)
    docs = await cursor.to_list(length=500)
    return [serialize_user(d) for d in docs]


@router.post("/{org_id}/invite", response_model=UserOut, status_code=201)
async def invite_user(
    org_id: str,
    body: InviteUserRequest,
    caller: dict = Depends(get_current_db_user),
):
    """
    Invite a user to the organisation.
    Creates their SuperTokens account with a random password, then immediately
    triggers a password-reset email so they set their own password on first login.
    The user record is stored with invite_status='pending'.
    """
    db = await get_database()
    if not ObjectId.is_valid(org_id):
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Check if already in our DB
    existing_user = await db["users"].find_one({"email": body.email})
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="A user with this email already exists in the system.",
        )

    # Create SuperTokens account
    from supertokens_python.recipe.emailpassword.asyncio import sign_up
    from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult

    random_password = secrets.token_urlsafe(24)
    signup_result = await sign_up("public", body.email, random_password)
    if not isinstance(signup_result, SignUpOkResult):
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists in the auth system.",
        )

    st_user_id = signup_result.user.id

    # Create password reset token so the user can set their password
    from supertokens_python.recipe.emailpassword.asyncio import create_reset_password_token
    from supertokens_python.recipe.emailpassword.interfaces import CreateResetPasswordOkResult

    token_result = await create_reset_password_token("public", st_user_id, body.email)
    invite_link: Optional[str] = None
    if isinstance(token_result, CreateResetPasswordOkResult):
        from config.settings import settings
        token = token_result.token
        invite_link = (
            f"{settings.WEBSITE_DOMAIN}/reset-password"
            f"?token={token}&rid=emailpassword"
        )

    # Try to send the invite email via Resend; fall back to copy-link if not configured
    from services.email import send_invite_email
    email_sent = False
    if invite_link:
        email_sent = send_invite_email(
            to_email=body.email,
            invite_link=invite_link,
            org_name=org["name"],
        )

    now = datetime.now(timezone.utc)
    user_doc = {
        "supertokens_user_id": st_user_id,
        "email": body.email,
        "full_name": body.full_name or body.email.split("@")[0],
        "organization_id": ObjectId(org_id),
        "role": body.role,
        "is_active": True,
        "invite_status": "pending",
        # Keep the link stored so it can be re-sent or copied if email failed
        "invite_link": invite_link,
        "invite_email_sent": email_sent,
        "created_at": now,
        "created_by_user_id": caller["_id"],
    }
    result = await db["users"].insert_one(user_doc)
    created = await db["users"].find_one({"_id": result.inserted_id})
    return serialize_user(created)


@router.get("/{org_id}/invite/{user_id}/link")
async def get_invite_link(org_id: str, user_id: str):
    """Return the invite link for a pending user (for copy-to-clipboard)."""
    db = await get_database()
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = await db["users"].find_one({
        "_id": ObjectId(user_id),
        "organization_id": ObjectId(org_id),
    })
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Regenerate the token if it's stale
    from supertokens_python.recipe.emailpassword.asyncio import create_reset_password_token
    from supertokens_python.recipe.emailpassword.interfaces import CreateResetPasswordOkResult

    token_result = await create_reset_password_token(
        "public", user["supertokens_user_id"], user["email"]
    )
    if not isinstance(token_result, CreateResetPasswordOkResult):
        raise HTTPException(status_code=500, detail="Could not generate invite link")

    from config.settings import settings
    link = (
        f"{settings.WEBSITE_DOMAIN}/reset-password"
        f"?token={token_result.token}&rid=emailpassword"
    )
    # Persist the fresh link
    await db["users"].update_one({"_id": user["_id"]}, {"$set": {"invite_link": link}})
    return {"invite_link": link}


@router.patch("/{org_id}/users/{user_id}/role", response_model=UserOut)
async def update_user_role(org_id: str, user_id: str, body: UpdateRoleRequest):
    db = await get_database()
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    doc = await db["users"].find_one_and_update(
        {"_id": ObjectId(user_id), "organization_id": ObjectId(org_id)},
        {"$set": {"role": body.role}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="User not found in this organization")
    return serialize_user(doc)


@router.patch("/{org_id}/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(org_id: str, user_id: str):
    """
    Deactivate a user — sets is_active to false so they cannot log in.
    Their account and data are preserved and can be restored via /reactivate.
    """
    db = await get_database()
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    doc = await db["users"].find_one_and_update(
        {"_id": ObjectId(user_id), "organization_id": ObjectId(org_id)},
        {"$set": {"is_active": False}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="User not found in this organization")
    return serialize_user(doc)


@router.patch("/{org_id}/users/{user_id}/reactivate", response_model=UserOut)
async def reactivate_user(org_id: str, user_id: str):
    """
    Reactivate a previously deactivated user — they can log in again.
    """
    db = await get_database()
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    doc = await db["users"].find_one_and_update(
        {"_id": ObjectId(user_id), "organization_id": ObjectId(org_id)},
        {"$set": {"is_active": True}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="User not found in this organization")
    return serialize_user(doc)
