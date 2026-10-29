"""
Resolve a CMS access token to a local users document.

CMS is the identity source. We store a profile (role, org, EULA) keyed by
cms_user_id (the OIDC sub), not by email.

On every authenticated API call we re-fetch GET /api/v1/sso/me/ so access
changes apply on the next request. There is no CMS webhook.

CMS returns 401 only when this user account is disabled. That ends the
login. If we can match the token to someone already stored, that person
is marked inactive. A new person who has never signed in is not created.

Each organisation on the profile has is_active, meaning this person can
use that operator. It is false when categorisation is switched off (the
role is kept) and when this person has no access (the role is null).
The session continues if any organisation in the list is active, and ends
only when none are. That person is then marked inactive in our database so
the user list matches. A suspension is a 401 on a token we have already
seen, and marks that same person inactive. A later successful sign-in
marks them active again. This profile does not change the organisation
row. A CMS scheduled task keeps that status.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from config.settings import settings
from database.session import get_database

logger = logging.getLogger(__name__)

_jwks_client: PyJWKClient | None = None

CASCADE_FLAG = "deactivated_by_org"
TOKEN_HASHES = "cms_token_hashes"
NO_ACCESS_DETAIL = "Your SelfBrief account is not permitted to use this application."


def is_cms_super_admin(claims: dict) -> bool:
    return claims.get("is_super_admin") is True


def organisation_claims(claims: dict) -> list[dict]:
    """
    CMS returns every organisation on this person's profile.
    Today that list has one item. A single organisation object is accepted
    so an older payload still parses.
    """
    raw = claims.get("organisations")
    if raw is None:
        raw = claims.get("organizations")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    single = claims.get("organisation") or claims.get("organization")
    if isinstance(single, dict):
        return [single]
    return []


def organisation_is_active(organisation: dict) -> bool:
    """True only when this organisation is available for the signed-in user."""
    return organisation.get("is_active") is True


def organisation_app_role(organisation: dict) -> str:
    name = str(organisation.get("role") or "").strip().lower().replace(" ", "_")
    if name in {"admin", "administrator"}:
        return "admin"
    if name == "user":
        return "user"
    raise HTTPException(status_code=403, detail=NO_ACCESS_DETAIL)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "organisation"


def _claim_sub(claims: dict) -> str:
    sub = claims.get("sub") or claims.get("user_id")
    if sub is None or str(sub).strip() == "":
        raise HTTPException(status_code=502, detail="SelfBrief did not return a user id.")
    return str(sub)


def _token_hash(access_token: str) -> str:
    return hashlib.sha256(access_token.encode()).hexdigest()


async def _deactivate_existing_user(db, cms_user_id: str) -> None:
    """Mark a person we already know as inactive. Does not create a row."""
    if not cms_user_id:
        return
    await db["users"].update_one(
        {"cms_user_id": cms_user_id},
        {"$set": {"is_active": False, TOKEN_HASHES: []}},
    )


def _cms_user_id_from_id_token(id_token: str | None) -> str | None:
    """Read the user id from a signed SelfBrief ID token. Expired or forged tokens are ignored."""
    global _jwks_client
    if not id_token or id_token.count(".") != 2:
        return None
    issuer = (settings.SELFBRIEF_ISSUER or "").rstrip("/")
    if not issuer:
        return None
    try:
        if _jwks_client is None:
            discovery = httpx.get(
                f"{issuer}/.well-known/openid-configuration",
                timeout=10.0,
            )
            discovery.raise_for_status()
            jwks_uri = discovery.json().get("jwks_uri")
            if not jwks_uri:
                return None
            _jwks_client = PyJWKClient(jwks_uri)
        signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
        )
    except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
        logger.info("Could not read the SelfBrief identity token: %s", exc)
        return None
    if not isinstance(claims, dict):
        return None
    raw = claims.get("sub") or claims.get("user_id")
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw)


async def _deactivate_from_rejected_login(access_token: str, id_token: str | None) -> None:
    """
    A 401 means this account is suspended. The profile is not returned.
    Match the access token from their last successful sign-in, or the
    identity token from this sign-in attempt.
    """
    db = await get_database()
    matched = await db["users"].find_one(
        {TOKEN_HASHES: _token_hash(access_token)},
        {"cms_user_id": 1},
    )
    cms_user_id = str(matched.get("cms_user_id")) if matched and matched.get("cms_user_id") else ""
    if not cms_user_id:
        cms_user_id = _cms_user_id_from_id_token(id_token) or ""
    await _deactivate_existing_user(db, cms_user_id)


async def remember_access_token(user: dict, access_token: str) -> None:
    """Keep a short list of this person's recent access tokens so a later 401 can find them."""
    if not user or not user.get("_id") or not access_token:
        return
    db = await get_database()
    current = await db["users"].find_one({"_id": user["_id"]}, {TOKEN_HASHES: 1})
    hashes = list((current or {}).get(TOKEN_HASHES) or [])
    digest = _token_hash(access_token)
    hashes = [item for item in hashes if item != digest]
    hashes.append(digest)
    await db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {TOKEN_HASHES: hashes[-8:]}},
    )


