from config.settings import settings

DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

PROD_ORIGINS = [
    settings.WEBSITE_DOMAIN,
]


def get_cors_origins():
    if settings.ENV == "prod":
        return PROD_ORIGINS
    return DEV_ORIGINS
