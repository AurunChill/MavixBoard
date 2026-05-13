from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mavixboard.core.logger import logger
from mavixboard.fc import detect as detect_module
from mavixboard.fc.controllers import FlightController, PacketCallback

DetectFn = Callable[[], Awaitable[FlightController | None]]
ChangeCallback = Callable[[str | None, str], None]


class FCService:
    def __init__(
        self,
        detect_fn: DetectFn | None = None,
        scan_interval: float = 1.0,
    ) -> None:
        self._detect_fn = detect_fn or detect_module.detect
        self._scan_interval = scan_interval
        self._controller: FlightController | None = None
        self._on_packet: PacketCallback | None = None
        self._on_change: ChangeCallback | None = None
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_connected(self) -> bool:
        return self._controller is not None and self._controller.is_running

    @property
    def kind(self) -> str | None:
        return self._controller.kind if self._controller else None

    @property
    def name(self) -> str:
        return self._controller.name if self._controller else ''

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb
        if self._controller is not None:
            self._controller.set_packet_callback(cb)

    def set_change_callback(self, cb: ChangeCallback | None) -> None:
        self._on_change = cb

    async def send(self, data: bytes) -> None:
        if self._controller is None:
            return
        await self._controller.send(data)

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        await self._teardown_controller()

    async def _scan_loop(self) -> None:
        logger.info('[fc-service] scan loop started')
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                if self._controller is None or not self._controller.is_running:
                    await self._teardown_controller()
                    controller = await self._detect_fn()
                    if controller is None:
                        await asyncio.sleep(self._scan_interval)
                        continue
                    await self._activate_controller(controller)
                await asyncio.sleep(self._scan_interval)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[fc-service] scan loop stopped')

    async def _activate_controller(self, controller: FlightController) -> None:
        controller.set_packet_callback(self._on_packet)
        await controller.start()
        self._controller = controller
        logger.info('[fc-service] FC connected: %s / %s', controller.kind, controller.name)
        if self._on_change:
            try:
                self._on_change(controller.kind, controller.name)
            except Exception as exc:
                logger.warning('[fc-service] change callback error: %s', exc)

    async def _teardown_controller(self) -> None:
        if self._controller is None:
            return
        ctrl = self._controller
        self._controller = None
        try:
            await ctrl.close()
        except Exception as exc:
            logger.warning('[fc-service] controller close error: %s', exc)
        logger.info('[fc-service] FC disconnected')
        if self._on_change:
            try:
                self._on_change(None, '')
            except Exception as exc:
                logger.warning('[fc-service] change callback error: %s', exc)
