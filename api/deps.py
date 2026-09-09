"""Shared FastAPI dependencies."""

from fastapi import Depends, HTTPException
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from database.session import get_database
from services.legal import has_accepted_current_eula


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
    # Fail closed: a missing org is as disqualifying as an inactive one,
    # otherwise a missing organisation_id would quietly grant access.
    if org is None or not org.get("is_active", True):
        raise HTTPException(status_code=403, detail=ORG_INACTIVE_DETAIL)


async def _load_active_user(session: SessionContainer) -> dict:
    """
    Resolve the SuperTokens session to an active MongoDB user whose organisation
    is also active. Does not require EULA acceptance.
    """
    db = await get_database()
    user = await db["users"].find_one({"supertokens_user_id": session.get_user_id()})
    if not user:
        raise HTTPException(status_code=404, detail="User profile not found")
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Your account has been deactivated.")
    await assert_org_active(db, user.get("organization_id"))
    return user


async def get_current_db_user_pre_eula(
    session: SessionContainer = Depends(verify_session()),
) -> dict:
    """Active user dependency used only for accepting the EULA."""
    return await _load_active_user(session)


async def get_current_db_user(
    session: SessionContainer = Depends(verify_session()),
) -> dict:
    """
    Resolve the authenticated SuperTokens session to our MongoDB users document.
    All app-level foreign keys should use this document's _id, not the ST id.
    The current EULA must have been accepted before any other API can be used.
    """
    user = await _load_active_user(session)
    if not has_accepted_current_eula(user):
        raise HTTPException(status_code=403, detail=EULA_REQUIRED_DETAIL)
    return user


async def build_user_map(db, creator_ids: list) -> dict:
    """
    Build a lookup map for creator ids.

    Keys include both str(Mongo _id) and supertokens_user_id so legacy
    documents that still store the ST id can resolve email/name.
    """
    from bson import ObjectId

    if not creator_ids:
        return {}

    oids = []
    st_ids = []
    for raw in creator_ids:
        if raw is None:
            continue
        if isinstance(raw, ObjectId):
            oids.append(raw)
        elif ObjectId.is_valid(str(raw)) and len(str(raw)) == 24:
            oids.append(ObjectId(str(raw)))
        else:
            st_ids.append(str(raw))

    query: dict = {"$or": []}
    if oids:
        query["$or"].append({"_id": {"$in": oids}})
    if st_ids:
        query["$or"].append({"supertokens_user_id": {"$in": st_ids}})
    if not query["$or"]:
        return {}

    users = await db["users"].find(query).to_list(length=500)
    user_map: dict = {}
    for u in users:
        user_map[str(u["_id"])] = u
        if u.get("supertokens_user_id"):
            user_map[u["supertokens_user_id"]] = u
    return user_map


def creator_lookup_key(raw) -> str:
    """Normalize a stored created_by / created_by_user_id value to a map key."""
    if raw is None:
        return ""
    return str(raw)
