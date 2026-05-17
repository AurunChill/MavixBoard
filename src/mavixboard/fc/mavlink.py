import struct
from typing import Optional

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

# Human-readable names for MAV_RESULT (COMMAND_ACK.result). PX4 uses
# these to tell us why an arm/set-mode/etc was refused.
MAV_RESULT = {
    0: 'ACCEPTED',
    1: 'TEMPORARILY_REJECTED',
    2: 'DENIED',
    3: 'UNSUPPORTED',
    4: 'FAILED',
    5: 'IN_PROGRESS',
    6: 'CANCELLED',
}

# Common commands we send and want pretty names for in the log.
MAV_CMD_NAMES = {
    176: 'DO_SET_MODE',
    400: 'COMPONENT_ARM_DISARM',
}


def decode_statustext(msg) -> dict | None:
    """STATUSTEXT (msgid 253) — человекочитаемое сообщение от PX4.
    Используется FC чтобы сказать «Arming denied: throttle above MIN»,
    «Pre-arm: Battery low» и т.п. — ровно то что отображает QGC."""
    try:
        msg_id = msg.get_msgId()
    except AttributeError:
        return None
    if msg_id != MSG_STATUSTEXT:
        return None
    sev = getattr(msg, 'severity', 6)
    text = getattr(msg, 'text', '')
    # pymavlink decodes text as bytes for v1, str for v2 — нормализуем.
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


def decode_heartbeat_armed(msg) -> dict | None:
    """Pull the armed bit out of HEARTBEAT.base_mode. PX4 sets
    MAV_MODE_FLAG_SAFETY_ARMED (0x80) when motors are actually live —
    this is what tells us if a previous COMMAND_ARM_DISARM stuck or
    PX4 auto-disarmed us right after (e.g. COM_DISARM_LAND timeout)."""
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


def decode_command_ack(msg) -> dict | None:
    """Pull COMMAND_ACK (msgid 77) into a dict with human-readable
    names. PX4 returns ACK on every COMMAND_LONG (DO_SET_MODE,
    COMPONENT_ARM_DISARM, …); the result code tells us *why* a
    command was refused — invaluable for «why won't it arm» debug."""
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


def decode_battery(msg) -> dict | None:
    """Extract a uniform {'type':'battery', voltage, current, remaining}
    dict from either SYS_STATUS (msgid 1) or BATTERY_STATUS (msgid 147).
    Returns None if the message isn't battery-related or fields are
    «unknown» sentinels (UINT16_MAX / -1)."""
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
        # voltages[] is per-cell in mV with 0xFFFF marking «not present».
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


def parse_msg_id(data: bytes) -> Optional[int]:
    if len(data) < 8:
        return None
    if data[0] == 0xFE:
        return data[5]
    elif data[0] == 0xFD and len(data) >= 12:
        return struct.unpack('<I', data[7:10] + b'\x00')[0]
    return None


def should_throttle_msg(msg_id: int | None, counters: list[int]) -> bool:
    """Return True if message must be sent. Uses 20:1 throttle for non-important msgs.

    `counters` is a 300-int list mutated in place: incremented for each non-important msg.
    """
    if msg_id is None or msg_id >= 300:
        return False
    if msg_id in IMPORTANT_MSGS:
        return True
    counters[msg_id] += 1
    return counters[msg_id] % 20 == 0
