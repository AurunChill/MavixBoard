from unittest.mock import MagicMock

from mavixboard.fc.mavlink import (
    IMPORTANT_MSGS,
    MAV_AUTOPILOT,
    MAV_TYPE,
    MSG_GLOBAL_POSITION_INT,
    MSG_HEARTBEAT,
    MSG_PARAM_REQUEST_LIST,
    MSG_PARAM_REQUEST_READ,
    MSG_PARAM_VALUE,
    decode_global_position,
    parse_msg_id,
    should_throttle_msg,
)


def _make_msg(msg_id: int, **attrs: object) -> MagicMock:
    msg = MagicMock()
    msg.get_msgId.return_value = msg_id
    for name, value in attrs.items():
        setattr(msg, name, value)
    return msg


#### parse_msg_id ######################################################################
def test_parse_msg_id_v1_format():
    # MAVLink v1: 0xFE + len + seq + sys + comp + msgid (byte) + payload + crc
    data = b'\xFE\x00\x00\x01\x01\x21' + b'\x00' * 4  # msg_id = 0x21 (33)
    assert parse_msg_id(data) == 0x21


def test_parse_msg_id_v1_heartbeat():
    data = b'\xFE\x00\x00\x01\x01\x00' + b'\x00' * 4
    assert parse_msg_id(data) == MSG_HEARTBEAT


def test_parse_msg_id_v2_format():
    # MAVLink v2: 0xFD + len + incompat + compat + seq + sys + comp + msgid (3 bytes) + ...
    # msg_id = 0x123456 (little-endian 24-bit)
    data = b'\xFD\x00\x00\x00\x00\x01\x01\x56\x34\x12' + b'\x00' * 4
    assert parse_msg_id(data) == 0x123456


def test_parse_msg_id_v2_heartbeat():
    data = b'\xFD\x00\x00\x00\x00\x01\x01\x00\x00\x00' + b'\x00' * 4
    assert parse_msg_id(data) == 0


def test_parse_msg_id_too_short_returns_none():
    assert parse_msg_id(b'') is None
    assert parse_msg_id(b'\xFE\x00\x00\x01\x01') is None  # 5 bytes < 8


def test_parse_msg_id_v2_too_short_returns_none():
    # v2 needs >= 12 bytes
    assert parse_msg_id(b'\xFD\x00\x00\x00\x00\x01\x01\x00') is None


def test_parse_msg_id_unknown_magic_returns_none():
    assert parse_msg_id(b'\xAB\x00\x00\x00\x00\x01\x01\x00') is None


#### constants sanity ##################################################################
def test_important_messages_includes_heartbeat_and_param_value():
    assert MSG_HEARTBEAT in IMPORTANT_MSGS
    assert MSG_PARAM_VALUE in IMPORTANT_MSGS


def test_param_request_msg_ids():
    assert MSG_PARAM_REQUEST_LIST == 21
    assert MSG_PARAM_REQUEST_READ == 20


def test_mav_autopilot_known_ids():
    assert MAV_AUTOPILOT[0] == 'generic'
    assert MAV_AUTOPILOT[3] == 'ardupilot'
    assert MAV_AUTOPILOT[12] == 'px4'


def test_mav_type_known_ids():
    assert MAV_TYPE[2] == 'quadrotor'
    assert MAV_TYPE[13] == 'hexarotor'


#### decode_global_position ############################################################
def test_decode_global_position():
    msg = _make_msg(
        MSG_GLOBAL_POSITION_INT,
        lat=557558000, lon=376173000, alt=150000, hdg=8950,
    )
    decoded = decode_global_position(msg)
    assert decoded is not None
    assert decoded['type'] == 'gps'
    assert decoded['lat'] == 55.7558
    assert decoded['lon'] == 37.6173
    assert decoded['alt'] == 150.0
    assert decoded['heading'] == 89.5
    assert decoded['sats'] == 0


def test_decode_global_position_unknown_heading_is_zero():
    msg = _make_msg(MSG_GLOBAL_POSITION_INT, lat=0, lon=0, alt=0, hdg=65535)
    decoded = decode_global_position(msg)
    assert decoded is not None
    assert decoded['heading'] == 0.0


def test_decode_global_position_wrong_msg_returns_none():
    assert decode_global_position(_make_msg(MSG_HEARTBEAT)) is None


def test_decode_global_position_no_msgid_returns_none():
    bad = object()
    assert decode_global_position(bad) is None


#### should_throttle_msg ###############################################################
def test_should_throttle_msg_important_always_passes():
    counters = [0] * 300
    for _ in range(50):
        assert should_throttle_msg(MSG_HEARTBEAT, counters) is True
    # counters for important are not mutated
    assert counters[MSG_HEARTBEAT] == 0


def test_should_throttle_msg_non_important_throttled_at_20():
    counters = [0] * 300
    # msg_id 100 is not in IMPORTANT_MSGS
    results = [should_throttle_msg(100, counters) for _ in range(40)]
    # Should be True at counter values 20 and 40 (modulo 20 == 0)
    assert results.count(True) == 2
    assert counters[100] == 40


def test_should_throttle_msg_invalid_returns_false():
    counters = [0] * 300
    assert should_throttle_msg(None, counters) is False
    assert should_throttle_msg(300, counters) is False
    assert should_throttle_msg(500, counters) is False
