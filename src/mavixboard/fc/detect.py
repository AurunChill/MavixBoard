from __future__ import annotations

import asyncio
import os
import time

import serial
import serial_asyncio
from pymavlink import mavutil

from mavixboard.core.config import settings
from mavixboard.core.logger import logger
from mavixboard.fc.controllers import CrsfController, FlightController, MavlinkController
from mavixboard.fc.crsf import BAUDRATE as CRSF_BAUDRATE
from mavixboard.fc.crsf import CRSF
from mavixboard.fc.mavlink import MAV_AUTOPILOT

DEFAULT_PORTS: tuple[str, ...] = (
    '/dev/ttyACM0',
    '/dev/ttyACM1',
    '/dev/ttyUSB0',
    '/dev/ttyUSB1',
    '/dev/ttyAMA0',
    '/dev/ttyAMA1',
)


async def detect(
    ports: tuple[str, ...] = DEFAULT_PORTS,
    mavlink_baud: int = 115200,
    mavlink_timeout: float = 3.0,
    crsf_timeout: float = 2.0,
) -> FlightController | None:
    # SITL-режим: MAVLINK_URL задан → подключаемся туда вместо UART-сканера.
    # CRSF в этом режиме не пробуем (симулятор отдаёт только MAVLink).
    url = settings.mavlink_url.strip()
    if url:
        return await _try_mavlink(url, mavlink_baud, mavlink_timeout)

    for port in ports:
        if not os.path.exists(port):
            continue
        ctrl = await _try_mavlink(port, mavlink_baud, mavlink_timeout)
        if ctrl is not None:
            return ctrl
        ctrl = await _try_crsf(port, crsf_timeout)
        if ctrl is not None:
            return ctrl
    return None


async def _try_mavlink(port: str, baud: int, timeout: float) -> MavlinkController | None:
    def _probe() -> tuple[str, mavutil.mavlink_connection] | None:
        conn = None
        try:
            conn = mavutil.mavlink_connection(port, baud=baud, source_system=255, source_component=0)
            msg = conn.wait_heartbeat(timeout=timeout)
            if msg:
                name = MAV_AUTOPILOT.get(getattr(msg, 'autopilot', 0), 'MAVLink FC')
                return name, conn
        except (serial.SerialException, OSError) as exc:
            logger.debug('[detect] mavlink %s: %s', port, exc)
        except Exception as exc:
            # Сетевые URL (udp/tcp) могут падать ValueError/AttributeError
            # из глубин pymavlink при кривом URL или порте, занятом другим
            # клиентом. Не валим detect-loop, просто логируем и идём дальше.
            logger.debug('[detect] mavlink %s: %s', port, exc)
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                logger.debug('[detect] mavlink close %s: %s', port, exc)
        return None

    result = await asyncio.to_thread(_probe)
    if result is None:
        return None
    name, conn = result
    return MavlinkController(conn, name=name)


async def _try_crsf(port: str, timeout: float) -> CrsfController | None:
    name = await _probe_crsf(port, timeout)
    if not name:
        return None
    return CrsfController(port, name=name)


async def _probe_crsf(port: str, timeout: float) -> str | None:
    try:
        reader, writer = await serial_asyncio.open_serial_connection(
            url=port, baudrate=CRSF_BAUDRATE,
        )
    except (serial.SerialException, OSError) as exc:
        logger.debug('[detect] crsf open %s: %s', port, exc)
        return None
    try:
        writer.write(CRSF.ping_frame())
        await writer.drain()
        deadline = time.time() + timeout
        buf = bytearray()
        device_name: str | None = None
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(64), timeout=min(remaining, 0.2))
            except asyncio.TimeoutError:
                continue
            if not chunk:
                await asyncio.sleep(0.01)
                continue
            buf.extend(chunk)
            for ftype, payload in CRSF.parse_frames(buf):
                decoded = CRSF.decode_telemetry(ftype, payload)
                if decoded is None:
                    continue
                if decoded.get('type') == 'device_info':
                    return decoded.get('name', 'CRSF FC')
                if device_name is None:
                    device_name = 'CRSF FC'
        return device_name
    except (serial.SerialException, OSError) as exc:
        logger.debug('[detect] crsf probe %s: %s', port, exc)
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            logger.debug('[detect] crsf cleanup %s: %s', port, exc)
