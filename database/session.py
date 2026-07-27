from motor.motor_asyncio import AsyncIOMotorDatabase

from database.database import Database


async def connect_db():
    await Database.connect_db()


async def close_db():
    await Database.close_db()


async def get_database() -> AsyncIOMotorDatabase:
    return Database.get_db()
