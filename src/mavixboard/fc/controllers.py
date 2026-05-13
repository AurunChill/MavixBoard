from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

import serial_asyncio
from pymavlink import mavutil

from mavixboard.core.logger import logger
from mavixboard.fc.crsf import BAUDRATE as CRSF_BAUDRATE
from mavixboard.fc.crsf import CRSF
from mavixboard.fc.mavlink import (
    MAV_AUTOPILOT,
    MAV_TYPE,
    MSG_HEARTBEAT,
    parse_msg_id,
    should_throttle_msg,
)

PacketCallback = Callable[[bytes], None]


class FlightController(Protocol):
    kind: str
    name: str

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    def set_packet_callback(self, cb: PacketCallback | None) -> None: ...
    @property
    def is_running(self) -> bool: ...


class MavlinkController:
    kind = 'mavlink'

    def __init__(self, connection: mavutil.mavlink_connection, name: str = 'MAVLink FC') -> None:
        self._conn = connection
        self.name = name
        self._on_packet: PacketCallback | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._counters = [0] * 300

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await asyncio.to_thread(self._safe_close_conn)

    def _safe_close_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception as exc:
            logger.debug('[mavlink] close error: %s', exc)

    async def send(self, data: bytes) -> None:
        if self._closed or self._conn is None:
            return
        await asyncio.to_thread(self._safe_write, data)

    def _safe_write(self, data: bytes) -> None:
        try:
            self._conn.write(data)
        except Exception as exc:
            logger.warning('[mavlink] write error: %s', exc)

    async def _read_loop(self) -> None:
        logger.info('[mavlink] read loop started')
        try:
            while not self._closed:
                msg = await asyncio.to_thread(self._recv_one)
                if msg is None:
                    continue
                raw = msg.get_msgbuf()
                msg_id = parse_msg_id(raw)
                if msg_id == MSG_HEARTBEAT and msg.get_srcSystem() != 255:
                    self.name = MAV_AUTOPILOT.get(getattr(msg, 'autopilot', 0), 'MAVLink FC')
                if not should_throttle_msg(msg_id, self._counters):
                    continue
                if self._on_packet:
                    try:
                        self._on_packet(bytes(raw))
                    except Exception as exc:
                        logger.warning('[mavlink] packet callback error: %s', exc)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[mavlink] read loop stopped')

    def _recv_one(self):
        try:
            return self._conn.recv_match(blocking=True, timeout=0.1)
        except Exception as exc:
            logger.debug('[mavlink] recv_match error: %s', exc)
            return None


class CrsfController:
    kind = 'crsf'

    def __init__(self, port: str, name: str = 'CRSF FC') -> None:
        self._port = port
        self.name = name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._on_packet: PacketCallback | None = None
        self._read_task: asyncio.Task | None = None
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._read_task is not None and not self._read_task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    async def start(self) -> None:
        if self._reader is not None:
            return
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=CRSF_BAUDRATE,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info('[crsf] started on %s', self._port)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as exc:
                logger.debug('[crsf] close error: %s', exc)
            self._writer = None
        self._reader = None
        logger.info('[crsf] closed')

    async def send(self, data: bytes) -> None:
        if self._closed or self._writer is None:
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            logger.warning('[crsf] write error: %s', exc)

    async def _read_loop(self) -> None:
        logger.info('[crsf] read loop started')
        buf = bytearray()
        assert self._reader is not None
        try:
            while not self._closed:
                chunk = await self._reader.read(64)
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                buf.extend(chunk)
                for ftype, payload in CRSF.parse_frames(buf):
                    decoded = CRSF.decode_telemetry(ftype, payload)
                    if not decoded:
                        continue
                    frame = CRSF._frame(ftype, payload)
                    if self._on_packet:
                        try:
                            self._on_packet(frame)
                        except Exception as exc:
                            logger.warning('[crsf] packet callback error: %s', exc)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[crsf] read loop stopped')
