"""Разбор отдельных MAVLINK-сообщений в единые телеметрийные словари."""

from __future__ import annotations

import struct

IMPORTANT_MSGS = {0, 1, 22, 77, 253, 33, 74, 147}
MSG_HEARTBEAT = 0
MSG_SYS_STATUS = 1
MSG_PARAM_VALUE = 22
MSG_PARAM_REQUEST_LIST = 21
MSG_PARAM_REQUEST_READ = 20
MSG_COMMAND_ACK = 77
MSG_BATTERY_STATUS = 147
MSG_STATUSTEXT = 253

# MAV_SEVERITY codes — для человекочитаемой строки в логе.
MAV_SEVERITY = {
    0: 'EMERGENCY', 1: 'ALERT', 2: 'CRITICAL', 3: 'ERROR',
    4: 'WARNING', 5: 'NOTICE', 6: 'INFO', 7: 'DEBUG',
}

# Человекочитаемые имена для MAV_RESULT (COMMAND_ACK.result). PX4
# использует их, чтобы объяснить, почему arm/set-mode и т.п. отклонён.
MAV_RESULT = {
    0: 'ACCEPTED',
    1: 'TEMPORARILY_REJECTED',
    2: 'DENIED',
    3: 'UNSUPPORTED',
    4: 'FAILED',
    5: 'IN_PROGRESS',
    6: 'CANCELLED',
}

# Часто отправляемые команды, для которых хотим красивые имена в логе.
MAV_CMD_NAMES = {
    176: 'DO_SET_MODE',
    400: 'COMPONENT_ARM_DISARM',
}


def decode_statustext(msg: object) -> dict | None:
    """STATUSTEXT (msgid 253) — человекочитаемое сообщение от PX4.

    Используется полётником, чтобы сказать «Arming denied: throttle above
    MIN», «Pre-arm: Battery low» и т.п. — ровно то, что отображает QGC.
    """
    try:
        msg_id = msg.get_msgId()
    except AttributeError:
        return None
    if msg_id != MSG_STATUSTEXT:
        return None
    sev = getattr(msg, 'severity', 6)
    text = getattr(msg, 'text', '')
    # pymavlink декодирует text как bytes для v1 и str для v2 — нормализуем.
    if isinstance(text, (bytes, bytearray)):
        text = text.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
    elif isinstance(text, str):
        text = text.rstrip('\x00')
    if not text:
        return None
    return {
        'type': 'statustext',
        'severity': int(sev),
        'severity_name': MAV_SEVERITY.get(int(sev), f'SEV_{sev}'),
        'text': text,
    }


def decode_heartbeat_armed(msg: object) -> dict | None:
    """Извлекает бит armed из HEARTBEAT.base_mode.

    PX4 выставляет MAV_MODE_FLAG_SAFETY_ARMED (0x80), когда моторы реально
    под напряжением — именно это говорит нам, закрепилась ли предыдущая
    COMMAND_ARM_DISARM или PX4 авто-дизармнул нас сразу после (например,
    по таймауту COM_DISARM_LAND).
    """
    try:
        msg_id = msg.get_msgId()
    except AttributeError:
        return None
    if msg_id != MSG_HEARTBEAT:
        return None
    base_mode = getattr(msg, 'base_mode', 0)
    armed = bool(int(base_mode) & 0x80)  # MAV_MODE_FLAG_SAFETY_ARMED
    return {
        'type': 'heartbeat',
        'armed': armed,
        'base_mode': int(base_mode),
        'custom_mode': int(getattr(msg, 'custom_mode', 0)),
        'system_status': int(getattr(msg, 'system_status', 0)),
    }


def decode_command_ack(msg: object) -> dict | None:
    """Преобразует COMMAND_ACK (msgid 77) в словарь с человекочитаемыми именами.

    PX4 возвращает ACK на каждый COMMAND_LONG (DO_SET_MODE,
    COMPONENT_ARM_DISARM, …); код результата объясняет, почему команда
    отклонена — бесценно для отладки «почему не армится».
    """
    try:
        msg_id = msg.get_msgId()
    except AttributeError:
        return None
    if msg_id != MSG_COMMAND_ACK:
        return None
    cmd = getattr(msg, 'command', None)
    res = getattr(msg, 'result', None)
    if cmd is None or res is None:
        return None
    return {
        'type': 'command_ack',
        'command': int(cmd),
        'command_name': MAV_CMD_NAMES.get(int(cmd), f'CMD_{cmd}'),
        'result': int(res),
        'result_name': MAV_RESULT.get(int(res), f'RESULT_{res}'),
    }


def decode_battery(msg: object) -> dict | None:
    """Извлекает единый словарь {'type':'battery', voltage, current, remaining}.

    Источник — SYS_STATUS (msgid 1) или BATTERY_STATUS (msgid 147).
    Возвращает None, если сообщение не про батарею или поля содержат
    «неизвестные» маркеры (UINT16_MAX / -1).
    """
    try:
        msg_id = msg.get_msgId()
    except AttributeError:
        return None
    if msg_id == MSG_SYS_STATUS:
        v_mv = getattr(msg, 'voltage_battery', 0xFFFF)
        c_ca = getattr(msg, 'current_battery', -1)
        rem = getattr(msg, 'battery_remaining', -1)
        if v_mv == 0xFFFF:
            return None
        return {
            'type': 'battery',
            'voltage': v_mv / 1000.0,
            'current': max(0.0, c_ca / 100.0) if c_ca != -1 else 0.0,
            'remaining': rem if rem != -1 else 0,
        }
    if msg_id == MSG_BATTERY_STATUS:
        cells = getattr(msg, 'voltages', None) or []
        # voltages[] — напряжение по ячейкам в mV, 0xFFFF означает «нет ячейки».
        total_mv = sum(v for v in cells if 0 < v < 0xFFFF)
        if total_mv == 0:
            return None
        c_ca = getattr(msg, 'current_battery', -1)
        rem = getattr(msg, 'battery_remaining', -1)
        return {
            'type': 'battery',
            'voltage': total_mv / 1000.0,
            'current': max(0.0, c_ca / 100.0) if c_ca != -1 else 0.0,
            'remaining': rem if rem != -1 else 0,
        }
    return None


MAV_AUTOPILOT = {0: 'generic', 3: 'ardupilot', 12: 'px4'}
MAV_TYPE = {
    0: 'generic', 1: 'fixed_wing', 2: 'quadrotor', 4: 'helicopter',
    13: 'hexarotor', 14: 'octorotor', 15: 'tricopter',
}


def parse_msg_id(data: bytes) -> int | None:
    if len(data) < 8:
        return None
    if data[0] == 0xFE:
        return data[5]
    elif data[0] == 0xFD and len(data) >= 12:
        return struct.unpack('<I', data[7:10] + b'\x00')[0]
    return None


def should_throttle_msg(msg_id: int | None, counters: list[int]) -> bool:
    """Возвращает True, если сообщение нужно отправить.

    Для неважных сообщений применяет троттлинг 20:1. `counters` — список
    из 300 чисел, мутируется на месте: инкрементируется для каждого
    неважного сообщения.
    """
    if msg_id is None or msg_id >= 300:
        return False
    if msg_id in IMPORTANT_MSGS:
        return True
    counters[msg_id] += 1
    return counters[msg_id] % 20 == 0
