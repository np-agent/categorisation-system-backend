from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENV: str = "local"

    # Mongo
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "selfbrief_dev"

    # Frontend origin, used for CORS.
    WEBSITE_DOMAIN: str = "http://localhost:3000"

    # SelfBrief CMS SSO
    SELFBRIEF_ISSUER: str = "https://dev-cms.selfbrief.aero/o"
    SELFBRIEF_BASE_URL: str = "https://dev-cms.selfbrief.aero"

    # AWS S3
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "eu-north-1"
    S3_BUCKET_NAME: str = "selfbrief-aip-docs"

    # Anthropic
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_MODEL: str = "claude-sonnet-4-5"
    ANTHROPIC_MAX_TOKENS: int = 16000
    ANTHROPIC_TEMPERATURE: float = 0.2

    # Background job poller (seconds between sweeps of running jobs)
    JOB_POLL_INTERVAL_SECONDS: int = 60

    # Resend email delivery
    RESEND_API_KEY: str | None = None
    FROM_EMAIL: str = "SelfBrief <onboarding@resend.dev>"

    # Bootstrap: first user to sign up gets super-admin. Set to false after setup.
    BOOTSTRAP_SUPER_ADMIN: bool = True

    # The internal SelfBrief organisation. Managed on its own team screen and
    # kept out of the customer organisation list. Only this org may hold
    # super-admin users.
    INTERNAL_ORG_SLUG: str = "selfbrief-aero"
    INTERNAL_ORG_NAME: str = "SelfBrief Aero"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
