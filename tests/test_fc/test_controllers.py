from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mavixboard.fc.controllers import CrsfController, MavlinkController
from mavixboard.fc.crsf import CRSF
from mavixboard.fc.mavlink import IMPORTANT_MSGS


# ============================================================================
# MavlinkController
# ============================================================================

def _make_mavlink_msg(raw: bytes, src_system: int = 1, autopilot: int = 3):
    msg = MagicMock()
    msg.get_msgbuf.return_value = raw
    msg.get_srcSystem.return_value = src_system
    msg.autopilot = autopilot
    return msg


def _make_heartbeat_msg():
    # v1 frame, msg_id = 0 (HEARTBEAT)
    raw = b'\xFE\x09\x00\x01\x01\x00\x00\x00' + b'\x00' * 10
    msg = _make_mavlink_msg(raw, src_system=1, autopilot=3)
    return msg


async def test_mavlink_is_running_false_before_start():
    conn = MagicMock()
    ctrl = MavlinkController(conn, name='Test FC')
    assert ctrl.is_running is False


async def test_mavlink_start_creates_task():
    conn = MagicMock()
    conn.recv_match.return_value = None  # nothing to read
    ctrl = MavlinkController(conn)
    await ctrl.start()
    assert ctrl.is_running is True
    await ctrl.close()


async def test_mavlink_double_start_idempotent():
    conn = MagicMock()
    conn.recv_match.return_value = None
    ctrl = MavlinkController(conn)
    await ctrl.start()
    task = ctrl._task
    await ctrl.start()
    assert ctrl._task is task
    await ctrl.close()


async def test_mavlink_close_stops_and_closes_conn():
    conn = MagicMock()
    conn.recv_match.return_value = None
    ctrl = MavlinkController(conn)
    await ctrl.start()
    await ctrl.close()
    assert ctrl.is_running is False
    conn.close.assert_called_once()


async def test_mavlink_send_writes_to_conn():
    conn = MagicMock()
    conn.recv_match.return_value = None
    ctrl = MavlinkController(conn)
    await ctrl.start()
    await ctrl.send(b'\x01\x02\x03')
    await ctrl.close()
    conn.write.assert_called_with(b'\x01\x02\x03')


async def test_mavlink_send_after_close_is_noop():
    conn = MagicMock()
    conn.recv_match.return_value = None
    ctrl = MavlinkController(conn)
    await ctrl.start()
    await ctrl.close()
    await ctrl.send(b'\x01')
    # Only the close call may write nothing; no extra write
    assert b'\x01' not in [c.args[0] for c in conn.write.call_args_list if c.args]


async def test_mavlink_send_swallows_exceptions():
    conn = MagicMock()
    conn.recv_match.return_value = None
    conn.write.side_effect = RuntimeError('boom')
    ctrl = MavlinkController(conn)
    await ctrl.start()
    # should not raise
    await ctrl.send(b'\x01')
    await ctrl.close()


async def test_mavlink_read_loop_forwards_packets_via_callback():
    msg = _make_heartbeat_msg()
    conn = MagicMock()
    # First call returns msg, subsequent return None forever
    conn.recv_match.side_effect = [msg] + [None] * 100

    received: list[bytes] = []
    ctrl = MavlinkController(conn)
    ctrl.set_packet_callback(lambda data: received.append(data))
    await ctrl.start()
    # let read_loop run
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.01)
    await ctrl.close()
    assert len(received) == 1
    assert received[0] == msg.get_msgbuf()


async def test_mavlink_heartbeat_updates_name():
    msg = _make_heartbeat_msg()  # autopilot=3 → ardupilot
    conn = MagicMock()
    conn.recv_match.side_effect = [msg] + [None] * 100
    ctrl = MavlinkController(conn, name='unknown')
    ctrl.set_packet_callback(lambda _: None)
    await ctrl.start()
    for _ in range(50):
        if ctrl.name == 'ardupilot':
            break
        await asyncio.sleep(0.01)
    await ctrl.close()
    assert ctrl.name == 'ardupilot'


async def test_mavlink_ignores_messages_from_self():
    # source system 255 → not used to update FC info (it's our own GCS)
    msg = _make_mavlink_msg(
        raw=b'\xFE\x09\x00\x01\x01\x00\x00\x00' + b'\x00' * 10,
        src_system=255,
        autopilot=12,
    )
    conn = MagicMock()
    conn.recv_match.side_effect = [msg] + [None] * 100
    ctrl = MavlinkController(conn, name='kept')
    ctrl.set_packet_callback(lambda _: None)
    await ctrl.start()
    await asyncio.sleep(0.05)
    await ctrl.close()
    assert ctrl.name == 'kept'


