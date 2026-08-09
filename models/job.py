from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel


JobStatus = Literal["pending", "running", "ended", "failed", "awaiting_aip"]
ChunkStatus = Literal["succeeded", "errored", "expired", "canceled"]


class JobChunk(BaseModel):
    chunk_index: int
    page_start: int
    page_end: int
    custom_id: str
    status: Optional[ChunkStatus] = None
    raw_text: str = ""
    final_category: Optional[str] = None
    confidence_level: Optional[str] = None
    error: Optional[str] = None


class JobSynthesis(BaseModel):
    prompt_sent: str
    raw_response: str
    final_category: Optional[str] = None
    confidence_level: Optional[str] = None
    error: Optional[str] = None


class JobFinalResult(BaseModel):
    raw_text: str
    final_category: Optional[str] = None
    confidence_level: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime


class JobCreate(BaseModel):
    title: str
    airport_icao: str
    template_id: str


class JobOut(BaseModel):
    id: str
    title: str
    organization_id: str
    created_by_user_id: str
    airport_id: Optional[str]
    airport_icao: str
    airport_name: Optional[str]
    template_id: str
    template_name: Optional[str]
    status: JobStatus
    batch_id: Optional[str]
    airport_aip_s3_key: Optional[str]
    aip_unavailable_at: Optional[datetime]
    submitted_at: Optional[datetime]
    completed_at: Optional[datetime]
    chunk_count: int
    chunks: list[JobChunk]
    synthesis: Optional[JobSynthesis]
    final_result: Optional[JobFinalResult]
    created_at: datetime
    updated_at: datetime

    class Config:
        populate_by_name = True


class JobSummary(BaseModel):
    """Lightweight version for the jobs list table."""
    id: str
    title: str
    status: JobStatus
    organization_id: str
    organization_name: Optional[str]
    airport_icao: str
    airport_name: Optional[str]
    template_name: Optional[str]
    created_by_user_id: str
    created_by_email: Optional[str]
    batch_id: Optional[str]
    created_at: datetime
    updated_at: datetime


def _serialize_chunk(c: dict) -> JobChunk:
    return JobChunk(
        chunk_index=c.get("chunk_index", 0),
        page_start=c.get("page_start", 0),
        page_end=c.get("page_end", 0),
        custom_id=c.get("custom_id", ""),
        status=c.get("status"),
        raw_text=c.get("raw_text", ""),
        final_category=c.get("final_category"),
        confidence_level=c.get("confidence_level"),
        error=c.get("error"),
    )


def _serialize_synthesis(s: dict | None) -> JobSynthesis | None:
    if not s:
        return None
    return JobSynthesis(
        prompt_sent=s.get("prompt_sent", ""),
        raw_response=s.get("raw_response", ""),
        final_category=s.get("final_category"),
        confidence_level=s.get("confidence_level"),
        error=s.get("error"),
    )


def _legacy_final_result(doc: dict) -> JobFinalResult | None:
    """Map old `results` array jobs to final_result for backward compatibility."""
    results = doc.get("results") or []
    if not results:
        return None
    r = results[-1]
    return JobFinalResult(
        raw_text=r.get("raw_text", ""),
        final_category=r.get("final_category"),
        confidence_level=r.get("confidence_level"),
        error=r.get("error"),
        created_at=r.get("created_at") or doc.get("updated_at") or doc["created_at"],
    )


def serialize_job(doc: dict) -> JobOut:
    final = None
    if doc.get("final_result"):
        fr = doc["final_result"]
        final = JobFinalResult(
            raw_text=fr.get("raw_text", ""),
            final_category=fr.get("final_category"),
            confidence_level=fr.get("confidence_level"),
            error=fr.get("error"),
            created_at=fr.get("created_at") or doc.get("updated_at") or doc["created_at"],
        )
    else:
        final = _legacy_final_result(doc)

    chunks = [_serialize_chunk(c) for c in doc.get("chunks", [])]

    return JobOut(
        id=str(doc["_id"]),
        title=doc["title"],
        organization_id=str(doc["organization_id"]),
        created_by_user_id=str(doc["created_by_user_id"]) if doc.get("created_by_user_id") else "",
        airport_id=str(doc["airport_id"]) if doc.get("airport_id") else None,
        airport_icao=doc["airport_icao"],
        airport_name=doc.get("airport_name"),
        template_id=str(doc["template_id"]),
        template_name=doc.get("template_name"),
        status=doc["status"],
        batch_id=doc.get("batch_id"),
        airport_aip_s3_key=doc.get("airport_aip_s3_key"),
        aip_unavailable_at=doc.get("aip_unavailable_at"),
        submitted_at=doc.get("submitted_at"),
        completed_at=doc.get("completed_at"),
        chunk_count=doc.get("chunk_count") or len(chunks) or (1 if final else 0),
        chunks=chunks,
        synthesis=_serialize_synthesis(doc.get("synthesis")),
        final_result=final,
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def serialize_job_summary(
    doc: dict,
    user_map: dict | None = None,
    org_map: dict | None = None,
) -> JobSummary:
    creator_key = str(doc.get("created_by_user_id", "") or "")
    creator = (user_map or {}).get(creator_key)
    org_id = str(doc["organization_id"])
    return JobSummary(
        id=str(doc["_id"]),
        title=doc["title"],
        status=doc["status"],
        organization_id=org_id,
        organization_name=(org_map or {}).get(org_id),
        airport_icao=doc["airport_icao"],
        airport_name=doc.get("airport_name"),
        template_name=doc.get("template_name"),
        created_by_user_id=creator_key,
        created_by_email=creator.get("email") if creator else None,
        batch_id=doc.get("batch_id"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )
