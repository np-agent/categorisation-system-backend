"""Shared FastAPI dependencies."""

from fastapi import Depends, HTTPException
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from database.session import get_database


async def get_current_db_user(
    session: SessionContainer = Depends(verify_session()),
) -> dict:
    """
    Resolve the authenticated SuperTokens session to our MongoDB users document.
    All app-level foreign keys should use this document's _id, not the ST id.
    """
    db = await get_database()
    user = await db["users"].find_one({"supertokens_user_id": session.get_user_id()})
    if not user:
        raise HTTPException(status_code=404, detail="User profile not found")
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Your account has been deactivated.")
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
