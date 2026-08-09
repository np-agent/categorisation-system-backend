"""
Shared job submission / completion logic used by API routes and the background poller.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId

from services.categorisation import (
    submit_batch_job,
    fetch_batch_result,
    merge_chunk_outcomes,
    run_synthesis,
)
from services.email import (
    send_job_created_email,
    send_job_completed_email,
    send_job_failed_email,
)

logger = logging.getLogger(__name__)


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
            # Legacy jobs stored the SuperTokens user id
            creator = await db["users"].find_one({"supertokens_user_id": str(creator_ref)})
        if not creator or not creator.get("email"):
            return
        to_email = creator["email"]
        title = job_doc.get("title", "")
        if event == "created":
            send_job_created_email(to_email, title)
        elif event == "ended":
            send_job_completed_email(to_email, title)
        elif event == "failed":
            send_job_failed_email(to_email, title)
    except Exception as exc:
        logger.error("notify_job error (%s): %s", event, exc)


async def submit_and_update_job(db, job_id: ObjectId, airport: dict, template: dict) -> None:
    """Submit batch to Anthropic and update the job document. Raises on failure."""
    submission = await submit_batch_job(
        airport_s3_key=airport["s3_key"],
        template_content=template["content"],
        job_id=str(job_id),
    )
    await db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": "running",
            "batch_id": submission["batch_id"],
            "chunk_count": submission["chunk_count"],
            "chunks": submission["chunks"],
            "airport_aip_s3_key": airport.get("s3_key"),
            "submitted_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def process_running_job(db, doc: dict) -> dict | None:
    """
    Poll Anthropic for one running job and finalize it if the batch has ended.

    Returns:
        None  — still in progress (or no batch_id / not processable)
        dict  — updated job document after settling to ended/failed
    """
    if doc.get("status") not in ("running", "pending"):
        return None
    if not doc.get("batch_id"):
        return None

    job_id = doc["_id"]

    try:
        batch_outcome = await fetch_batch_result(doc["batch_id"])
    except Exception as exc:
        logger.error("Failed to fetch batch %s for job %s: %s", doc["batch_id"], job_id, exc)
        return None

    if batch_outcome is None:
        return None

    now = datetime.now(timezone.utc)

    if batch_outcome.get("batch_error"):
        final_result = {
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": batch_outcome["batch_error"],
            "created_at": now,
        }
        await db["jobs"].update_one(
            {"_id": job_id},
            {"$set": {
                "status": "failed",
                "completed_at": now,
                "updated_at": now,
                "final_result": final_result,
            }},
        )
        updated = await db["jobs"].find_one({"_id": job_id})
        await notify_job(db, updated, "failed")
        return updated

    chunks = merge_chunk_outcomes(
        doc.get("chunks") or [],
        batch_outcome.get("chunks") or [],
    )

    any_failed = any(c.get("status") != "succeeded" for c in chunks)
    if any_failed:
        errors = [c.get("error") for c in chunks if c.get("error")]
        final_result = {
            "raw_text": "",
            "final_category": None,
            "confidence_level": None,
            "error": "; ".join(errors) if errors else "One or more chunks failed",
            "created_at": now,
        }
        await db["jobs"].update_one(
            {"_id": job_id},
            {"$set": {
                "status": "failed",
                "completed_at": now,
                "updated_at": now,
                "chunks": chunks,
                "final_result": final_result,
            }},
        )
        updated = await db["jobs"].find_one({"_id": job_id})
        await notify_job(db, updated, "failed")
        return updated

    synthesis = None
    if len(chunks) > 1:
        template = await db["prompt_templates"].find_one({"_id": doc["template_id"]})
        template_content = (template or {}).get("content", "")
        synthesis = await run_synthesis(template_content, chunks)
        if synthesis.get("error"):
            final_result = {
                "raw_text": "",
                "final_category": None,
                "confidence_level": None,
                "error": synthesis["error"],
                "created_at": now,
            }
            await db["jobs"].update_one(
                {"_id": job_id},
                {"$set": {
                    "status": "failed",
                    "completed_at": now,
                    "updated_at": now,
                    "chunks": chunks,
                    "synthesis": synthesis,
                    "final_result": final_result,
                }},
            )
            updated = await db["jobs"].find_one({"_id": job_id})
            await notify_job(db, updated, "failed")
            return updated

        final_result = {
            "raw_text": synthesis.get("raw_response", ""),
            "final_category": synthesis.get("final_category"),
            "confidence_level": synthesis.get("confidence_level"),
            "error": None,
            "created_at": now,
        }
    else:
        only = chunks[0]
        final_result = {
            "raw_text": only.get("raw_text", ""),
            "final_category": only.get("final_category"),
            "confidence_level": only.get("confidence_level"),
            "error": None,
            "created_at": now,
        }

    await db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": "ended",
            "completed_at": now,
            "updated_at": now,
            "chunks": chunks,
            "synthesis": synthesis,
            "final_result": final_result,
        }},
    )
    updated = await db["jobs"].find_one({"_id": job_id})
    await notify_job(db, updated, "ended")
    return updated
