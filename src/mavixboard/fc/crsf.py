import struct

BAUDRATE = 420000
CH_MIN, CH_CENTER, CH_MAX = 172, 992, 1811


class CRSF:
    TYPE_NAMES = {
        0x02: 'GPS',
        0x08: 'BATTERY',
        0x14: 'LINK_STATS',
        0x16: 'RC_CHANNELS',
        0x1E: 'ATTITUDE',
        0x21: 'FLIGHT_MODE',
        0x28: 'DEVICE_PING',
        0x29: 'DEVICE_INFO',
    }

    @staticmethod
    def crc8(data):
        crc = 0
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = ((crc << 1) ^ 0xD5 if crc & 0x80 else crc << 1) & 0xFF
        return crc

    @staticmethod
    def _frame(ftype, payload, addr=0xC8):
        body = bytes([ftype]) + payload
        return bytes([addr, len(body) + 1]) + body + bytes([CRSF.crc8(body)])

    @staticmethod
    def rc_frame(channels):
        ch = (list(channels) + [CH_CENTER] * 16)[:16]
        bits = 0
        for i, v in enumerate(ch):
            bits |= max(0, min(0x7FF, v)) << (i * 11)
        return CRSF._frame(0x16, bits.to_bytes(22, 'little'))

    @staticmethod
    def link_stats_frame(rssi=-50, lq=100):
        r = rssi & 0xFF
        return CRSF._frame(0x14, bytes([r, r, lq, 10, 0, 4, 2, r, lq, 10]))

    @staticmethod
    def ping_frame():
        return CRSF._frame(0x28, bytes([0xC8, 0xEE]))

    @staticmethod
    def parse_frames(buf):
        while len(buf) >= 4:
            if buf[0] not in (0xC8, 0xEE, 0xEC, 0x00):
                buf.pop(0); continue
            fl = buf[1]
            if not 2 <= fl <= 62:
                buf.pop(0); continue
            total = fl + 2
            if len(buf) < total: break
            raw = bytes(buf[:total]); del buf[:total]
            if CRSF.crc8(raw[2:-1]) == raw[-1]:
                yield raw[2], raw[3:-1]

    @staticmethod
    def decode_telemetry(ftype, payload):
        p = payload
        if ftype == 0x08 and len(p) >= 8:
            return {'type': 'battery', 'voltage': int.from_bytes(p[0:2], 'big') / 10,
                    'current': int.from_bytes(p[2:4], 'big') / 10,
                    'capacity': int.from_bytes(p[4:7], 'big'), 'remaining': p[7]}
        if ftype == 0x02 and len(p) >= 15:
            return {'type': 'gps', 'lat': struct.unpack('>i', p[0:4])[0] / 1e7,
                    'lon': struct.unpack('>i', p[4:8])[0] / 1e7,
                    'satellites': p[14], 'alt': int.from_bytes(p[12:14], 'big', signed=True) - 1000}
        if ftype == 0x1E and len(p) >= 6:
            r = lambda i: round(struct.unpack('>h', p[i:i+2])[0] / 10000 * 57.2958, 1)
            return {'type': 'attitude', 'pitch': r(0), 'roll': r(2), 'yaw': r(4)}
        if ftype == 0x21:
            try: return {'type': 'flight_mode', 'mode': p.rstrip(b'\x00').decode('ascii')}
            except UnicodeDecodeError: pass
        if ftype == 0x29:
            try: return {'type': 'device_info', 'name': p[2:].split(b'\x00')[0].decode('ascii')}
            except UnicodeDecodeError: pass
        return None

    @staticmethod
    def axis_to_crsf(v, dz=0.05):
        if abs(v) < dz: return CH_CENTER
        n = (v - dz) / (1 - dz) if v > 0 else (v + dz) / (1 - dz)
        return max(CH_MIN, min(CH_MAX, int(CH_CENTER + n * (CH_MAX - CH_CENTER))))

    @staticmethod
    def throttle_to_crsf(v):
        n = max(0.0, min(1.0, (v + 1) / 2))
        return int(CH_MIN + n * (CH_MAX - CH_MIN))

    @staticmethod
    def crsf_to_us(v):
        return 1500 + (v - CH_CENTER) / 1.6
