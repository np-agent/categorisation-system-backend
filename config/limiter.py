from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request


def get_remote_address(request: Request):
    return get_remote_address(request)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address) 