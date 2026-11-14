from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import ReturnDocument

from api.deps import build_user_map, get_current_db_user
from config.settings import settings
from database.session import get_database
from models.prompt_template import (
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptTemplateSummary,
    PromptTemplateUpdate,
    normalise_template_structure,
    serialize_template,
    serialize_template_summary,
)

router = APIRouter()


async def _remove_template_from_all_orgs(db, template_oid: ObjectId) -> None:
    """Pull this template ID from every organisation's templates list."""
    await db["organizations"].update_many(
        {"templates": template_oid},
        {"$pull": {"templates": template_oid}},
    )


async def _assign_template_to_internal_org(db, template_oid: ObjectId) -> None:
    """SelfBrief always has every active template available for job creation."""
    await db["organizations"].update_one(
        {"slug": settings.INTERNAL_ORG_SLUG},
        {"$addToSet": {"templates": template_oid}},
    )


@router.get("", response_model=list[PromptTemplateOut])
async def list_templates(
    status: str = Query(default="all", pattern="^(all|active|inactive)$"),
    _caller: dict = Depends(get_current_db_user),
):
    """
    List templates for the manage-templates screen.
    status: all | active | inactive
    """
    db = await get_database()
    if status == "active":
        query = {"is_active": True}
    elif status == "inactive":
        query = {"is_active": False}
    else:
        query = {}
    cursor = db["prompt_templates"].find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    user_map = await build_user_map(db, [d.get("created_by_user_id") for d in docs])
    return [serialize_template(d, user_map=user_map) for d in docs]


@router.get("/summary", response_model=list[PromptTemplateSummary])
async def list_templates_summary(
    _caller: dict = Depends(get_current_db_user),
):
    """Lightweight list for dropdowns — active templates only, no content."""
    db = await get_database()
    cursor = db["prompt_templates"].find(
        {"is_active": True},
        {"content": 0},
    ).sort("name", 1)
    docs = await cursor.to_list(length=200)
    return [serialize_template_summary(d) for d in docs]


@router.get("/{template_id}", response_model=PromptTemplateOut)
async def get_template(
    template_id: str,
    _caller: dict = Depends(get_current_db_user),
):
    db = await get_database()
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status_code=400, detail="Invalid template ID")
    doc = await db["prompt_templates"].find_one({"_id": ObjectId(template_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Template not found")
    user_map = await build_user_map(db, [doc.get("created_by_user_id")])
    return serialize_template(doc, user_map=user_map)


@router.post("", response_model=PromptTemplateOut, status_code=201)
async def create_template(
    data: PromptTemplateCreate,
    caller: dict = Depends(get_current_db_user),
):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    try:
        structured = normalise_template_structure(
            data.notes,
            [item.model_dump() for item in data.categories],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = await get_database()
    now = datetime.now(timezone.utc)
    doc = {
        "name": name,
        "category": data.category,
        "description": (data.description or "").strip() or None,
        "notes": structured["notes"],
        "categories": structured["categories"],
        "content": structured["content"],
        "is_active": True,
        "created_by_user_id": caller["_id"],
        "created_at": now,
        "updated_at": now,
    }
    result = await db["prompt_templates"].insert_one(doc)
    created = await db["prompt_templates"].find_one({"_id": result.inserted_id})
    await _assign_template_to_internal_org(db, result.inserted_id)
    user_map = await build_user_map(db, [created.get("created_by_user_id")])
    return serialize_template(created, user_map=user_map)


@router.patch("/{template_id}", response_model=PromptTemplateOut)
async def update_template(
    template_id: str,
    data: PromptTemplateUpdate,
    _caller: dict = Depends(get_current_db_user),
):
    db = await get_database()
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status_code=400, detail="Invalid template ID")

    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "name" in updates:
        updates["name"] = (updates["name"] or "").strip()
        if not updates["name"]:
            raise HTTPException(status_code=400, detail="Template name is required")

    template_oid = ObjectId(template_id)
    existing = await db["prompt_templates"].find_one({"_id": template_oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")

    if "categories" in updates or "notes" in updates:
        notes = updates["notes"] if "notes" in updates else existing.get("notes") or ""
        if "categories" in updates:
            raw_categories = updates["categories"] or []
        else:
            raw_categories = existing.get("categories") or []
        if raw_categories:
            try:
                structured = normalise_template_structure(notes, raw_categories)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            updates["notes"] = structured["notes"]
            updates["categories"] = structured["categories"]
            updates["content"] = structured["content"]
        elif "notes" in updates:
            updates["notes"] = (notes or "").strip()

    updates["updated_at"] = datetime.now(timezone.utc)
    doc = await db["prompt_templates"].find_one_and_update(
        {"_id": template_oid},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Template not found")

    if updates.get("is_active") is False:
        await _remove_template_from_all_orgs(db, template_oid)
    elif updates.get("is_active") is True:
        await _assign_template_to_internal_org(db, template_oid)

    user_map = await build_user_map(db, [doc.get("created_by_user_id")])
    return serialize_template(doc, user_map=user_map)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    _caller: dict = Depends(get_current_db_user),
):
    """Soft delete — sets is_active false and removes from all organisations."""
    db = await get_database()
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status_code=400, detail="Invalid template ID")

    template_oid = ObjectId(template_id)
    result = await db["prompt_templates"].update_one(
        {"_id": template_oid},
        {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")

    await _remove_template_from_all_orgs(db, template_oid)
