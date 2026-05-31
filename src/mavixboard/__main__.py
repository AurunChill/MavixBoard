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
    # force_update=False: переиспользуем кэшированную калибровку, когда имя
    # устройства совпадает с ранее сохранённой Camera
    # (~/.local/share/mavixboard/<name>.json). Если подключена другая камера
    # (новое имя) или кэш сброшен из-за того, что CameraWatcher заметил смену
    # набора устройств, CameraRegistry._scan уходит на калибровку именно
    # этого устройства.
    cameras = CameraManager.get_cameras(force_update=False)
    if not cameras:
        logger.error('[app] камеры не найдены')
        return None
    return GStreamerPipe(cameras)


async def _ensure_registered(api_session: api.ApiSession, token: str) -> bool:
    while True:
        if not await api_session.connection_check():
            logger.error('[app] сигнальный сервер недоступен')
            await asyncio.sleep(5)
            continue
        if await api_session.send_register(drone_token=token):
            logger.info('[app] дрон зарегистрирован')
            return True
        logger.error('[app] ошибка регистрации, повтор')
        await asyncio.sleep(5)


async def _resolve_drone_token() -> str:
    """Определяет, какой токен использовать для WS-авторизации.

    Production-путь: preset.env заполняет
    settings.drone_token (и DRONE_ID) — сервер уже создал строку Drone,
    поэтому просто используем зашитый токен и пропускаем регистрацию при
    старте.

    Dev-путь (нет preset.env): генерируем токен локально, сохраняем его и
    регистрируемся при каждом запуске (drone_service.register идемпотентна
    для одного drone_id, поэтому это безопасно между перезапусками).
    """
    if settings.drone_token:
        logger.info('[app] используется DRONE_TOKEN из preset.env (drone_id=%s)', settings.drone_id or '<unset>')
        return settings.drone_token

    token = storage.get()
    if not token:
        token = generator.generate(length=64)
        storage.write(token)

    # Только для dev: «звоним домой», чтобы строка Drone существовала. Теперь,
    # когда авторизация на /drones/register обязательна, понадобился бы
    # пользовательский JWT — в dev это означает отдельный скрипт
    # `mavixboard-enroll` (вне области этого кода);
    api_session = await api.ApiSession.create()
    try:
        await _ensure_registered(api_session, token)
    finally:
        await api_session.close()
    return token


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
