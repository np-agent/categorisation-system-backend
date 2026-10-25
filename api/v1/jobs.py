from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import ReturnDocument

from api.deps import build_user_map, get_current_db_user
from database.session import get_database
from models.job import (
    JobCreate,
    JobOut,
    JobReviewUpdate,
    JobSummary,
    caller_acknowledged_job,
    serialize_job,
    serialize_job_summary,
)
from services.job_processor import notify_job, notify_job_queued

router = APIRouter()


@router.get("", response_model=list[JobSummary])
async def list_jobs(
    caller: dict = Depends(get_current_db_user),
    status: str | None = Query(default=None),
    airport_icao: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
):
    """
    Return jobs scoped by the caller's role:
      - super-admin : all jobs across all organisations
      - admin/user  : every job in their organisation
    Optional filters: status, airport_icao
    """
    db = await get_database()
    role = caller.get("role", "user")

    query: dict = {}
    if role != "super-admin":
        query["organization_id"] = caller["organization_id"]

    if status:
        query["status"] = status
    if airport_icao:
        query["airport_icao"] = airport_icao.upper()

    cursor = db["jobs"].find(query).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)

    creator_ids = [d.get("created_by_user_id") for d in docs if d.get("created_by_user_id")]
    user_map = await build_user_map(db, creator_ids)

    org_ids = list({d["organization_id"] for d in docs})
    org_docs = await db["organizations"].find(
        {"_id": {"$in": org_ids}}
    ).to_list(length=200)
    org_map = {str(o["_id"]): o["name"] for o in org_docs}

    return [serialize_job_summary(d, user_map=user_map, org_map=org_map) for d in docs]


def _job_visible_to(doc: dict, caller: dict) -> bool:
    if caller.get("role") == "super-admin":
        return True
    return str(doc.get("organization_id")) == str(caller.get("organization_id"))


