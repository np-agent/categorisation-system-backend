from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

from config.settings import settings


class Database:
    client: AsyncIOMotorClient = None
    db: AsyncIOMotorDatabase = None

    @classmethod
    def get_db(cls) -> AsyncIOMotorDatabase:
        if cls.db is None:
            raise Exception("Database not initialized")
        return cls.db

    @classmethod
    async def connect_db(cls):
        print(f"Connecting to database '{settings.MONGO_DB_NAME}' (env={settings.ENV})")
        cls.client = AsyncIOMotorClient(
            settings.MONGO_URI,
            serverSelectionTimeoutMS=5000,
        )
        cls.db = cls.client[settings.MONGO_DB_NAME]
        print("Successfully connected to MongoDB")
        await cls._ensure_indexes()

    @classmethod
    async def close_db(cls):
        if cls.client is not None:
            cls.client.close()

    @classmethod
    async def _ensure_indexes(cls):
        db = cls.db

        # users
        await cls._drop_legacy_user_identity(db["users"])
        await db["users"].create_indexes([
            IndexModel([("cms_user_id", ASCENDING)], unique=True, sparse=True),
            IndexModel([("email", ASCENDING)], unique=True),
            IndexModel([("organization_id", ASCENDING)]),
        ])

        # organisations
        await db["organizations"].create_indexes([
            IndexModel([("slug", ASCENDING)], unique=True),
            IndexModel([("cms_organisation_id", ASCENDING)], unique=True, sparse=True),
        ])

        # airports
        await db["airports"].create_indexes([
            IndexModel([("icao_code", ASCENDING)], unique=True),
            IndexModel([("name", ASCENDING)]),
            IndexModel([("aip_available", ASCENDING)]),
        ])

        # prompt_templates
        await db["prompt_templates"].create_indexes([
            IndexModel([("name", ASCENDING)]),
            IndexModel([("is_active", ASCENDING)]),
        ])

        # jobs
        await db["jobs"].create_indexes([
            IndexModel([("organization_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("batch_id", ASCENDING)], sparse=True),
            IndexModel([("status", ASCENDING)]),
            IndexModel([("created_by_user_id", ASCENDING)]),
        ])

        print("Database indexes ensured")

    @classmethod
    async def _drop_legacy_user_identity(cls, users) -> None:
        """Remove the old email-password identity field and its unique index."""
        info = await users.index_information()
        if "supertokens_user_id_1" in info:
            await users.drop_index("supertokens_user_id_1")
        await users.update_many(
            {"supertokens_user_id": {"$exists": True}},
            {"$unset": {"supertokens_user_id": ""}},
        )
