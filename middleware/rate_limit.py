from fastapi import Request
from supertokens_python.recipe.session.framework.fastapi import verify_session
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi.util import get_remote_address


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            session = await verify_session()(request)
            request.state.user_id = session.get_user_id()
        except Exception:
            address = get_remote_address(request)
            request.state.user_id = str(address)
        return await call_next(request)