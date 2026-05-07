import asyncio

from mavixboard.log import logger
from mavixboard.server import api
from mavixboard.token import generator, storage


async def main():
    token = storage.get()
    if not token:
        token = generator.generate(length=64)
        storage.write(token)
    session = await api.ApiSession.create()
    while True:
        connected = await session.connection_check()
        logger.info("connection: %s", connected)


asyncio.run(main())
