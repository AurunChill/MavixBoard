from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mavixboard.fc import detect as detect_module
from mavixboard.fc.controllers import CrsfController, MavlinkController
from mavixboard.fc.crsf import CRSF


class _FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(0.5)
        return b''


class _FakeWriter:
    def __init__(self) -> None:
        self.buffer: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.append(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


# ---------- detect prefers Mavlink ----------

async def test_detect_returns_mavlink_when_heartbeat_received(monkeypatch):
    heartbeat = MagicMock()
    heartbeat.autopilot = 3  # ardupilot

    conn = MagicMock()
    conn.wait_heartbeat.return_value = heartbeat

    def fake_connection(port, baud, source_system, source_component):
        return conn

    monkeypatch.setattr('mavixboard.fc.detect.mavutil.mavlink_connection', fake_connection)

    ctrl = await detect_module.detect(ports=('/dev/fake0',))
    assert isinstance(ctrl, MavlinkController)
    assert ctrl.name == 'ardupilot'


# ---------- detect falls through to CRSF ----------

async def test_detect_falls_back_to_crsf_when_no_heartbeat(monkeypatch):
    # Mavlink probe yields None (no heartbeat)
    mav_conn = MagicMock()
    mav_conn.wait_heartbeat.return_value = None
    monkeypatch.setattr(
        'mavixboard.fc.detect.mavutil.mavlink_connection',
        lambda *a, **kw: mav_conn,
    )

    # CRSF probe yields a device_info frame
    name_payload = b'\x00\x00Pixhawk6\x00'
    crsf_frame = CRSF._frame(0x29, name_payload)
    reader = _FakeReader([crsf_frame])
    writer = _FakeWriter()

    async def fake_open(url, baudrate):
        return reader, writer

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    ctrl = await detect_module.detect(ports=('/dev/fake0',), crsf_timeout=1.0)
    assert isinstance(ctrl, CrsfController)
    assert ctrl.name == 'Pixhawk6'


# ---------- detect returns None when nothing answers ----------

async def test_detect_returns_none_when_no_fc(monkeypatch):
    mav_conn = MagicMock()
    mav_conn.wait_heartbeat.return_value = None
    monkeypatch.setattr(
        'mavixboard.fc.detect.mavutil.mavlink_connection',
        lambda *a, **kw: mav_conn,
    )

    reader = _FakeReader([])  # silence
    writer = _FakeWriter()

    async def fake_open(url, baudrate):
        return reader, writer

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    ctrl = await detect_module.detect(ports=('/dev/fake0',), crsf_timeout=0.3)
    assert ctrl is None


# ---------- detect handles serial errors gracefully ----------

async def test_detect_handles_serial_open_error_for_crsf(monkeypatch):
    import serial

    mav_conn = MagicMock()
    mav_conn.wait_heartbeat.return_value = None
    monkeypatch.setattr(
        'mavixboard.fc.detect.mavutil.mavlink_connection',
        lambda *a, **kw: mav_conn,
    )

    async def fake_open(url, baudrate):
        raise serial.SerialException('port busy')

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    ctrl = await detect_module.detect(ports=('/dev/fake0',), crsf_timeout=0.3)
    assert ctrl is None


async def test_detect_handles_mavlink_open_error(monkeypatch):
    import serial

    def fake_connection(*args, **kwargs):
        raise serial.SerialException('cannot open')

    monkeypatch.setattr('mavixboard.fc.detect.mavutil.mavlink_connection', fake_connection)

    reader = _FakeReader([])
    writer = _FakeWriter()

    async def fake_open(url, baudrate):
        return reader, writer

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    ctrl = await detect_module.detect(ports=('/dev/fake0',), crsf_timeout=0.3)
    assert ctrl is None


# ---------- detect iterates over ports ----------

async def test_detect_tries_all_ports_in_order(monkeypatch):
    seen_ports: list[str] = []

    def fake_connection(port, **kw):
        seen_ports.append(port)
        conn = MagicMock()
        conn.wait_heartbeat.return_value = None
        return conn

    monkeypatch.setattr('mavixboard.fc.detect.mavutil.mavlink_connection', fake_connection)

    crsf_ports: list[str] = []

    async def fake_open(url, baudrate):
        crsf_ports.append(url)
        return _FakeReader([]), _FakeWriter()

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    await detect_module.detect(ports=('/a', '/b', '/c'), crsf_timeout=0.1)
    assert seen_ports == ['/a', '/b', '/c']
    assert crsf_ports == ['/a', '/b', '/c']


async def test_detect_stops_at_first_found(monkeypatch):
    heartbeat = MagicMock()
    heartbeat.autopilot = 12  # px4

    def fake_connection(port, **kw):
        conn = MagicMock()
        # Heartbeat only on second port
        if port == '/b':
            conn.wait_heartbeat.return_value = heartbeat
        else:
            conn.wait_heartbeat.return_value = None
        return conn

    monkeypatch.setattr('mavixboard.fc.detect.mavutil.mavlink_connection', fake_connection)

    crsf_attempts: list[str] = []

    async def fake_open(url, baudrate):
        crsf_attempts.append(url)
        return _FakeReader([]), _FakeWriter()

    monkeypatch.setattr('mavixboard.fc.detect.serial_asyncio.open_serial_connection', fake_open)

    ctrl = await detect_module.detect(ports=('/a', '/b', '/c'), crsf_timeout=0.1)
    assert isinstance(ctrl, MavlinkController)
    assert ctrl.name == 'px4'
    # /c is not tried; /a probed for crsf after mavlink failed there
    assert '/c' not in crsf_attempts
