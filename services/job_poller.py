"""
Background worker for categorisation jobs.

Jobs wait in Mongo with status pending. A few run at a time as direct model calls.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from config.settings import settings
from database.session import get_database
from models.job import result_audit_entry
from services.job_processor import job_queued, run_queued_job

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=8)
_tasks: set[asyncio.Task] = set()


async def _recover_stale(db) -> None:
    """A job left running after a restart goes back to the queue, once."""
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    stale = await db["jobs"].find(
        {
            "status": "running",
            "updated_at": {"$lt": cutoff},
        }
    ).to_list(length=50)
    now = datetime.now(timezone.utc)
    for doc in stale:
        attempts = int(doc.get("processing_attempts") or 1)
        if attempts >= 2:
            await db["jobs"].update_one(
                {"_id": doc["_id"], "status": "running"},
                {"$set": {
                    "status": "failed",
                    "completed_at": now,
                    "updated_at": now,
                    "final_result": {
                        "raw_text": "",
                        "final_category": None,
                        "confidence_level": None,
                        "error": "The job was interrupted and did not finish on a second attempt",
                        "answers": [],
                        "created_at": now,
                    },
                },
                "$push": {"result_audit": result_audit_entry({
                    "raw_text": "",
                    "final_category": None,
                    "confidence_level": None,
                    "error": "The job was interrupted and did not finish on a second attempt",
                    "answers": [],
                    "created_at": now,
                }, saved_at=now)}},
            )
            continue
        await db["jobs"].update_one(
            {"_id": doc["_id"], "status": "running"},
            {"$set": {"status": "pending", "updated_at": now}},
        )
        job_queued.set()


async def _claim_one(db) -> dict | None:
    now = datetime.now(timezone.utc)
    return await db["jobs"].find_one_and_update(
        {
            "status": "pending",
            "airport_aip_s3_key": {"$nin": [None, ""]},
        },
        {
            "$set": {"status": "running", "updated_at": now},
            "$inc": {"processing_attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _running_here() -> int:
    return sum(1 for task in _tasks if not task.done())


async def _start_queued(db) -> None:
    limit = max(1, settings.JOB_MAX_CONCURRENT)
    while _running_here() < limit:
        doc = await _claim_one(db)
        if not doc:
            return
        task = asyncio.create_task(_run(db, doc))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _run(db, doc: dict) -> None:
    try:
        await run_queued_job(db, doc)
    except Exception as exc:
        logger.error("Queued job %s crashed: %s", doc.get("_id"), exc)
    job_queued.set()


async def poll_running_jobs_once() -> None:
    db = await get_database()
    await _recover_stale(db)
    await _start_queued(db)


async def job_poller_loop(stop_event: asyncio.Event) -> None:
    interval = max(15, settings.JOB_POLL_INTERVAL_SECONDS)
    logger.info(
        "Job worker started (sweep=%ss, concurrent=%s)",
        interval,
        settings.JOB_MAX_CONCURRENT,
    )

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            await poll_running_jobs_once()
        except Exception as exc:
            logger.error("Job worker cycle failed: %s", exc)

        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(job_queued.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        job_queued.clear()

    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
    logger.info("Job worker stopped")
