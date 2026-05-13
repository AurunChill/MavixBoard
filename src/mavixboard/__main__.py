import asyncio

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)

from mavixboard.coordinator import SessionCoordinator
from mavixboard.core.config import settings
from mavixboard.core.logger import logger, setup_file_logging
from mavixboard.fc.service import FCService
from mavixboard.gstreamer.camera import CameraManager
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.gstreamer.watcher import CameraWatcher
from mavixboard.server import api
from mavixboard.server.signal_client import SignalClient
from mavixboard.token import generator, storage


def _init_dirs() -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    setup_file_logging()


def _build_pipeline() -> GStreamerPipe | None:
    cameras = CameraManager.get_cameras(force_update=True)
    if not cameras:
        logger.error('cameras not found')
        return None
    return GStreamerPipe(cameras)


async def _ensure_registered(api_session: api.ApiSession, token: str) -> bool:
    while True:
        if not await api_session.connection_check():
            logger.error('signal server not reachable')
            await asyncio.sleep(5)
            continue
        if await api_session.send_register(drone_token=token):
            logger.info('drone is registered')
            return True
        logger.error('register error, retrying')
        await asyncio.sleep(5)


async def main() -> None:
    _init_dirs()

    token = storage.get()
    if not token:
        token = generator.generate(length=64)
        storage.write(token)

    api_session = await api.ApiSession.create()
    try:
        await _ensure_registered(api_session, token)
    finally:
        await api_session.close()

    fc_service = FCService()
    await fc_service.start()
    watcher = CameraWatcher()

    signal_client = SignalClient(url=settings.ws_url, drone_token=token)
    coordinator = SessionCoordinator(
        signal_client=signal_client,
        pipeline_factory=_build_pipeline,
        fc_service=fc_service,
        watcher=watcher,
    )
    try:
        await coordinator.run()
    finally:
        await fc_service.stop()


if __name__ == '__main__':
    asyncio.run(main())
