"""Public legal documents (EULA)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.legal import current_eula_text, current_eula_version

router = APIRouter()


class EulaOut(BaseModel):
    version: str
    text: str


@router.get("/eula", response_model=EulaOut)
async def get_eula():
    try:
        return EulaOut(version=current_eula_version(), text=current_eula_text())
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="End User Licence Agreement is unavailable.")
