"""Контроллеры полётника: чтение/запись по MAVLINK и CRSF поверх UART."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

import serial_asyncio
from pymavlink import mavutil

from mavixboard.core.logger import logger
from mavixboard.fc.crsf import BAUDRATE as CRSF_BAUDRATE
from mavixboard.fc.crsf import CRSF, FRAME_RC_CHANNELS, VALID_ADDRESSES
from mavixboard.fc.mavlink import (
    MAV_AUTOPILOT,
    MAX_MSG_ID,
    MSG_HEARTBEAT,
    decode_battery,
    decode_command_ack,
    decode_global_position,
    decode_heartbeat_armed,
    decode_statustext,
    parse_msg_id,
    should_throttle_msg,
)

PacketCallback = Callable[[bytes], None]

# MAVLink system-id нашего GCS: HEARTBEAT с таким src — наш собственный
# исходящий, а не реальный полётник, поэтому им не обновляем имя/armed FC.
GCS_SYSTEM_ID = 255

# Раскладка заголовка CRSF-кадра — по ней RC-репитер опознаёт RC-кадр.
FRAME_ADDR_INDEX = 0       # байт device-address (sync начала кадра)
FRAME_TYPE_INDEX = 2       # байт типа кадра
FRAME_HEADER_MIN_LEN = 3   # минимум байт, чтобы прочитать адрес/длину/тип


#### Протоколы и типы ##################################################################
class FlightController(Protocol):
    kind: str
    name: str

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    def set_packet_callback(self, cb: PacketCallback | None) -> None: ...
    @property
    def is_running(self) -> bool: ...


#### Контроллер MAVLINK ################################################################
class MavlinkController:
    kind = 'mavlink'

    def __init__(self, connection: mavutil.mavlink_connection, name: str = 'MAVLink FC') -> None:
        self._conn = connection
        self.name = name
        self._on_packet: PacketCallback | None = None
        self._on_telemetry: Callable[[dict], None] | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._counters = [0] * MAX_MSG_ID
        # Отслеживаем фронт armed-состояния, чтобы логировать только переходы.
        self._last_armed: bool | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        """Аналог CrsfController.set_telemetry_callback.

        Состояние батареи PX4/ArduPilot отдаётся тем же словарём
        {'type':'battery', voltage, current, remaining}, чтобы GCS было
        всё равно, от какого полётника пришли байты.
        """
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
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None
        await asyncio.to_thread(self._safe_close_conn)

    def _safe_close_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception as exc:
            logger.debug('[mavlink] ошибка закрытия: %s', exc)

    async def send(self, data: bytes) -> None:
        if self._closed or self._conn is None:
            return
        await asyncio.to_thread(self._safe_write, data)

    def _safe_write(self, data: bytes) -> None:
        try:
            self._conn.write(data)
        except Exception as exc:
            logger.warning('[mavlink] ошибка записи: %s', exc)

    async def _read_loop(self) -> None:
        logger.info('[mavlink] цикл чтения запущен')
        # Считаем подряд идущие ошибки recv. Pyserial-овская «device
        # reports readiness to read but returned no data» сыплется при
        # отключении полётника — без circuit-breaker'а цикл крутится на
        # 100% CPU и забивает логи. После нескольких подряд закрываем
        # контроллер, чтобы scan-loop FCService подхватил отключение.
        consecutive_errors = 0
        ERROR_THRESHOLD = 5
        try:
            while not self._closed:
                msg, errored = await asyncio.to_thread(self._recv_one)
                if errored:
                    consecutive_errors += 1
                    if consecutive_errors >= ERROR_THRESHOLD:
                        logger.info('[mavlink] %d ошибок recv подряд, закрываю контроллер',
                                    consecutive_errors)
                        self._closed = True
                        break
                    # Короткая пауза, чтобы не крутиться вплотную на сбойном устройстве.
                    await asyncio.sleep(0.1)
                    continue
                consecutive_errors = 0
                if msg is None:
                    continue
                raw = msg.get_msgbuf()
                msg_id = parse_msg_id(raw)
                if msg_id == MSG_HEARTBEAT and msg.get_srcSystem() != GCS_SYSTEM_ID:
                    self.name = MAV_AUTOPILOT.get(getattr(msg, 'autopilot', 0), 'MAVLink FC')
                # HEARTBEAT несёт *реальное* armed-состояние в base_mode.
                # Лог перехода показывает, закрепилась ли предыдущая
                # COMMAND_ARM_DISARM или PX4 авто-дизармнул.
                hb = decode_heartbeat_armed(msg) if msg_id == MSG_HEARTBEAT else None
                if hb is not None and msg.get_srcSystem() != GCS_SYSTEM_ID:
                    if self._last_armed is None or hb['armed'] != self._last_armed:
                        logger.info('[mavlink] полётник armed=%s (custom_mode=0x%08x system_status=%d)',
                                    hb['armed'], hb['custom_mode'], hb['system_status'])
                        self._last_armed = hb['armed']
                        if self._on_telemetry is not None:
                            try:
                                self._on_telemetry(hb)
                            except Exception as exc:
                                logger.warning('[mavlink] ошибка hb-колбэка: %s', exc)
                # Телеметрия батареи приходит из SYS_STATUS (1) или
                # BATTERY_STATUS (147). Декодируем и отдаём ДО throttle-
                # гейта, чтобы не выбросить батарею только из-за того, что
                # она делит слот с высокочастотным мусором.
                battery = decode_battery(msg)
                if battery is not None and self._on_telemetry is not None:
                    try:
                        self._on_telemetry(battery)
                    except Exception as exc:
                        logger.warning('[mavlink] ошибка telemetry-колбэка: %s', exc)
                # GLOBAL_POSITION_INT (33) — слитая позиция: уходит оператору
                # по выделенному telemetry-каналу. Декодируем до throttle-гейта.
                pos = decode_global_position(msg)
                if pos is not None and self._on_telemetry is not None:
                    try:
                        self._on_telemetry(pos)
                    except Exception as exc:
                        logger.warning('[mavlink] ошибка gps-колбэка: %s', exc)
                # COMMAND_ACK — ответ на каждый отправленный нами
                # COMMAND_LONG (SET_MODE / ARM_DISARM). Логируется на борту
                # И форвардится в GCS, чтобы оператор видел причины отказа.
                ack = decode_command_ack(msg)
                if ack is not None:
                    logger.info('[mavlink] COMMAND_ACK cmd=%s result=%s',
                                ack['command_name'], ack['result_name'])
                    if self._on_telemetry is not None:
                        try:
                            self._on_telemetry(ack)
                        except Exception as exc:
                            logger.warning('[mavlink] ошибка ack-колбэка: %s', exc)
                # STATUSTEXT — то, что QGC показывает в виде «Arming
                # denied: ...», «Pre-arm: ...» и т.п. Лог + форвард.
                st = decode_statustext(msg)
                if st is not None:
                    logger.info('[mavlink] STATUSTEXT [%s] %s',
                                st['severity_name'], st['text'])
                    if self._on_telemetry is not None:
                        try:
                            self._on_telemetry(st)
                        except Exception as exc:
                            logger.warning('[mavlink] ошибка statustext-колбэка: %s', exc)
                if not should_throttle_msg(msg_id, self._counters):
                    continue
                if self._on_packet:
                    try:
                        self._on_packet(bytes(raw))
                    except Exception as exc:
                        logger.warning('[mavlink] ошибка packet-колбэка: %s', exc)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[mavlink] цикл чтения остановлен')

    def _recv_one(self) -> tuple[object | None, bool]:
        """Возвращает (msg, errored); msg может быть None и при успехе (таймаут)."""
        try:
            return self._conn.recv_match(blocking=True, timeout=0.1), False
        except Exception as exc:
            logger.debug('[mavlink] ошибка recv_match: %s', exc)
            return None, True


#### Контроллер CRSF ###################################################################
class CrsfController:
    kind = 'crsf'
    # Betaflight / INAV считают RC-линк живым по LINK_STATISTICS
    # (кадр 0x14) — без него они срываются в RXLOSS даже при идущем
    # RC_CHANNELS (0x16). 0.5 c было мало (BF подменял
    # каналы failsafe-значениями между LINK_STATS-фреймами); 0.1 c ближе
    # к реальной Crossfire-каденции (~10 Hz).
    LINK_STATS_INTERVAL_SECONDS = 0.1
    # RC-репитер: WebRTC доставляет пакеты пачками с джиттером, что
    # Betaflight видит как пропадание сигнала. Поэтому кэшируем последний
    # RC-фрейм и переотправляем его в UART с фиксированным шагом 5 мс
    # (200 Hz) — близко к реальной Crossfire-каденции. Если новых RC-
    # фреймов не было дольше RC_FRAME_TIMEOUT_SECONDS, репитер замолкает,
    # чтобы дрон не висел на последних стиках при разрыве линка (BF тогда
    # уйдёт в свой штатный RXLOSS-failsafe).
    RC_PUMP_INTERVAL_SECONDS = 0.005
    RC_FRAME_TIMEOUT_SECONDS = 0.6
    RC_FRAME_TYPE = FRAME_RC_CHANNELS

    def __init__(self, port: str, name: str = 'CRSF FC') -> None:
        self._port = port
        self.name = name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._on_packet: PacketCallback | None = None
        self._on_telemetry: Callable[[dict], None] | None = None
        self._read_task: asyncio.Task | None = None
        self._link_stats_task: asyncio.Task | None = None
        self._rc_pump_task: asyncio.Task | None = None
        self._latest_rc_frame: bytes | None = None
        self._last_rc_recv: float = 0.0
        self._rc_recv_count = 0
        self._rc_pump_count = 0
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._read_task is not None and not self._read_task.done()

    def set_packet_callback(self, cb: PacketCallback | None) -> None:
        self._on_packet = cb

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        """Получает каждый успешно декодированный CRSF-кадр телеметрии словарём.

        Форму словаря см. в CRSF.decode_telemetry — battery / gps /
        attitude / flight_mode / device_info. Координатор подключает это,
        чтобы пушить состояние батареи в GCS по config data-каналу.
        """
        self._on_telemetry = cb

    async def start(self) -> None:
        if self._reader is not None:
            return
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=CRSF_BAUDRATE,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        self._link_stats_task = asyncio.create_task(self._link_stats_loop())
        self._rc_pump_task = asyncio.create_task(self._rc_pump_loop())
        self._write_count = 0
        logger.info('[crsf] запущен на %s', self._port)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._read_task, self._link_stats_task, self._rc_pump_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._read_task = None
        self._link_stats_task = None
        self._rc_pump_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as exc:
                logger.debug('[crsf] ошибка закрытия: %s', exc)
            self._writer = None
        self._reader = None
        logger.info('[crsf] закрыт')

    async def send(self, data: bytes) -> None:
        """RC-кадры НЕ пишем в UART напрямую — кладём в кэш, а _rc_pump_loop
        переотправляет их в UART строго каждые RC_PUMP_INTERVAL_SECONDS.

        Это развязывает WebRTC-джиттер (пакеты от Desktop приходят пачками)
        от cadence'а UART, которую ожидает Betaflight. Все остальные
        кадры (на будущее — config/telemetry от GCS) идут напрямую.
        """
        if self._closed or self._writer is None:
            if self._closed:
                logger.debug('[crsf] отправка отброшена (контроллер закрыт)')
            elif self._writer is None:
                logger.warning('[crsf] отправка отброшена (writer не инициализирован)')
            return
        # Кладём кадр в RC-кэш, только если заголовок похож на RC-кадр:
        # валидный device-address (VALID_ADDRESSES) и тип == FRAME_RC_CHANNELS.
        # Прочие кадры (config/telemetry) идут ниже в UART напрямую.
        if (len(data) >= FRAME_HEADER_MIN_LEN
                and data[FRAME_ADDR_INDEX] in VALID_ADDRESSES
                and data[FRAME_TYPE_INDEX] == self.RC_FRAME_TYPE):
            self._latest_rc_frame = bytes(data)
            self._last_rc_recv = asyncio.get_event_loop().time()
            cnt = self._rc_recv_count + 1
            self._rc_recv_count = cnt
            if cnt == 1 or cnt % 100 == 0:
                # При tick'е Desktop 100 Hz это лог раз в секунду.
                logger.info('[crsf] RC-кэш #%d len=%d head=%s',
                            cnt, len(data), data[:6].hex())
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            logger.warning('[crsf] ошибка записи: %s', exc)
            return
        cnt = self._write_count + 1
        self._write_count = cnt
        if cnt == 1 or cnt % 50 == 0:
            logger.info('[crsf] →UART напрямую #%d len=%d head=%s',
                        cnt, len(data), data[:6].hex())

    async def _rc_pump_loop(self) -> None:
        """200 Hz пуш кэшированного RC-кадра в UART.

        Если кадр устарел (> RC_FRAME_TIMEOUT_SECONDS), бросаем
        переотправку — пусть BF сам триггерит RXLOSS-failsafe, а не
        остаётся на залипших стиках.
        """
        logger.info('[crsf] rc pump запущен @ %d Hz', int(1 / self.RC_PUMP_INTERVAL_SECONDS))
        loop = asyncio.get_event_loop()
        try:
            while not self._closed:
                if self._writer is not None and self._latest_rc_frame is not None:
                    age = loop.time() - self._last_rc_recv
                    if age >= self.RC_FRAME_TIMEOUT_SECONDS:
                        # Кадр протух — выкидываем кэш, repeater молчит до
                        # прихода нового свежего RC-кадра.
                        logger.warning('[crsf] rc pump: кадр устарел (%.2fs), пауза', age)
                        self._latest_rc_frame = None
                    else:
                        try:
                            self._writer.write(self._latest_rc_frame)
                            # drain не зовём: 26 байт × 200 Hz = 5.2 KB/s
                            # против пропускной 52 KB/s — буфер не забьётся,
                            # а лишний await съел бы cadence pump'а.
                            cnt = self._rc_pump_count + 1
                            self._rc_pump_count = cnt
                            if cnt == 1 or cnt % 1000 == 0:
                                # 1 раз в 5 секунд — подтверждаем, что pump жив.
                                logger.info('[crsf] rc pump #%d', cnt)
                        except Exception as exc:
                            logger.debug('[crsf] ошибка записи rc pump: %s', exc)
                await asyncio.sleep(self.RC_PUMP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[crsf] rc pump остановлен')

    async def _link_stats_loop(self) -> None:
        """Подмешивает кадр LINK_STATISTICS каждые LINK_STATS_INTERVAL_SECONDS.

        Так Betaflight/INAV считают, что у RC-линка есть сигнал — без этого
        они поднимают RXLOSS независимо от приходящего RC_CHANNELS. Кадр —
        постоянный heartbeat «хороший линк»; реальное качество линка из
        peer-to-peer WebRTC-канала отражается в UI отдельно.
        """
        logger.info('[crsf] цикл link_stats запущен')
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
                        logger.warning('[crsf] ошибка записи link_stats: %s', exc)
                await asyncio.sleep(self.LINK_STATS_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[crsf] цикл link_stats остановлен')

    async def _read_loop(self) -> None:
        logger.info('[crsf] цикл чтения запущен')
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
                            logger.warning('[crsf] ошибка telemetry-колбэка: %s', exc)
                    frame = CRSF._frame(ftype, payload)
                    if self._on_packet:
                        try:
                            self._on_packet(frame)
                        except Exception as exc:
                            logger.warning('[crsf] ошибка packet-колбэка: %s', exc)
        except asyncio.CancelledError:
            return
        finally:
            logger.info('[crsf] цикл чтения остановлен')
