import struct
from typing import Optional

IMPORTANT_MSGS = {0, 1, 22, 77, 253, 33, 74, 147}
MSG_HEARTBEAT = 0
MSG_PARAM_VALUE = 22
MSG_PARAM_REQUEST_LIST = 21
MSG_PARAM_REQUEST_READ = 20

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
