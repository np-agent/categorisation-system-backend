"""
Keep local organisations in step with SelfBrief operators.

Each run asks CMS for an application token, then for every operator that
currently has Airport Categorisation access. An operator we have never
stored is created. One that returns is restored the same way as the
Reactivate button. One that is missing is archived the same way as
Deactivate. A failed call changes nothing.

The token lasts 15 minutes and is not stored. People are not created
here: the operators list has no users. A person whose own access was
removed is still refused on their next sign-in.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from pymongo.errors import DuplicateKeyError

from config.settings import settings
from database.session import get_database
from services.sso import _slugify

logger = logging.getLogger(__name__)

_token_endpoint: str | None = None


async def _discover_token_endpoint() -> str:
    global _token_endpoint
    if _token_endpoint:
        return _token_endpoint
    issuer = (settings.SELFBRIEF_ISSUER or "").rstrip("/")
    if not issuer:
        raise RuntimeError("SELFBRIEF_ISSUER is not set")
    url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        endpoint = response.json().get("token_endpoint")
    if not endpoint:
        raise RuntimeError("SelfBrief discovery has no token_endpoint")
    _token_endpoint = str(endpoint)
    return _token_endpoint


async def _application_token() -> str:
    endpoint = await _discover_token_endpoint()
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "scope": "operators:read",
                "client_id": settings.SELFBRIEF_CLIENT_ID or "",
                "client_secret": settings.SELFBRIEF_CLIENT_SECRET or "",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"SelfBrief token request failed ({response.status_code})")
    body = response.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError("SelfBrief token response had no access_token")
    return str(token)


async def _fetch_operators(token: str) -> list[dict]:
    base = (settings.SELFBRIEF_BASE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("SELFBRIEF_BASE_URL is not set")
    url = f"{base}/api/v1/sso/operators/"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"SelfBrief operators request failed ({response.status_code})")
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("SelfBrief operators response was not a list")
    return [item for item in payload if isinstance(item, dict) and item.get("id") is not None]


def _operator_name(operator: dict) -> str:
    name = str(operator.get("registered_name") or operator.get("name") or "").strip()
    return name or "Organisation"


async def _create_operator(db, cms_id: str, name: str, now: datetime) -> None:
    slug = _slugify(name)
    if slug == settings.INTERNAL_ORG_SLUG:
        slug = f"{slug}-operator"
    doc = {
        "name": name,
        "slug": slug,
        "cms_organisation_id": cms_id,
        "is_active": True,
        "templates": [],
        "created_at": now,
        "created_by_user_id": None,
    }
    try:
        await db["organizations"].insert_one(doc)
        return
    except DuplicateKeyError:
        pass
    already = await db["organizations"].find_one({"cms_organisation_id": cms_id})
    if already:
        return
    doc["slug"] = f"{slug}-{cms_id}"
    try:
        await db["organizations"].insert_one(doc)
    except DuplicateKeyError:
        logger.info("Operator %s was stored by another run", cms_id)


async def sync_operators_once() -> None:
    if not settings.SELFBRIEF_CLIENT_ID or not settings.SELFBRIEF_CLIENT_SECRET:
        logger.info("Operator sync skipped: SelfBrief client credentials are not set")
        return

    # Imported here so starting the API does not cycle through the routers.
    from api.v1.organizations import _set_org_active

    operators = await _fetch_operators(await _application_token())
    seen = {str(item["id"]) for item in operators}
    if not seen:
        logger.warning("SelfBrief returned no operators. CMS-linked organisations will be archived.")

    db = await get_database()
    now = datetime.now(timezone.utc)
    created = 0
    restored = 0
    renamed = 0
    archived = 0

    for operator in operators:
        cms_id = str(operator["id"])
        name = _operator_name(operator)
        existing = await db["organizations"].find_one({"cms_organisation_id": cms_id})
        if not existing:
            await _create_operator(db, cms_id, name, now)
            created += 1
            continue
        if existing.get("slug") == settings.INTERNAL_ORG_SLUG:
            continue
        if existing.get("name") != name:
            await db["organizations"].update_one(
                {"_id": existing["_id"]},
                {"$set": {"name": name}},
            )
            renamed += 1
        if not existing.get("is_active", True):
            await _set_org_active(str(existing["_id"]), True)
            restored += 1

    linked = await db["organizations"].find(
        {
            "cms_organisation_id": {"$type": "string", "$ne": ""},
            "slug": {"$ne": settings.INTERNAL_ORG_SLUG},
            "is_active": {"$ne": False},
        },
        {"cms_organisation_id": 1},
    ).to_list(length=500)
    for doc in linked:
        cms_id = str(doc.get("cms_organisation_id") or "")
        if not cms_id or cms_id in seen:
            continue
        await _set_org_active(str(doc["_id"]), False)
        archived += 1

    logger.info(
        "Operator sync finished (operators=%s, created=%s, restored=%s, renamed=%s, archived=%s)",
        len(seen),
        created,
        restored,
        renamed,
        archived,
    )


async def operator_sync_loop(stop_event: asyncio.Event) -> None:
    interval = max(60, settings.OPERATOR_SYNC_INTERVAL_SECONDS)
    logger.info("Operator sync started (every %ss)", interval)
    while not stop_event.is_set():
        try:
            await sync_operators_once()
        except Exception as exc:
            logger.error("Operator sync failed: %s", exc)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("Operator sync stopped")
