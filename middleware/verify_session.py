from fastapi import Depends, Request
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session
from config.settings import settings
from typing import Optional


def get_session_dependency():
    return Depends(verify_session()) if settings.ENV != "dev" else None


async def verify_and_log_session(request: Request):
    if settings.ENV == "dev" or settings.ENV == "local" or settings.ENV == "aws-dev":
        request.state.user_id = "dev-user"
        print("[Auth] Dev mode — skipping session verification")
        return None

    session: SessionContainer = await verify_session()(request)
    user_id = session.get_user_id()
    request.state.user_id = user_id
    print(f"[Auth] Request by user: {user_id}")
    return session

# async def verify_and_log_session(request: Request):
#     user_id = getattr(request.state, "user_id", "anonymous")
#     print(f"[Auth] Request by user: {user_id}")
#     return user_id
