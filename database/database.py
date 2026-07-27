from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

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

    @classmethod
    async def close_db(cls):
        if cls.client is not None:
            cls.client.close()

    @classmethod
    def get_collection(cls, collection_name: str):
        return cls.get_db()[collection_name]