async def fetch_cms_me(access_token: str, id_token: str | None = None) -> dict:
    base = (settings.SELFBRIEF_BASE_URL or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="SelfBrief SSO is not configured on the server.",
        )
    url = f"{base}/api/v1/sso/me/"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.RequestError as exc:
        logger.error("CMS SSO me request failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not reach SelfBrief to verify your session.",
        ) from exc

    # 401 means this user is suspended. Mark the known row inactive, then stop.
    if response.status_code == 401:
        await _deactivate_from_rejected_login(access_token, id_token)
        raise HTTPException(status_code=401, detail="Not authenticated")
    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="Your SelfBrief account is not permitted to use this application.",
        )
    if response.status_code >= 400:
        logger.error("CMS SSO me returned %s: %s", response.status_code, response.text[:300])
        raise HTTPException(
            status_code=502,
            detail="Could not verify your SelfBrief session.",
        )
    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected SelfBrief profile response.")
    return data


async def _ensure_internal_org(db, now: datetime) -> dict:
    org = await db["organizations"].find_one({"slug": settings.INTERNAL_ORG_SLUG})
    if org:
        return org
    result = await db["organizations"].insert_one({
        "name": settings.INTERNAL_ORG_NAME,
        "slug": settings.INTERNAL_ORG_SLUG,
        "is_active": True,
        "templates": [],
        "created_at": now,
        "created_by_user_id": None,
    })
    org = await db["organizations"].find_one({"_id": result.inserted_id})
    if not org:
        raise HTTPException(status_code=500, detail="Could not create the internal organisation.")
    return org


async def _find_operator_org(db, organisation_claim: dict) -> dict | None:
    cms_org_id = organisation_claim.get("id")
    if cms_org_id is not None:
        found = await db["organizations"].find_one({"cms_organisation_id": str(cms_org_id)})
        if found:
            return found
    name = (
        organisation_claim.get("registered_name")
        or organisation_claim.get("name")
        or ""
    )
    slug = _slugify(str(name)) if name else ""
    if slug and slug != settings.INTERNAL_ORG_SLUG:
        return await db["organizations"].find_one({"slug": slug})
    return None


