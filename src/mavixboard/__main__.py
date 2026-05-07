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
        if await session.connection_check():
            logger.info(f'Server is alive!')
            if await session.send_register(drone_token=token):
                logger.info(f'Drone is registered!')
            else:
                logger.error(f'Register error :(')
        else:
            logger.error(f'Server is not reachable!')
            asyncio.sleep(1)

    
asyncio.run(main())
