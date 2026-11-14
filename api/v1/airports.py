import hashlib
import io
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pymongo import ReturnDocument
from pypdf import PdfReader

from api.deps import get_current_db_user
from config.settings import settings
from database.session import get_database
from models.airport import (
    AirportCreate,
    AirportOut,
    AirportUpdate,
    serialize_airport,
)

# Gate the whole router: every airport route requires an active user in an
# active organisation, so deactivation applies here too.
router = APIRouter(dependencies=[Depends(get_current_db_user)])


@router.get("", response_model=list[AirportOut])
async def list_airports(
    q: str = Query(default="", description="Search by ICAO code, name, or city"),
    limit: int = Query(default=25, le=500),
    skip: int = Query(default=0, ge=0),
):
    """
    List/search airports with optional pagination.
    Used by both the manage-airports admin table (paginated) and the
    job creation dropdown (skip=0, limit=100, q=<search>).
    """
    db = await get_database()

    query: dict = {}
    if q.strip():
        import re
        pattern = re.compile(re.escape(q.strip()), re.IGNORECASE)
        query = {
            "$or": [
                {"icao_code": pattern},
                {"iata_code": pattern},
                {"name": pattern},
                {"city": pattern},
            ]
        }

    cursor = db["airports"].find(query).sort("icao_code", 1).skip(skip).limit(limit)
    airports = await cursor.to_list(length=limit)
    return [serialize_airport(a) for a in airports]


@router.get("/count")
async def count_airports(q: str = Query(default="")):
    """Return total airport count for a given search query. Used for pagination."""
    db = await get_database()

    query: dict = {}
    if q.strip():
        import re
        pattern = re.compile(re.escape(q.strip()), re.IGNORECASE)
        query = {
            "$or": [
                {"icao_code": pattern},
                {"iata_code": pattern},
                {"name": pattern},
                {"city": pattern},
            ]
        }

    total = await db["airports"].count_documents(query)
    return {"total": total}


@router.get("/{icao_code}", response_model=AirportOut)
async def get_airport(icao_code: str):
    db = await get_database()
    doc = await db["airports"].find_one({"icao_code": icao_code.upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="Airport not found")
    return serialize_airport(doc)


@router.post("", response_model=AirportOut, status_code=201)
async def create_airport(data: AirportCreate):
    db = await get_database()
    existing = await db["airports"].find_one({"icao_code": data.icao_code.upper()})
    if existing:
        raise HTTPException(status_code=400, detail="Airport with this ICAO code already exists")

    now = datetime.now(timezone.utc)
    doc = {
        **data.model_dump(),
        "icao_code": data.icao_code.upper(),
        "aip_versions": [],
        "last_updated": now,
    }
    result = await db["airports"].insert_one(doc)
    created = await db["airports"].find_one({"_id": result.inserted_id})
    return serialize_airport(created)


@router.patch("/{icao_code}", response_model=AirportOut)
async def update_airport(icao_code: str, data: AirportUpdate):
    db = await get_database()
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["last_updated"] = datetime.now(timezone.utc)
    doc = await db["airports"].find_one_and_update(
        {"icao_code": icao_code.upper()},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Airport not found")
    return serialize_airport(doc)


@router.post("/upload-aip", response_model=AirportOut, status_code=200)
async def upload_aip(
    icao_code: str = Form(...),
    airport_name: str = Form(...),
    iata_code: str = Form(default=""),
    city: str = Form(default=""),
    country: str = Form(default=""),
    file: UploadFile = File(...),
):
    """
    Upload an AIP PDF for an airport.

    - Reads the PDF, counts pages, and computes a SHA-256 content hash.
    - Uploads to S3 with key: {ICAO}.pdf (overwrites any previous version).
    - Creates or updates the airport record in MongoDB.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # SHA-256 hash of the file contents
    file_hash = hashlib.sha256(contents).hexdigest()

    # Count pages using pypdf
    try:
        reader = PdfReader(io.BytesIO(contents))
        page_count = len(reader.pages)
    except Exception:
        page_count = None

    icao = icao_code.strip().upper()
    s3_key = f"{file_hash}.pdf"

    db = await get_database()
    existing = await db["airports"].find_one({"icao_code": icao})

    # Deduplication: same content hash means this PDF is already the active version
    if existing and existing.get("s3_key") == s3_key:
        raise HTTPException(
            status_code=409,
            detail=f"This PDF is already the current AIP for {icao}. No changes were made.",
        )

    # Upload to S3 — key is the SHA-256 hash, so each unique file content has its own object
    try:
        s3 = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Body=contents,
            ContentType="application/pdf",
            Metadata={"icao_code": icao},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"S3 upload failed: {e}")

    now = datetime.now(timezone.utc)

    set_fields: dict = {
        "s3_key": s3_key,
        "aip_available": True,
        "page_count": page_count,
        "last_updated": now,
    }
    if airport_name.strip():
        set_fields["name"] = airport_name.strip()
    if iata_code.strip():
        set_fields["iata_code"] = iata_code.strip().upper()
    if city.strip():
        set_fields["city"] = city.strip()
    if country.strip():
        set_fields["country"] = country.strip()

    if existing:
        # Update: archive the current version before replacing it.
        # $set and $push on the same field in one op causes a MongoDB conflict,
        # so we split: $push the old version, then $set the new fields.
        mongo_op: dict = {"$set": set_fields}
        if existing.get("s3_key"):
            old_version = {
                "s3_key": existing["s3_key"],
                "page_count": existing.get("page_count"),
                "uploaded_at": existing.get("last_updated", now),
            }
            mongo_op["$push"] = {"aip_versions": old_version}

        doc = await db["airports"].find_one_and_update(
            {"icao_code": icao},
            mongo_op,
            return_document=ReturnDocument.AFTER,
        )
    else:
        # Insert: new airport, empty version history
        new_doc = {
            **set_fields,
            "icao_code": icao,
            "aip_versions": [],
            "created_at": now,
        }
        result = await db["airports"].insert_one(new_doc)
        doc = await db["airports"].find_one({"_id": result.inserted_id})

    return serialize_airport(doc)
