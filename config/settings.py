from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENV: str = "local"

    # Mongo
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "boardcert_db_dev"

    # Domains (required by SuperTokens)
    API_DOMAIN: str = "http://localhost:8000"
    WEBSITE_DOMAIN: str = "http://localhost:3000"

    # SuperTokens
    SUPERTOKENS_CONNECTION_URI: str | None = None
    SUPERTOKENS_API_KEY: str | None = None

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
