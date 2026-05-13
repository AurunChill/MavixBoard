from __future__ import annotations

import asyncio
from collections.abc import Callable

from mavixboard.core.logger import logger
from mavixboard.gstreamer.camera import V4l2Scanner

ChangedCallback = Callable[[set[int]], None]


def _enumerate_capture_indices(scanner: V4l2Scanner) -> set[int]:
    """Cheaply list /dev/videoN indices that are video-capture devices.

    Used by the polling loop to detect hot-plug. Must NOT open the device
    or trigger calibration — that races with the active GStreamer pipeline
    and falsely reports the camera as gone.
    """
    if not scanner.is_available():
        return set()
    names = scanner.get_device_names()
    paths = scanner.filter_capture_devices(names)
    ids: set[int] = set()
    for path in paths:
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