async def _load_visible_job(db, job_id: str, caller: dict) -> dict:
    if not ObjectId.is_valid(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    doc = await db["jobs"].find_one({"_id": ObjectId(job_id)})
    if not doc or not _job_visible_to(doc, caller):
        raise HTTPException(status_code=404, detail="Job not found")
    return doc


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    db = await get_database()
    doc = await _load_visible_job(db, job_id, caller)
    return serialize_job(doc, caller)


@router.post("", response_model=JobOut, status_code=201)
async def create_job(
    data: JobCreate,
    caller: dict = Depends(get_current_db_user),
):
    """
    Create a new categorisation job for the authenticated user's organisation.

    If the airport has an AIP, the job waits in the queue and a worker runs it.
    Status stays "pending" until a worker claims it, then "running".

    If the AIP is missing, the job is created with status "awaiting_aip" and
    nothing is sent until a super-admin triggers it after the AIP is uploaded.
    """
    db = await get_database()

    if not ObjectId.is_valid(data.template_id):
        raise HTTPException(status_code=400, detail="Invalid template ID")

    airport = await db["airports"].find_one({"icao_code": data.airport_icao.upper()})
    if not airport:
        raise HTTPException(status_code=404, detail=f"Airport '{data.airport_icao}' not found")

    template = await db["prompt_templates"].find_one({"_id": ObjectId(data.template_id), "is_active": True})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found or inactive")

    now = datetime.now(timezone.utc)
    job_doc = {
        "title": data.title,
        "organization_id": caller["organization_id"],
        "created_by_user_id": caller["_id"],
        "airport_id": airport["_id"],
        "airport_icao": airport["icao_code"],
        "airport_name": airport["name"],
        "template_id": template["_id"],
        "template_name": template["name"],
        "status": "pending",
        "airport_aip_s3_key": airport.get("s3_key"),
        "submitted_at": None,
        "completed_at": None,
        "chunk_count": 0,
        "chunks": [],
        "synthesis": None,
        "final_result": None,
        "created_at": now,
        "updated_at": now,
    }

    if not airport.get("aip_available") or not airport.get("s3_key"):
        job_doc["status"] = "awaiting_aip"
        job_doc["aip_unavailable_at"] = now

    result = await db["jobs"].insert_one(job_doc)
    created = await db["jobs"].find_one({"_id": result.inserted_id})
    await notify_job(db, created, "created")
    if created.get("status") == "pending":
        notify_job_queued()
    return serialize_job(created, caller)


@router.get("/{job_id}/status", response_model=JobOut)
async def check_job_status(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    """Return the job as stored. The worker updates status; this does not call the model."""
    db = await get_database()
    doc = await _load_visible_job(db, job_id, caller)
    return serialize_job(doc, caller)


@router.post("/{job_id}/trigger", response_model=JobOut)
async def trigger_job(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    """
    Super-admin only: queue an awaiting_aip job once the AIP has been uploaded.
    Same job ID. Status moves to pending until a worker claims it.
    """
    if caller.get("role") != "super-admin":
        raise HTTPException(status_code=403, detail="Only super-admins can trigger jobs")

    db = await get_database()
    if not ObjectId.is_valid(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    doc = await db["jobs"].find_one({"_id": ObjectId(job_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")
    if doc["status"] != "awaiting_aip":
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be triggered from status '{doc['status']}'",
        )

    airport = await db["airports"].find_one({"icao_code": doc["airport_icao"]})
    if not airport or not airport.get("aip_available") or not airport.get("s3_key"):
        raise HTTPException(
            status_code=400,
            detail="AIP document still not available for this airport",
        )

    template = await db["prompt_templates"].find_one(
        {"_id": doc["template_id"], "is_active": True}
    )
    if not template:
        raise HTTPException(status_code=404, detail="Template no longer available")

    now = datetime.now(timezone.utc)
    updated = await db["jobs"].find_one_and_update(
        {"_id": ObjectId(job_id), "status": "awaiting_aip"},
        {
            "$set": {
                "status": "pending",
                "airport_aip_s3_key": airport["s3_key"],
                "updated_at": now,
            },
            "$unset": {"aip_unavailable_at": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=400, detail="Job cannot be triggered")
    notify_job_queued()
    return serialize_job(updated, caller)


@router.post("/{job_id}/advisory", response_model=JobOut)
async def acknowledge_job_advisory(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    """
    Record that this user accepted the advisory notice for this job.
    Later views of the same job by the same user skip the popup.
    """
    db = await get_database()
    doc = await _load_visible_job(db, job_id, caller)
    job_id_str = str(doc["_id"])

    if not caller_acknowledged_job(caller, job_id_str):
        caller = await db["users"].find_one_and_update(
            {"_id": caller["_id"]},
            {
                "$push": {
                    "advisory_job_acknowledgements": {
                        "job_id": job_id_str,
                        "accepted_at": datetime.now(timezone.utc),
                    }
                },
            },
            return_document=ReturnDocument.AFTER,
        ) or caller

    return serialize_job(doc, caller)


@router.patch("/{job_id}/review", response_model=JobOut)
async def save_job_review(
    job_id: str,
    data: JobReviewUpdate,
    caller: dict = Depends(get_current_db_user),
):
    """
    Save the reviewer's notes and final category.

    The first non-empty value is recorded as added. A later change is recorded
    as updated. Each change is appended to review_audit. An unchanged value
    does not add another audit row. Closing an ended job sets its status to
    closed and requires a final category.
    """
    db = await get_database()
    doc = await _load_visible_job(db, job_id, caller)
    if doc.get("status") not in ("ended", "closed") or not doc.get("final_result") or doc["final_result"].get("error"):
        raise HTTPException(status_code=400, detail="This job has no result to review")

    notes = data.notes.strip()
    category = data.final_category.strip()
    if len(notes) > 4000:
        raise HTTPException(status_code=400, detail="Notes are too long")
    if len(category) > 200:
        raise HTTPException(status_code=400, detail="Final category is too long")

    previous_notes = doc.get("reviewed_notes") or ""
    previous_category = doc.get("reviewed_category") or ""
    closing = bool(data.close) and doc.get("status") == "ended"
    if closing and not category:
        raise HTTPException(status_code=400, detail="Enter a final category before closing the job.")
    if notes == previous_notes and category == previous_category and not closing:
        return serialize_job(doc, caller)

    now = datetime.now(timezone.utc)
    user_name = (caller.get("full_name") or caller.get("email") or "").strip()
    audit = []
    for field, previous, value in (
        ("notes", previous_notes, notes),
        ("final_category", previous_category, category),
    ):
        if previous == value:
            continue
        audit.append({
            "field": field,
            "action": "added" if not previous and value else "updated",
            "previous_value": previous,
            "value": value,
            "user_id": str(caller["_id"]),
            "user_name": user_name,
            "at": now,
        })

    updated = await db["jobs"].find_one_and_update(
        {"_id": doc["_id"]},
        {
            "$set": {
                "reviewed_notes": notes,
                "reviewed_category": category,
                "updated_at": now,
                **({"status": "closed"} if closing else {}),
            },
            **({"$push": {"review_audit": {"$each": audit}}} if audit else {}),
        },
        return_document=ReturnDocument.AFTER,
    )
    return serialize_job(updated, caller)


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    db = await get_database()
    await _load_visible_job(db, job_id, caller)
    result = await db["jobs"].delete_one({"_id": ObjectId(job_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")
