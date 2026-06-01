"""Разбор кадров телеметрии CRSF (TBS Crossfire) и сборка служебных кадров.

Сторона дрона: принимает по UART телеметрию от полётного контроллера и
отдаёт ему служебные кадры (LINK_STATISTICS, DEVICE_PING). Кодирование
RC-каналов живёт на стороне оператора (MavixDesktop), сюда они приходят
уже готовыми байтами и лишь ретранслируются — поэтому его здесь нет.

Декодируем только то, что реально потребляется: BATTERY (пробрасывается
в GCS) и DEVICE_INFO (опознание полётника при детекте). GPS / ATTITUDE /
FLIGHT_MODE дрон не использует, поэтому они не декодируются.
"""

from __future__ import annotations

from collections.abc import Iterator

#### Константы протокола ###############################################################
BAUDRATE = 420000                 # стандартная скорость UART CRSF

# CRSF device-address (sync-байт начала кадра, buf[0])
ADDR_FC = 0xC8                    # полётный контроллер
ADDR_TX = 0xEE                    # модуль передатчика (пульт)
ADDR_RX = 0xEC                    # приёмник на дроне
ADDR_BROADCAST = 0x00
VALID_ADDRESSES = (ADDR_FC, ADDR_TX, ADDR_RX, ADDR_BROADCAST)

# Тип кадра (3-й байт)
FRAME_RC_CHANNELS = 0x16          # RC-каналы: дрон не декодирует, а ретранслирует
FRAME_BATTERY = 0x08
FRAME_LINK_STATS = 0x14
FRAME_DEVICE_PING = 0x28
FRAME_DEVICE_INFO = 0x29

# CRC-8 (полином 0xD5, как в DVB-S2)
CRC8_POLY = 0xD5
CRC8_MSB = 0x80                  # старший бит байта — флаг переноса в цикле
BYTE_MASK = 0xFF

# Границы длины кадра (2-й байт): тип + payload + CRC
MIN_FRAME_LEN = 2
MAX_FRAME_LEN = 62

# Напряжение/ток батареи хранятся в десятых долях (×10)
TELEM_DECISCALE = 10


class CRSF:
    #### CRC и сборка кадра ################################################################
    @staticmethod
    def crc8(data: bytes) -> int:
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ CRC8_POLY if crc & CRC8_MSB else crc << 1) & BYTE_MASK
        return crc

    @staticmethod
    def _frame(ftype: int, payload: bytes, addr: int = ADDR_FC) -> bytes:
        body = bytes([ftype]) + payload
        return bytes([addr, len(body) + 1]) + body + bytes([CRSF.crc8(body)])

    #### Исходящие служебные кадры #########################################################
    @staticmethod
    def link_stats_frame(rssi: int = -50, lq: int = 100) -> bytes:
        r = rssi & BYTE_MASK
        # payload: uplink RSSI ×2, uplink LQ, SNR, антенна, RF-режим, мощность TX,
        # downlink RSSI, downlink LQ, downlink SNR. Часть полей — правдоподобные
        # заглушки: FC важны лишь RSSI и LQ, чтобы считать линк живым.
        return CRSF._frame(FRAME_LINK_STATS, bytes([r, r, lq, 10, 0, 4, 2, r, lq, 10]))

    @staticmethod
    def ping_frame() -> bytes:
        # payload [кому, от кого]: пинг FC от имени TX-модуля
        return CRSF._frame(FRAME_DEVICE_PING, bytes([ADDR_FC, ADDR_TX]))

    #### Разбор и декодирование телеметрии #################################################
    @staticmethod
    def parse_frames(buf: bytearray) -> Iterator[tuple[int, bytes]]:
        while len(buf) >= 4:
            # buf[0] — device-address (sync начала кадра). Если байт не из
            # набора валидных адресов — сдвигаемся на 1 и ищем начало кадра.
            if buf[0] not in VALID_ADDRESSES:
                buf.pop(0)
                continue
            frame_len = buf[1]
            if not MIN_FRAME_LEN <= frame_len <= MAX_FRAME_LEN:
                buf.pop(0)
                continue
            total = frame_len + 2
            if len(buf) < total:
                break
            raw = bytes(buf[:total])
            del buf[:total]
            if CRSF.crc8(raw[2:-1]) == raw[-1]:
                yield raw[2], raw[3:-1]

    @staticmethod
    def decode_telemetry(ftype: int, payload: bytes) -> dict | None:
        p = payload
        if ftype == FRAME_BATTERY and len(p) >= 8:
            return {'type': 'battery',
                    'voltage': int.from_bytes(p[0:2], 'big') / TELEM_DECISCALE,
                    'current': int.from_bytes(p[2:4], 'big') / TELEM_DECISCALE,
                    'capacity': int.from_bytes(p[4:7], 'big'), 'remaining': p[7]}
        if ftype == FRAME_DEVICE_INFO:
            try:
                return {'type': 'device_info', 'name': p[2:].split(b'\x00')[0].decode('ascii')}
            except UnicodeDecodeError:
                pass
        return None
