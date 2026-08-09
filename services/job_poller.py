"""
Background poller for running categorisation jobs.

Every POLL_INTERVAL_SECONDS, finds jobs with status=running and a batch_id,
checks Anthropic, and finalizes ended/failed jobs (including synthesis + email).
"""

import asyncio
import logging

from config.settings import settings
from database.session import get_database
from services.job_processor import process_running_job

logger = logging.getLogger(__name__)


async def poll_running_jobs_once() -> None:
    db = await get_database()
    cursor = db["jobs"].find({
        "status": "running",
        "batch_id": {"$ne": None},
    }).limit(100)
    docs = await cursor.to_list(length=100)

    if not docs:
        return

    logger.info("Polling %d running job(s)", len(docs))
    for doc in docs:
        try:
            updated = await process_running_job(db, doc)
            if updated:
                logger.info(
                    "Job %s settled -> %s",
                    doc["_id"],
                    updated.get("status"),
                )
        except Exception as exc:
            logger.error("Error processing job %s: %s", doc["_id"], exc)


async def job_poller_loop(stop_event: asyncio.Event) -> None:
    interval = max(15, settings.JOB_POLL_INTERVAL_SECONDS)
    logger.info("Job poller started (interval=%ss)", interval)

    # Small delay so the app finishes starting before the first pass
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            await poll_running_jobs_once()
        except Exception as exc:
            logger.error("Job poller cycle failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            continue

    logger.info("Job poller stopped")