async def test_mavlink_callback_errors_are_swallowed():
    msg = _make_heartbeat_msg()
    conn = MagicMock()
    conn.recv_match.side_effect = [msg] + [None] * 100

    def bad_cb(_data):
        raise RuntimeError('cb crash')

    ctrl = MavlinkController(conn)
    ctrl.set_packet_callback(bad_cb)
    await ctrl.start()
    await asyncio.sleep(0.05)
    # If unhandled, the test task would have died — close should still work
    await ctrl.close()
    assert ctrl.is_running is False


# ============================================================================
# CrsfController
# ============================================================================

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


@pytest.fixture
def patch_serial(monkeypatch):
    """Patch serial_asyncio.open_serial_connection to return preset reader/writer."""
    state = {'reader': None, 'writer': None}

    async def fake_open(url: str, baudrate: int):
        return state['reader'], state['writer']

    monkeypatch.setattr('mavixboard.fc.controllers.serial_asyncio.open_serial_connection', fake_open)
    return state


async def test_crsf_start_opens_reader_writer(patch_serial):
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = _FakeWriter()
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    assert ctrl.is_running is True
    await ctrl.close()


async def test_crsf_start_idempotent(patch_serial):
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = _FakeWriter()
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    first = ctrl._read_task
    await ctrl.start()
    assert ctrl._read_task is first
    await ctrl.close()


async def test_crsf_send_writes_to_writer(patch_serial):
    writer = _FakeWriter()
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = writer
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    await ctrl.send(b'\xAA\xBB')
    await ctrl.close()
    assert writer.buffer[0] == b'\xAA\xBB'


async def test_crsf_send_after_close_is_noop(patch_serial):
    writer = _FakeWriter()
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = writer
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    await ctrl.close()
    await ctrl.send(b'\xCC')
    assert b'\xCC' not in writer.buffer


async def test_crsf_close_idempotent(patch_serial):
    writer = _FakeWriter()
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = writer
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    await ctrl.close()
    await ctrl.close()  # double close should not raise
    assert writer.closed is True


async def test_crsf_read_loop_forwards_decoded_frames(patch_serial):
    # Build a battery frame
    payload = (115).to_bytes(2, 'big') + (24).to_bytes(2, 'big') + (12345).to_bytes(3, 'big') + bytes([78])
    frame = CRSF._frame(0x08, payload)
    patch_serial['reader'] = _FakeReader([frame])
    patch_serial['writer'] = _FakeWriter()

    received: list[bytes] = []
    ctrl = CrsfController('/dev/ttyUSB0')
    ctrl.set_packet_callback(lambda data: received.append(data))
    await ctrl.start()
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.01)
    await ctrl.close()
    assert len(received) == 1
    # Forwarded frame contains the original ftype+payload
    assert received[0][2] == 0x08


async def test_crsf_read_loop_skips_undecodable_frames(patch_serial):
    # 0x99 is unknown -> decode_telemetry returns None -> not forwarded
    frame = CRSF._frame(0x99, b'\x01\x02')
    patch_serial['reader'] = _FakeReader([frame])
    patch_serial['writer'] = _FakeWriter()

    received: list[bytes] = []
    ctrl = CrsfController('/dev/ttyUSB0')
    ctrl.set_packet_callback(lambda data: received.append(data))
    await ctrl.start()
    await asyncio.sleep(0.05)
    await ctrl.close()
    assert received == []


async def test_crsf_callback_errors_swallowed(patch_serial):
    payload = (115).to_bytes(2, 'big') + (24).to_bytes(2, 'big') + (12345).to_bytes(3, 'big') + bytes([78])
    frame = CRSF._frame(0x08, payload)
    patch_serial['reader'] = _FakeReader([frame])
    patch_serial['writer'] = _FakeWriter()

    ctrl = CrsfController('/dev/ttyUSB0')
    ctrl.set_packet_callback(lambda _: (_ for _ in ()).throw(RuntimeError('boom')))
    await ctrl.start()
    await asyncio.sleep(0.05)
    # Should not crash the loop
    await ctrl.close()


async def test_crsf_send_swallows_writer_errors(patch_serial):
    writer = _FakeWriter()
    def bad_drain():
        raise RuntimeError('boom')
    writer.drain = AsyncMock(side_effect=RuntimeError('boom'))
    patch_serial['reader'] = _FakeReader([])
    patch_serial['writer'] = writer
    ctrl = CrsfController('/dev/ttyUSB0')
    await ctrl.start()
    await ctrl.send(b'\x01')  # should not raise
    await ctrl.close()
