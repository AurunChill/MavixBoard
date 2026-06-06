from __future__ import annotations

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
from mavixboard.gstreamer.camera import get_default_registry
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.gstreamer.watcher import CameraWatcher
from mavixboard.server.enroll import ensure_enrolled
from mavixboard.server.signal_client import SignalClient


#### Подготовка окружения ##############################################################
def _init_dirs() -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    setup_file_logging()


def _build_pipeline() -> GStreamerPipe | None:
    # force_update=False: переиспользуем кэшированную калибровку, когда имя
    # устройства совпадает с ранее сохранённой Camera
    # (~/.local/share/mavixboard/<name>.json). Если подключена другая камера
    # (новое имя) или кэш сброшен из-за того, что CameraWatcher заметил смену
    # набора устройств, CameraRegistry._scan уходит на калибровку именно
    # этого устройства.
    cameras = get_default_registry().get_cameras(force_update=False)
    if not cameras:
        logger.error('[app] камеры не найдены')
        return None
    return GStreamerPipe(cameras)


async def _resolve_drone_token() -> str:
    """Возвращает DRONE_TOKEN для WS-авторизации.

    Если DRONE_TOKEN ещё не выдан (первый запуск), board сам регистрируется
    по ADMIN_ID + ENROLLMENT_TOKEN, получает токен/имя и дописывает их в
    env-файл. Дальнейшие запуски используют сохранённый токен.
    """
    drone_id, token, name = await ensure_enrolled(
        base_url=settings.signal_server_ip,
        admin_id=settings.admin_id,
        enrollment_token=settings.enrollment_token,
        drone_id=settings.drone_id,
        drone_token=settings.drone_token,
        drone_name=settings.drone_name,
        env_path=settings.identity_env_path,
    )
    logger.info('[app] drone_id=%s name=%s', drone_id, name or '<unset>')
    return token


#### Точка входа #######################################################################
async def main() -> None:
    _init_dirs()

    # Главный цикл GLib должен работать ДО сборки любого GStreamer-пайплайна:
    # Gst.Bus.add_watch и GLib.idle_add срабатывают только пока он крутится, а
    # asyncio не прокачивает дефолтный контекст GLib.
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

    # systemd шлёт SIGTERM при остановке; SIGINT приходит от Ctrl+C в dev.
    # Оба должны запускать корректное завершение координатора (который владеет
    # пайплайном / data-каналами / связью с FC), а затем цикла GLib.
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info('[app] получен сигнал завершения, останавливаем координатор')
        coordinator.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Не все платформы поддерживают add_signal_handler; тесты на
            # потоках без дефолтного loop попадают в ветку RuntimeError.
            pass

    try:
        await coordinator.run()
    finally:
        await fc_service.stop()
        glib.stop()


if __name__ == '__main__':
    asyncio.run(main())
