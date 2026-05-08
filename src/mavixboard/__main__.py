import asyncio

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

from mavixboard.server import api
from mavixboard.token import generator, storage
from mavixboard.gstreamer.camera import CameraManager
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.core.logger import logger, setup_file_logging
from mavixboard.core.config import settings


def _init_dirs() -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    setup_file_logging()


async def main():
    _init_dirs()

    token = storage.get()
    if not token:
        token = generator.generate(length=64)
        storage.write(token)

    session = await api.ApiSession.create()
    try:
        while True:
            if await session.connection_check():
                logger.info('Server is alive!')
                if await session.send_register(drone_token=token):
                    logger.info('Drone is registered!')
                    cameras = CameraManager.get_cameras(force_update=True)
                    if len(cameras) > 0:
                        gstreamer = GStreamerPipe(cameras)
                        gstreamer.start()
                        break
                    else:
                        logger.error('Cameras not found!')
                else:
                    logger.error('Register error :(')
            else:
                logger.error('Server is not reachable!')
            await asyncio.sleep(5)
    finally:
        await session.close()


asyncio.run(main())
