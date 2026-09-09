from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import ReturnDocument

from api.deps import build_user_map, get_current_db_user
from database.session import get_database
from models.job import (
    JobCreate,
    JobOut,
    JobSummary,
    caller_acknowledged_job,
    serialize_job,
    serialize_job_summary,
)
from services.job_processor import (
    notify_job,
    submit_and_update_job,
    process_running_job,
)

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

    If the airport has aip_available=True, the job is submitted to the Anthropic
    batch API immediately and status is set to "running".

    If aip_available=False, the job is created with status "awaiting_aip" and
    nothing is sent to Anthropic until a super-admin triggers it after AIP upload.
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
        "batch_id": None,
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

    if not airport.get("aip_available"):
        job_doc["status"] = "awaiting_aip"
        job_doc["aip_unavailable_at"] = now
        result = await db["jobs"].insert_one(job_doc)
        created = await db["jobs"].find_one({"_id": result.inserted_id})
        await notify_job(db, created, "created")
        return serialize_job(created, caller)

    result = await db["jobs"].insert_one(job_doc)
    job_id = result.inserted_id

    try:
        await submit_and_update_job(db, job_id, airport, template)
    except Exception as e:
        now_fail = datetime.now(timezone.utc)
        await db["jobs"].update_one(
            {"_id": job_id},
            {"$set": {
                "status": "failed",
                "updated_at": now_fail,
                "completed_at": now_fail,
                "final_result": {
                    "raw_text": "",
                    "final_category": None,
                    "confidence_level": None,
                    "error": str(e),
                    "created_at": now_fail,
                },
            }},
        )

    created = await db["jobs"].find_one({"_id": job_id})
    await notify_job(db, created, "created")
    return serialize_job(created, caller)


@router.get("/{job_id}/status", response_model=JobOut)
async def check_job_status(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    """
    Optional manual poll endpoint. The background poller handles this in normal use.
    Kept for debugging / ops.
    """
    db = await get_database()
    doc = await _load_visible_job(db, job_id, caller)

    if doc["status"] not in ("running", "pending"):
        return serialize_job(doc, caller)

    if not doc.get("batch_id"):
        raise HTTPException(status_code=400, detail="Job has no batch ID to poll")

    updated = await process_running_job(db, doc)
    return serialize_job(updated or doc, caller)


@router.post("/{job_id}/trigger", response_model=JobOut)
async def trigger_job(
    job_id: str,
    caller: dict = Depends(get_current_db_user),
):
    """
    Super-admin only: submit an awaiting_aip job once the AIP has been uploaded.
    Same job ID — status moves to running.
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
    if not airport or not airport.get("aip_available"):
        raise HTTPException(
            status_code=400,
            detail="AIP document still not available for this airport",
        )

    template = await db["prompt_templates"].find_one(
        {"_id": doc["template_id"], "is_active": True}
    )
    if not template:
        raise HTTPException(status_code=404, detail="Template no longer available")

    try:
        await submit_and_update_job(db, ObjectId(job_id), airport, template)
        await db["jobs"].update_one(
            {"_id": ObjectId(job_id)},
            {"$unset": {"aip_unavailable_at": ""}},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to submit batch job: {e}")

    updated = await db["jobs"].find_one({"_id": ObjectId(job_id)})
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
