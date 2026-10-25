"""
Shared job submission / completion logic used by API routes and the background poller.
"""

import asyncio
import logging
from datetime import datetime, timezone

from bson import ObjectId

from services.categorisation import categorise_document
from services.email import (
    send_job_created_email,
    send_job_completed_email,
    send_job_failed_email,
)

logger = logging.getLogger(__name__)

# Set when a job is queued so the worker starts it without waiting for the next sweep.
job_queued = asyncio.Event()


def notify_job_queued() -> None:
    job_queued.set()


async def notify_job(db, job_doc: dict, event: str) -> None:
    """Send a job status email to the creator. Fire-and-forget — never raises."""
    try:
        from bson import ObjectId

        creator_ref = job_doc.get("created_by_user_id")
        creator = None
        if isinstance(creator_ref, ObjectId) or (
            isinstance(creator_ref, str) and ObjectId.is_valid(creator_ref) and len(creator_ref) == 24
        ):
            creator = await db["users"].find_one({"_id": ObjectId(str(creator_ref))})
        if not creator and creator_ref:
            creator = await db["users"].find_one({"cms_user_id": str(creator_ref)})
        if not creator or not creator.get("email"):
            return
        to_email = creator["email"]
        title = job_doc.get("title", "")
        icao = job_doc.get("airport_icao")
        name = job_doc.get("airport_name")
        if event == "created":
            send_job_created_email(to_email, title, icao, name)
        elif event == "ended":
            send_job_completed_email(to_email, title, icao, name)
        elif event == "failed":
            send_job_failed_email(to_email, title, icao, name)
    except Exception as exc:
        logger.error("notify_job error (%s): %s", event, exc)


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("busy", "timeout", "timed out", "429", "503", "529"))


async def run_queued_job(db, doc: dict) -> None:
    """
    Run one job that has already been claimed (status running).

    A transport failure on the first attempt puts the job back in the queue.
    A bad result, or a second failure, marks it failed.
    """
    job_id = doc["_id"]
    now = datetime.now(timezone.utc)
    attempts = int(doc.get("processing_attempts") or 1)

    try:
        template = await db["prompt_templates"].find_one(
            {"_id": doc["template_id"], "is_active": True}
        )
        if not template:
            raise RuntimeError("Template no longer available")
        s3_key = doc.get("airport_aip_s3_key")
        if not s3_key:
            raise RuntimeError("AIP document is not available for this airport")

        org = None
        if doc.get("organization_id"):
            org = await db["organizations"].find_one({"_id": doc["organization_id"]})
        instructions = (org or {}).get("job_instructions") or ""
        await db["jobs"].update_one(
            {"_id": job_id, "status": "running"},
            {"$set": {
                "submitted_at": now,
                "updated_at": now,
                "organisation_instructions": instructions,
            }},
        )
        outcome = await categorise_document(
            s3_key,
            template,
            str(job_id),
            organisation_instructions=instructions,
        )
    except Exception as exc:
        logger.error("Job %s failed while running: %s", job_id, exc)
        if _is_transient(exc) and attempts < 2:
            await db["jobs"].update_one(
                {"_id": job_id, "status": "running"},
                {"$set": {"status": "pending", "updated_at": datetime.now(timezone.utc)}},
            )
            notify_job_queued()
            return
        await _fail_job(db, job_id, str(exc))
        return

    finished = datetime.now(timezone.utc)
    final_result = outcome["final_result"]
    final_result["created_at"] = finished
    failed = bool(final_result.get("error"))
    await db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": "failed" if failed else "ended",
            "completed_at": finished,
            "updated_at": finished,
            "chunk_count": len(outcome["chunks"]),
            "chunks": outcome["chunks"],
            "synthesis": outcome["synthesis"],
            "final_result": final_result,
        }},
    )
    updated = await db["jobs"].find_one({"_id": job_id})
    await notify_job(db, updated, "failed" if failed else "ended")


async def _fail_job(db, job_id, message: str) -> None:
    now = datetime.now(timezone.utc)
    await db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": "failed",
            "completed_at": now,
            "updated_at": now,
            "final_result": {
                "raw_text": "",
                "final_category": None,
                "confidence_level": None,
                "error": message[:500],
                "created_at": now,
            },
        }},
    )
    updated = await db["jobs"].find_one({"_id": job_id})
    if updated:
        await notify_job(db, updated, "failed")
