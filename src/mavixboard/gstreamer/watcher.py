from __future__ import annotations

import asyncio
from collections.abc import Callable

from mavixboard.core.logger import logger
from mavixboard.gstreamer.camera import CameraManager

ChangedCallback = Callable[[set[int]], None]


class CameraWatcher:
    def __init__(self, interval: float = 5.0) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._known_ids: set[int] = set()
        self._callback: ChangedCallback | None = None

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
            CameraManager.clear_cache()
            cameras = await loop.run_in_executor(None, CameraManager.get_cameras, True)
        except Exception as exc:
            logger.warning('[watcher] scan error: %s', exc)
            return
        new_ids = {cam.device_index for cam in cameras}
        if new_ids != self._known_ids:
            logger.info('[watcher] camera set changed: %s -> %s', sorted(self._known_ids), sorted(new_ids))
            self._known_ids = new_ids
            if self._callback is not None:
                try:
                    self._callback(new_ids)
                except Exception as exc:
                    logger.warning('[watcher] callback error: %s', exc)
