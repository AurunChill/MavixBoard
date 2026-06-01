from mavixboard.fc.crsf import BAUDRATE, CRSF


#### crc8 ##############################################################################
def test_crc8_empty():
    assert CRSF.crc8(b'') == 0


def test_crc8_known_vector():
    # CRC8/DVB-S2 (polynomial 0xD5, init 0x00)
    assert CRSF.crc8(b'\x00') == 0
    assert CRSF.crc8(b'\x01') == 0xD5


def test_crc8_consistency_for_same_input():
    assert CRSF.crc8(b'foo') == CRSF.crc8(b'foo')


#### frame builder #####################################################################
def test_frame_structure_starts_with_addr_and_length():
    payload = b'\x01\x02\x03'
    frame = CRSF._frame(0x14, payload, addr=0xC8)
    assert frame[0] == 0xC8
    # length byte = len(ftype + payload + crc) = 1 + 3 + 1 = 5
    assert frame[1] == 5
    assert frame[2] == 0x14
    assert frame[3:6] == payload


def test_frame_includes_valid_crc():
    payload = b'\xAA\xBB'
    frame = CRSF._frame(0x16, payload)
    body = frame[2:-1]
    assert frame[-1] == CRSF.crc8(body)


#### link_stats_frame ##################################################################
def test_link_stats_frame_type_and_length():
    frame = CRSF.link_stats_frame(rssi=-75, lq=88)
    assert frame[2] == 0x14
    assert len(frame) == 14  # addr + len + ftype + 10 payload + crc


def test_link_stats_frame_default_signals_set():
    frame = CRSF.link_stats_frame()
    assert frame[2] == 0x14


#### ping_frame ########################################################################
def test_ping_frame_format():
    frame = CRSF.ping_frame()
    assert frame[2] == 0x28
    # Payload after ftype: bytes([0xC8, 0xEE])
    assert frame[3] == 0xC8
    assert frame[4] == 0xEE


#### parse_frames ######################################################################
def test_parse_frames_extracts_single_valid_frame():
    payload = b'\x01\x02\x03'
    frame = CRSF._frame(0x14, payload)
    buf = bytearray(frame)
    result = list(CRSF.parse_frames(buf))
    assert result == [(0x14, payload)]
    assert len(buf) == 0


def test_parse_frames_skips_garbage_prefix():
    frame = CRSF._frame(0x14, b'\xFF\xEE')
    buf = bytearray(b'\xAB\xCD' + frame)
    result = list(CRSF.parse_frames(buf))
    assert len(result) == 1
    assert result[0][0] == 0x14


def test_parse_frames_skips_bad_length():
    # leading 0xC8, then bogus length byte 0x00
    buf = bytearray(b'\xC8\x00\xC8' + CRSF._frame(0x14, b'\x01\x02'))
    result = list(CRSF.parse_frames(buf))
    assert len(result) == 1


def test_parse_frames_keeps_partial_buffer():
    frame = CRSF._frame(0x14, b'\x01\x02\x03')
    buf = bytearray(frame[:-2])
    result = list(CRSF.parse_frames(buf))
    assert result == []
    # buffer not consumed
    assert len(buf) == len(frame) - 2


def test_parse_frames_rejects_bad_crc():
    frame = bytearray(CRSF._frame(0x14, b'\x01'))
    frame[-1] ^= 0xFF  # corrupt CRC
    result = list(CRSF.parse_frames(frame))
    assert result == []


def test_parse_frames_multiple_in_one_buffer():
    f1 = CRSF._frame(0x14, b'\x01')
    f2 = CRSF._frame(0x16, b'\x02\x03')
    buf = bytearray(f1 + f2)
    result = list(CRSF.parse_frames(buf))
    assert [ftype for ftype, _ in result] == [0x14, 0x16]


#### decode_telemetry ##################################################################
def test_decode_battery():
    # voltage=11.5 -> 115; current=2.4 -> 24; cap=12345; remaining=78
    payload = (
        (115).to_bytes(2, 'big')
        + (24).to_bytes(2, 'big')
        + (12345).to_bytes(3, 'big')
        + bytes([78])
    )
    decoded = CRSF.decode_telemetry(0x08, payload)
    assert decoded == {
        'type': 'battery',
        'voltage': 11.5,
        'current': 2.4,
        'capacity': 12345,
        'remaining': 78,
    }


def test_decode_battery_short_payload_returns_none():
    assert CRSF.decode_telemetry(0x08, b'\x00' * 7) is None


def test_decode_device_info():
    payload = b'\x00\x00Pixhawk\x00trailing'
    decoded = CRSF.decode_telemetry(0x29, payload)
    assert decoded == {'type': 'device_info', 'name': 'Pixhawk'}


def test_decode_unknown_type_returns_none():
    assert CRSF.decode_telemetry(0x99, b'\x01\x02') is None


#### BAUDRATE sanity ###################################################################
def test_baudrate_is_420000():
    assert BAUDRATE == 420000
