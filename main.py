import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

# Must be imported before the app is created so the SuperTokens SDK is initialized.
import config.supertoken_config  # noqa: F401,E402
from supertokens_python.framework.fastapi import get_middleware  # noqa: E402

from api.health import router as health_router  # noqa: E402
from api.v1.users import router as users_router  # noqa: E402
from config.cors import get_cors_origins  # noqa: E402
from config.settings import settings  # noqa: E402
from database.session import close_db, connect_db  # noqa: E402
from middleware.rate_limit import RateLimitMiddleware  # noqa: E402
from middleware.verify_session import verify_and_log_session  # noqa: E402
from config.limiter import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


port = int(os.environ.get("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()


app = FastAPI(
    title="Categorisation System Backend",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(get_middleware())
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SlowAPIMiddleware)

# CORS

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Categorisation System Backend is running"}


app.include_router(health_router, prefix="/api/health", tags=["Health"])
app.include_router(users_router, prefix="/api/v1/users", tags=["Users"])
# app.include_router(users_router, prefix="/api/v1/users", tags=["Users"], dependencies=[Depends(verify_and_log_session)])


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=settings.ENV == "local")