async def _ensure_operator_org(db, organisation_claim: dict | None, now: datetime) -> dict:
    if not organisation_claim or not isinstance(organisation_claim, dict):
        raise HTTPException(
            status_code=403,
            detail="Your SelfBrief account has no organisation. Ask a SelfBrief administrator for access.",
        )
    cms_org_id = organisation_claim.get("id")
    name = (
        organisation_claim.get("registered_name")
        or organisation_claim.get("name")
        or "Organisation"
    )
    existing = await _find_operator_org(db, organisation_claim)
    if existing:
        updates = {}
        if existing.get("name") != name:
            updates["name"] = name
        if cms_org_id is not None and not existing.get("cms_organisation_id"):
            updates["cms_organisation_id"] = str(cms_org_id)
        if updates:
            await db["organizations"].update_one({"_id": existing["_id"]}, {"$set": updates})
            existing.update(updates)
        return existing

    slug = _slugify(str(name))
    if slug == settings.INTERNAL_ORG_SLUG:
        slug = f"{slug}-operator"
    doc = {
        "name": name,
        "slug": slug,
        "is_active": True,
        "templates": [],
        "created_at": now,
        "created_by_user_id": None,
    }
    if cms_org_id is not None:
        doc["cms_organisation_id"] = str(cms_org_id)
    result = await db["organizations"].insert_one(doc)
    org = await db["organizations"].find_one({"_id": result.inserted_id})
    if not org:
        raise HTTPException(status_code=500, detail="Could not create the organisation.")
    return org


async def _choose_organisation(db, claims: dict) -> dict:
    """
    Pick the organisation for this session.

    Organisations with is_active false are skipped. The session ends only
    when the list has no active organisation, and that person is marked
    inactive. With several, keep the one this user already belongs to when
    it is still active, otherwise the first active one. There is no
    organisation switcher yet.
    """
    options = [item for item in organisation_claims(claims) if organisation_is_active(item)]
    if not options:
        try:
            await _deactivate_existing_user(db, _claim_sub(claims))
        except HTTPException:
            pass
        raise HTTPException(status_code=403, detail=NO_ACCESS_DETAIL)

    cms_user_id = _claim_sub(claims)
    user = await db["users"].find_one({"cms_user_id": cms_user_id})
    current_id = None
    if user and user.get("organization_id"):
        current = await db["organizations"].find_one(
            {"_id": user["organization_id"]},
            {"cms_organisation_id": 1},
        )
        if current and current.get("cms_organisation_id"):
            current_id = str(current["cms_organisation_id"])
    if current_id:
        for option in options:
            if str(option.get("id")) == current_id:
                return option
    return options[0]


async def _resolve_operator(db, claims: dict, now: datetime) -> tuple[str, dict]:
    chosen = await _choose_organisation(db, claims)
    role = organisation_app_role(chosen)
    org = await _ensure_operator_org(db, chosen, now)
    return role, org


async def upsert_user_from_claims(claims: dict) -> dict:
    db = await get_database()
    now = datetime.now(timezone.utc)
    cms_user_id = _claim_sub(claims)
    email = str(claims.get("email") or "").strip().lower()
    full_name = (
        str(claims.get("name") or "").strip()
        or " ".join(
            part for part in [
                str(claims.get("first_name") or "").strip(),
                str(claims.get("last_name") or "").strip(),
            ] if part
        )
        or (email.split("@")[0] if email else "User")
    )
    if is_cms_super_admin(claims):
        role = "super-admin"
        org = await _ensure_internal_org(db, now)
    else:
        role, org = await _resolve_operator(db, claims, now)

    user = await db["users"].find_one({"cms_user_id": cms_user_id})
    if not user and email:
        user = await db["users"].find_one({"email": email})

    fields = {
        "cms_user_id": cms_user_id,
        "email": email or (user.get("email") if user else ""),
        "full_name": full_name,
        "organization_id": org["_id"],
        "role": role,
        "is_active": True,
    }

    if user:
        await db["users"].update_one(
            {"_id": user["_id"]},
            {"$set": fields, "$unset": {CASCADE_FLAG: "", "invite_status": ""}},
        )
        updated = await db["users"].find_one({"_id": user["_id"]})
        if not updated:
            raise HTTPException(status_code=404, detail="User profile not found")
        return updated

    new_user = {
        **fields,
        "created_at": now,
        "created_by_user_id": None,
    }
    result = await db["users"].insert_one(new_user)
    await db["users"].update_one(
        {"_id": result.inserted_id},
        {"$set": {"created_by_user_id": result.inserted_id}},
    )
    created = await db["users"].find_one({"_id": result.inserted_id})
    if not created:
        raise HTTPException(status_code=500, detail="Could not create user profile.")
    return created
