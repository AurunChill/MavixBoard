import asyncio
import signal

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)

from mavixboard.coordinator import SessionCoordinator
from mavixboard.core.config import settings
from mavixboard.core.glib_loop import GLibMainLoopThread
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


async def _resolve_drone_token() -> str:
    """Decide which token to use for WS auth.

    Production path (.deb-installed): preset.env populates settings.drone_token
    (and DRONE_ID) — the server has already created the Drone row, so we
    just use the baked-in token and skip the on-boot register call.

    Dev path (no preset.env): generate a token locally, persist it, and
    register on every boot (drone_service.register is idempotent for the
    same drone_id, so this is safe across restarts).
    """
    if settings.drone_token:
        logger.info('using DRONE_TOKEN from preset.env (drone_id=%s)', settings.drone_id or '<unset>')
        return settings.drone_token

    token = storage.get()
    if not token:
        token = generator.generate(length=64)
        storage.write(token)

    # Dev-only: phone home so a Drone row exists. With auth now enforced
    # on /drones/register we'd need a user JWT — in dev that means a
    # separate `mavixboard-enroll` script (out of scope here); for the
    # common case where the .deb path is used this branch is not hit.
    api_session = await api.ApiSession.create()
    try:
        await _ensure_registered(api_session, token)
    finally:
        await api_session.close()
    return token


async def main() -> None:
    _init_dirs()

    # The GLib main loop must be running BEFORE any GStreamer pipeline is
    # built — Gst.Bus.add_watch and GLib.idle_add only fire while it
    # iterates, and asyncio does not pump the GLib default context.
    glib = GLibMainLoopThread()
    glib.start()

    token = await _resolve_drone_token()

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

    # systemd sends SIGTERM on stop; SIGINT comes from Ctrl+C in dev.
    # Both should trigger a graceful shutdown of the coordinator (which
    # owns the pipeline / data channels / FC link), then the GLib loop.
    loop = asyncio.get_running_loop()
    def _request_stop() -> None:
        logger.info('shutdown signal received; stopping coordinator')
        coordinator.stop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Not all platforms support add_signal_handler; tests on
            # threads-with-no-default-loop hit the RuntimeError branch.
            pass

    try:
        await coordinator.run()
    finally:
        await fc_service.stop()
        glib.stop()


if __name__ == '__main__':
    asyncio.run(main())
