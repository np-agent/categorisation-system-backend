"""Shared FastAPI dependencies."""

from fastapi import Depends, Header, HTTPException

from database.session import get_database
from services.legal import has_accepted_current_eula
from services.sso import fetch_cms_me, remember_access_token, upsert_user_from_claims


ORG_INACTIVE_DETAIL = "Your organisation has been deactivated."
EULA_REQUIRED_DETAIL = "Please accept the End User Licence Agreement to continue."


async def assert_org_active(db, organization_id) -> None:
    """
    Deactivating an org revokes access for everyone inside it.

    The deactivation also switches each member's own is_active flag off, so this
    check is belt and braces: it still blocks the org if those flags ever drift
    out of sync with the org's state.
    """
    if organization_id is None:
        return
    org = await db["organizations"].find_one(
        {"_id": organization_id}, {"is_active": 1}
    )
    if org is None or not org.get("is_active", True):
        raise HTTPException(status_code=403, detail=ORG_INACTIVE_DETAIL)


async def _bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return token.strip()


async def _load_active_user(access_token: str, id_token: str | None = None) -> dict:
    claims = await fetch_cms_me(access_token, id_token)
    user = await upsert_user_from_claims(claims)
    await remember_access_token(user, access_token)
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Your account has been deactivated.")
    db = await get_database()
    await assert_org_active(db, user.get("organization_id"))
    return user


async def get_current_db_user_pre_eula(
    access_token: str = Depends(_bearer_token),
    id_token: str | None = Header(default=None, alias="X-SelfBrief-Id-Token"),
) -> dict:
    """Active user dependency used only for accepting the EULA."""
    return await _load_active_user(access_token, id_token)


async def get_current_db_user(
    access_token: str = Depends(_bearer_token),
    id_token: str | None = Header(default=None, alias="X-SelfBrief-Id-Token"),
) -> dict:
    """
    Resolve the CMS access token to our MongoDB users document.
    The current EULA must have been accepted before any other API can be used.
    """
    user = await _load_active_user(access_token, id_token)
    if not has_accepted_current_eula(user):
        raise HTTPException(status_code=403, detail=EULA_REQUIRED_DETAIL)
    return user


async def build_user_map(db, creator_ids: list) -> dict:
    """
    Build a lookup map for creator ids.

    Keys are the Mongo user id and the CMS user id.
    """
    from bson import ObjectId

    if not creator_ids:
        return {}

    oids = []
    extra_ids = []
    for raw in creator_ids:
        if raw is None:
            continue
        if isinstance(raw, ObjectId):
            oids.append(raw)
        elif ObjectId.is_valid(str(raw)) and len(str(raw)) == 24:
            oids.append(ObjectId(str(raw)))
        else:
            extra_ids.append(str(raw))

    query: dict = {"$or": []}
    if oids:
        query["$or"].append({"_id": {"$in": oids}})
    if extra_ids:
        query["$or"].append({"cms_user_id": {"$in": extra_ids}})
    if not query["$or"]:
        return {}

    users = await db["users"].find(query).to_list(length=500)
    user_map: dict = {}
    for u in users:
        user_map[str(u["_id"])] = u
        if u.get("cms_user_id"):
            user_map[str(u["cms_user_id"])] = u
    return user_map


def creator_lookup_key(raw) -> str:
    """Normalize a stored created_by / created_by_user_id value to a map key."""
    if raw is None:
        return ""
    return str(raw)
