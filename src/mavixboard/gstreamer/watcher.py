"""Опрос v4l2 для обнаружения горячего подключения/отключения камер."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mavixboard.core.logger import logger
from mavixboard.gstreamer.camera import V4l2Scanner

ChangedCallback = Callable[[set[int]], None]


def _enumerate_capture_indices(scanner: V4l2Scanner) -> set[int]:
    """Возвращает индексы /dev/videoN, похожие на настоящие камеры.

    Используется циклом опроса для обнаружения hot-plug. НЕ должна
    открывать устройство или запускать калибровку — это гонка с активным
    GStreamer-пайплайном, из-за которой камера ложно считается пропавшей.

    Один `filter_capture_devices` слишком либерален на Raspberry Pi: узлы
    ISP, кодека, unicam (обычно /dev/video10..23) все объявляют Video
    Capture, но не дают пригодных raw-форматов. Они появлялись бы и
    исчезали в счётчике иначе, чем то, что реально оставляет
    CameraRegistry._scan (он отбрасывает всё с пустым
    parse_camera_params), и watcher решал бы, что набор меняется на каждом
    опросе, запуская цикл пересоздания. Поэтому повторяем фильтр _scan:
    считаем только устройства хотя бы с одной комбинацией
    width × height × fps × format.
    """
    if not scanner.is_available():
        return set()
    names = scanner.get_device_names()
    paths = scanner.filter_capture_devices(names)
    ids: set[int] = set()
    for path in paths:
        if not scanner.parse_camera_params(path):
            continue
        try:
            ids.add(int(path.split('video')[1]))
        except (ValueError, IndexError):
            continue
    return ids


class CameraWatcher:
    def __init__(self, interval: float = 5.0) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._known_ids: set[int] = set()
        self._callback: ChangedCallback | None = None
        self._scanner = V4l2Scanner()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, initial_ids: set[int], callback: ChangedCallback) -> None:
        if self._task is not None:
            return
        self._known_ids = set(initial_ids)
        self._callback = callback
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._callback = None
        self._known_ids = set()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._tick()
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            new_ids = await loop.run_in_executor(None, _enumerate_capture_indices, self._scanner)
        except Exception as exc:
            logger.warning('[watcher] scan error: %s', exc)
            return
        if new_ids != self._known_ids:
            logger.info('[watcher] camera set changed: %s -> %s', sorted(self._known_ids), sorted(new_ids))
            self._known_ids = new_ids
            if self._callback is not None:
                try:
                    self._callback(new_ids)
                except Exception as exc:
                    logger.warning('[watcher] callback error: %s', exc)
