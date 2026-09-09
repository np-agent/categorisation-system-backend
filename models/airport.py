from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class AipVersion(BaseModel):
    """A single historical version of an airport's AIP document."""
    s3_key: str
    page_count: Optional[int]
    uploaded_at: datetime


class AirportCreate(BaseModel):
    icao_code: str
    iata_code: Optional[str] = None
    name: str
    city: Optional[str] = None
    country: Optional[str] = None
    s3_key: Optional[str] = None
    aip_available: bool = False
    page_count: Optional[int] = None


class AirportUpdate(BaseModel):
    name: Optional[str] = None
    iata_code: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    s3_key: Optional[str] = None
    aip_available: Optional[bool] = None
    page_count: Optional[int] = None


class AirportOut(BaseModel):
    id: str
    icao_code: str
    iata_code: Optional[str]
    name: str
    city: Optional[str]
    country: Optional[str]
    s3_key: Optional[str]
    aip_available: bool
    page_count: Optional[int]
    aip_versions: list[AipVersion]
    last_updated: Optional[datetime]

    class Config:
        populate_by_name = True


def serialize_airport(doc: dict) -> AirportOut:
    versions = [
        AipVersion(
            s3_key=v["s3_key"],
            page_count=v.get("page_count"),
            uploaded_at=v["uploaded_at"],
        )
        for v in doc.get("aip_versions", [])
    ]
    return AirportOut(
        id=str(doc["_id"]),
        icao_code=doc["icao_code"],
        iata_code=doc.get("iata_code"),
        name=doc["name"],
        city=doc.get("city"),
        country=doc.get("country"),
        s3_key=doc.get("s3_key"),
        aip_available=doc.get("aip_available", False),
        page_count=doc.get("page_count"),
        aip_versions=versions,
        last_updated=doc.get("last_updated"),
    )
