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
    decode_battery,
    decode_command_ack,
    decode_heartbeat_armed,
    decode_statustext,
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
        self._on_telemetry: Callable[[dict], None] | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._counters = [0] * 300
        # Track armed-state edge so we only log on transitions.
        self._last_armed: bool | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        """Mirror of CrsfController.set_telemetry_callback. PX4/ArduPilot
        battery state is emitted as the same {'type':'battery', voltage,
        current, remaining} dict so the GCS doesn't care which FC the
        bytes came from."""
        self._on_telemetry = cb

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
        # Count consecutive recv failures. pyserial's "device reports
        # readiness to read but returned no data" repeatedly fires when
        # the FC is unplugged — without a circuit-breaker the loop spins
        # at 100% CPU spamming logs. After a few in a row we close the
        # controller so FCService's scan loop picks up the disconnection.
        consecutive_errors = 0
        ERROR_THRESHOLD = 5
        try:
            while not self._closed:
                msg, errored = await asyncio.to_thread(self._recv_one)
                if errored:
                    consecutive_errors += 1
                    if consecutive_errors >= ERROR_THRESHOLD:
                        logger.info('[mavlink] %d consecutive recv errors, closing controller',
                                    consecutive_errors)
                        self._closed = True
                        break
                    # Brief sleep so we don't tight-loop on a busted device.
                    await asyncio.sleep(0.1)
                    continue
                consecutive_errors = 0
                if msg is None:
                    continue
                raw = msg.get_msgbuf()
                msg_id = parse_msg_id(raw)
                if msg_id == MSG_HEARTBEAT and msg.get_srcSystem() != 255:
                    self.name = MAV_AUTOPILOT.get(getattr(msg, 'autopilot', 0), 'MAVLink FC')
                # HEARTBEAT carries the *real* armed state in base_mode.
                # Logging the transition tells us whether a previous
                # COMMAND_ARM_DISARM actually stuck or PX4 auto-disarmed.
                hb = decode_heartbeat_armed(msg) if msg_id == MSG_HEARTBEAT else None
                if hb is not None and msg.get_srcSystem() != 255:
                    if self._last_armed is None or hb['armed'] != self._last_armed:
                        logger.info('[mavlink] FC armed=%s (custom_mode=0x%08x system_status=%d)',
                                    hb['armed'], hb['custom_mode'], hb['system_status'])
                        self._last_armed = hb['armed']
                        if self._on_telemetry is not None:
                            try:
                                self._on_telemetry(hb)
                            except Exception as exc:
                                logger.warning('[mavlink] hb callback error: %s', exc)
                # Battery telemetry comes from SYS_STATUS (1) or
                # BATTERY_STATUS (147). Decode and fire BEFORE the
                # throttle gate so we don't drop battery just because
                # it shares a slot with high-rate junk.
                battery = decode_battery(msg)
                if battery is not None and self._on_telemetry is not None:
                    try:
                        self._on_telemetry(battery)
                    except Exception as exc:
                        logger.warning('[mavlink] telemetry callback error: %s', exc)
                # COMMAND_ACK — the response to every COMMAND_LONG we
                # send (SET_MODE / ARM_DISARM). Logged on board AND
                # forwarded to GCS so the operator sees rejection reasons.
                ack = decode_command_ack(msg)
                if ack is not None:
                    logger.info('[mavlink] COMMAND_ACK cmd=%s result=%s',
                                ack['command_name'], ack['result_name'])
                    if self._on_telemetry is not None:
                        try:
                            self._on_telemetry(ack)
                        except Exception as exc:
                            logger.warning('[mavlink] ack callback error: %s', exc)
                # STATUSTEXT — это то что QGC показывает в виде «Arming
                # denied: ...», «Pre-arm: ...» и т.п. Лог + forward.
                st = decode_statustext(msg)
                if st is not None:
                    logger.info('[mavlink] STATUSTEXT [%s] %s',
                                st['severity_name'], st['text'])
                    if self._on_telemetry is not None:
                        try:
                            self._on_telemetry(st)
                        except Exception as exc:
                            logger.warning('[mavlink] statustext callback error: %s', exc)
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

    def _recv_one(self) -> tuple[object | None, bool]:
        """Return (msg, errored). msg may be None even on success (timeout)."""
        try:
            return self._conn.recv_match(blocking=True, timeout=0.1), False
        except Exception as exc:
            logger.debug('[mavlink] recv_match error: %s', exc)
            return None, True


