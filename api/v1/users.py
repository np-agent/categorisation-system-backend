from fastapi import APIRouter, Depends, HTTPException, Request
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from database.session import get_database
from models.user import UserCreate, UserOut
from config.limiter import limiter

router = APIRouter()


def serialize_user(user: dict) -> UserOut:
    return UserOut(id=str(user["_id"]), name=user["name"], email=user["email"])

def get_user_id_key(request: Request):
    user_id = getattr(request.state, "user_id", "anonymous")
    print(f"[RateLimit] user_id = {user_id}")
    return user_id

@router.get("", response_model=list[UserOut])
@limiter.limit("15/minute", key_func=get_user_id_key)
async def list_users(request: Request):
    """Test endpoint: returns every user stored in MongoDB."""
    db = await get_database()
    users = await db.users.find().to_list(length=100)
    return [serialize_user(user) for user in users]


@router.post("", response_model=UserOut)
async def create_user(payload: UserCreate):
    """Test endpoint: inserts a user document into MongoDB."""
    db = await get_database()

    existing = await db.users.find_one({"email": payload.email})
    if existing:
        raise HTTPException(status_code=400, detail="User with this email already exists")

    result = await db.users.insert_one(payload.model_dump())
    created = await db.users.find_one({"_id": result.inserted_id})
    return serialize_user(created)


@router.get("/me")
async def get_me(session: SessionContainer = Depends(verify_session())):
    """Test endpoint: confirms SuperTokens session verification is working."""
    return {"user_id": session.get_user_id()}
