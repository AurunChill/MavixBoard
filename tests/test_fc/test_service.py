from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mavixboard.fc.service import FCService


class _FakeController:
    """Minimal in-memory controller for testing FCService."""

    def __init__(self, kind: str = 'mavlink', name: str = 'fake') -> None:
        self.kind = kind
        self.name = name
        self.sent: list[bytes] = []
        self._on_packet = None
        self._running = False
        self.closed = False

    @property
    def is_running(self) -> bool:
        return self._running and not self.closed

    def set_packet_callback(self, cb) -> None:
        self._on_packet = cb

    async def start(self) -> None:
        self._running = True

    async def close(self) -> None:
        self._running = False
        self.closed = True

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    def emit_packet(self, data: bytes) -> None:
        if self._on_packet:
            self._on_packet(data)


#### start / stop ######################################################################
async def test_service_start_finds_controller():
    fake = _FakeController(kind='crsf', name='TBS')

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.05)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    assert svc.is_connected is True
    assert svc.kind == 'crsf'
    assert svc.name == 'TBS'
    await svc.stop()


async def test_service_returns_none_when_no_fc():
    async def detector():
        return None

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    await asyncio.sleep(0.1)
    assert svc.is_connected is False
    assert svc.kind is None
    assert svc.name == ''
    await svc.stop()


async def test_service_double_start_idempotent():
    fake = _FakeController()

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.05)
    await svc.start()
    first = svc._loop_task
    await svc.start()
    assert svc._loop_task is first
    await svc.stop()


async def test_service_stop_closes_controller():
    fake = _FakeController()

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    await svc.stop()
    assert fake.closed is True
    assert svc.is_connected is False


async def test_service_stop_without_start_does_not_raise():
    svc = FCService(detect_fn=lambda: None)
    await svc.stop()


#### packet callback ###################################################################
async def test_service_forwards_packet_callback():
    fake = _FakeController()

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    received: list[bytes] = []
    svc.set_packet_callback(lambda data: received.append(data))
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    fake.emit_packet(b'\xAA')
    fake.emit_packet(b'\xBB')
    assert received == [b'\xAA', b'\xBB']
    await svc.stop()


async def test_service_set_packet_callback_after_connect():
    fake = _FakeController()

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    received: list[bytes] = []
    svc.set_packet_callback(lambda data: received.append(data))
    fake.emit_packet(b'\xCC')
    assert received == [b'\xCC']
    await svc.stop()


#### send ##############################################################################
async def test_service_send_routes_to_controller():
    fake = _FakeController()

    async def detector():
        return fake

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    await svc.send(b'\x01\x02')
    assert fake.sent == [b'\x01\x02']
    await svc.stop()


async def test_service_send_without_controller_is_noop():
    async def detector():
        return None

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    await asyncio.sleep(0.05)
    await svc.send(b'\x01')  # should not raise
    await svc.stop()


#### change callback ###################################################################
async def test_service_change_callback_called_on_connect():
    fake = _FakeController(kind='mavlink', name='ardupilot')

    async def detector():
        return fake

    changes: list[tuple[str | None, str]] = []
    svc = FCService(detect_fn=detector, scan_interval=0.02)
    svc.set_change_callback(lambda k, n: changes.append((k, n)))
    await svc.start()
    for _ in range(50):
        if changes:
            break
        await asyncio.sleep(0.02)
    await svc.stop()
    # Expect at least the initial connect; stop also produces (None, '')
    assert ('mavlink', 'ardupilot') in changes
    assert (None, '') in changes


async def test_service_change_callback_errors_swallowed():
    fake = _FakeController()

    async def detector():
        return fake

    def bad_cb(_k, _n):
        raise RuntimeError('cb boom')

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    svc.set_change_callback(bad_cb)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    await svc.stop()


#### reconnect #########################################################################
async def test_service_reconnects_after_controller_dies():
    """If controller becomes is_running=False, service re-detects."""
    fake1 = _FakeController(name='first')
    fake2 = _FakeController(name='second')
    sequence = [fake1, fake2]

    async def detector():
        return sequence.pop(0) if sequence else None

    svc = FCService(detect_fn=detector, scan_interval=0.02)
    await svc.start()
    for _ in range(50):
        if svc.is_connected:
            break
        await asyncio.sleep(0.02)
    assert svc.name == 'first'

    # Kill the controller
    fake1._running = False

    for _ in range(50):
        if svc.name == 'second':
            break
        await asyncio.sleep(0.02)
    assert svc.name == 'second'
    await svc.stop()