class CrsfController:
    kind = 'crsf'
    # Betaflight / INAV decide that the RC link is alive based on
    # LINK_STATISTICS (frame 0x14) — without it they raise RXLOSS even
    # if RC_CHANNELS (0x16) keeps streaming. We inject a synthetic
    # LINK_STATISTICS frame at the period below alongside the operator's
    # joystick frames. 0.5 s matches Crossfire/ELRS hardware cadence and
    # the legacy board implementation.
    LINK_STATS_INTERVAL_SECONDS = 0.5

    def __init__(self, port: str, name: str = 'CRSF FC') -> None:
        self._port = port
        self.name = name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._on_packet: PacketCallback | None = None
        self._on_telemetry: Callable[[dict], None] | None = None
        self._read_task: asyncio.Task | None = None
        self._link_stats_task: asyncio.Task | None = None
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._read_task is not None and not self._read_task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        """Receives every successfully-decoded CRSF telemetry frame as a
        dict (see CRSF.decode_telemetry for the shape — battery / gps /
        attitude / flight_mode / device_info). The coordinator wires this
        to push battery state to the GCS via the config data-channel."""
        self._on_telemetry = cb

    async def start(self) -> None:
        if self._reader is not None:
            return
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=CRSF_BAUDRATE,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        self._link_stats_task = asyncio.create_task(self._link_stats_loop())
        self._write_count = 0
        logger.info('[crsf] started on %s', self._port)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._read_task, self._link_stats_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._read_task = None
        self._link_stats_task = None
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
            if self._closed:
                logger.debug('[crsf] send dropped (controller closed)')
            elif self._writer is None:
                logger.warning('[crsf] send dropped (writer not initialised)')
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            logger.warning('[crsf] write error: %s', exc)
            return
        cnt = self._write_count + 1
        self._write_count = cnt
        if cnt == 1 or cnt % 50 == 0:
            # Stream is 50 Hz when joystick is active — one log per second.
            logger.info('[crsf] →UART packet #%d len=%d head=%s',
                        cnt, len(data), data[:6].hex())

    async def _link_stats_loop(self) -> None:
        """Inject a LINK_STATISTICS frame at LINK_STATS_INTERVAL_SECONDS so
        Betaflight/INAV believe the RC link has signal — without it they
        raise RXLOSS regardless of RC_CHANNELS arriving. The frame is a
        constant «good link» heartbeat; real link quality from the
        peer-to-peer WebRTC channel is reflected separately in the UI."""
        logger.info('[crsf] link_stats loop started')
        count = 0
        try:
            while not self._closed:
                if self._writer is not None:
                    frame = CRSF.link_stats_frame()
                    try:
                        self._writer.write(frame)
                        await self._writer.drain()
                        count += 1
                        if count == 1 or count % 20 == 0:
                            # 1 раз в ~10 сек подтверждаем, что цикл живой.
                            logger.info('[crsf] →UART LINK_STATS #%d len=%d head=%s',
                                        count, len(frame), frame[:6].hex())
                    except Exception as exc:
                        logger.warning('[crsf] link_stats write error: %s', exc)
                await asyncio.sleep(self.LINK_STATS_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[crsf] link_stats loop stopped')

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
                    if self._on_telemetry is not None:
                        try:
                            self._on_telemetry(decoded)
                        except Exception as exc:
                            logger.warning('[crsf] telemetry callback error: %s', exc)
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
