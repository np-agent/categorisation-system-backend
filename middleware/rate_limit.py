import hashlib

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi.util import get_remote_address


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer ") and authorization.split(" ", 1)[1].strip():
            token = authorization.split(" ", 1)[1].strip()
            request.state.user_id = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        else:
            request.state.user_id = str(get_remote_address(request))
        return await call_next(request)
