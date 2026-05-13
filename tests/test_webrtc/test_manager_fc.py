"""Integration tests for WebRTCManager ↔ FCService wiring via DataChannelHub."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mavixboard.webrtc.manager import WebRTCManager


def _make_webrtc_mock() -> MagicMock:
    webrtc = MagicMock(name='webrtcbin')
    counter = {'sig_n': 0}

    def fake_connect(_signal, _callback):
        counter['sig_n'] += 1
        return counter['sig_n']

    webrtc.connect.side_effect = fake_connect

    # 'create-data-channel' returns a new channel mock each time
    channels: list[MagicMock] = []

    def fake_emit(signal, *args, **kwargs):
        if signal == 'create-data-channel':
            ch = MagicMock(name=f'datachannel-{len(channels)}')

            def chan_connect(_sig, _cb):
                return 1
            ch.connect.side_effect = chan_connect
            channels.append(ch)
            return ch
        return None

    webrtc.emit.side_effect = fake_emit
    webrtc._channels = channels
    return webrtc


class _FakeFCService:
    """Minimal stand-in for FCService."""
    def __init__(self, kind='mavlink', name='ardupilot', connected=True):
        self.kind = kind
        self.name = name
        self.is_connected = connected
        self.packet_cb = None
        self.sent: list[bytes] = []

    def set_packet_callback(self, cb):
        self.packet_cb = cb

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.fixture(autouse=True)
def immediate_glib():
    def call_now(fn, *args):
        fn(*args)
        return 0
    with patch('mavixboard.webrtc.channels.GLib.idle_add', side_effect=call_now), \
         patch('mavixboard.webrtc.channels.GLib.Bytes.new', side_effect=lambda x: x):
        yield


def _open_channel(channel_mock):
    state = MagicMock()
    state.value_nick = 'open'
    state.__eq__ = lambda self, other: other == 'OPEN'
    channel_mock.get_property.return_value = state


# ============================================================================

async def test_manager_creates_channels_on_start_session():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    fc = _FakeFCService()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')

    assert mgr.channels is not None
    assert len(webrtc._channels) == 3
    mgr.end_session()


async def test_manager_does_not_require_fc_service():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')
    assert mgr.channels is not None
    mgr.end_session()


async def test_fc_packet_callback_set_on_session_start():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')

    assert fc.packet_cb is not None
    mgr.end_session()


async def test_fc_packet_callback_cleared_on_end_session():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')
    mgr.end_session()

    assert fc.packet_cb is None


async def test_fc_packet_flows_to_packet_channel_when_open():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')
    packet_ch_mock = webrtc._channels[0]  # first created = packet
    _open_channel(packet_ch_mock)
    # trigger state listener
    mgr.channels.packet._on_state(packet_ch_mock, None)

    fc.packet_cb(b'\xAB\xCD')

    packet_ch_mock.emit.assert_called_with('send-data', b'\xAB\xCD')
    mgr.end_session()


async def test_dc_packet_message_forwards_to_fc():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')

    buf = MagicMock()
    buf.get_data.return_value = b'\x11\x22'
    mgr.channels.packet._on_data(webrtc._channels[0], buf)
    # allow scheduled coro to run
    await asyncio.sleep(0.05)

    assert fc.sent == [b'\x11\x22']
    mgr.end_session()


async def test_config_channel_sends_fc_info_on_open():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService(kind='mavlink', name='ardupilot', connected=True)
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')
    config_ch_mock = webrtc._channels[2]  # third = config
    _open_channel(config_ch_mock)
    mgr.channels.config._on_state(config_ch_mock, None)

    # Expect a JSON FC-info message
    call = config_ch_mock.emit.call_args
    assert call.args[0] == 'send-data'
    import json
    decoded = json.loads(call.args[1].decode('utf-8'))
    assert decoded == {'type': 'fc', 'kind': 'mavlink', 'name': 'ardupilot'}
    mgr.end_session()


async def test_config_channel_sends_none_when_no_fc():
    """When the manager has no FC service, it still announces 'fc:none' on
    config-channel open so the GCS knows the FC slot is empty."""
    import json
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')
    config_ch_mock = webrtc._channels[2]
    _open_channel(config_ch_mock)
    mgr.channels.config._on_state(config_ch_mock, None)

    sent_messages = [
        json.loads(c.args[1].decode('utf-8'))
        for c in config_ch_mock.emit.call_args_list
        if len(c.args) >= 2 and c.args[0] == 'send-data'
    ]
    fc_msgs = [m for m in sent_messages if m.get('type') == 'fc']
    assert fc_msgs == [{'type': 'fc', 'kind': 'none', 'name': ''}]
    mgr.end_session()


async def test_end_session_closes_channels():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')
    hub = mgr.channels
    mgr.end_session()
    assert mgr.channels is None
    assert hub.packet.is_open is False
    assert hub.ping.is_open is False
    assert hub.config.is_open is False


async def test_cameras_sent_on_config_open():
    """When the config channel opens, the manager pushes the camera list."""
    import json

    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)

    cam = MagicMock()
    cam.device_index = 0
    cam.name = 'cam0'
    cam.bitrate_kbs = 1000
    cam.params = []
    cam.param_index = 0
    # Use dataclasses-asdict-friendly via __dataclass_fields__
    import dataclasses
    @dataclasses.dataclass
    class _Cam:
        device_index: int = 0
        name: str = 'cam0'
        bitrate_kbs: int = 1000
        param_index: int = 0
        params: list = dataclasses.field(default_factory=list)

    mgr.start_session('gcs-1', cameras=[_Cam()])
    config_ch_mock = webrtc._channels[2]
    _open_channel(config_ch_mock)
    mgr.channels.config._on_state(config_ch_mock, None)

    # We sent two messages: fc + cameras
    sent_messages = [
        json.loads(c.args[1].decode('utf-8'))
        for c in config_ch_mock.emit.call_args_list
        if len(c.args) >= 2 and c.args[0] == 'send-data'
    ]
    types = [m.get('type') for m in sent_messages]
    assert 'cameras' in types
    cameras_msg = next(m for m in sent_messages if m['type'] == 'cameras')
    assert cameras_msg['cameras'] == [
        {'device_index': 0, 'name': 'cam0', 'bitrate_kbs': 1000, 'param_index': 0, 'params': []}
    ]
    mgr.end_session()


async def test_no_cameras_message_when_list_empty():
    """If no cameras provided, only fc is sent (cameras-list is omitted)."""
    import json
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1', cameras=None)
    config_ch_mock = webrtc._channels[2]
    _open_channel(config_ch_mock)
    mgr.channels.config._on_state(config_ch_mock, None)

    sent_messages = [
        json.loads(c.args[1].decode('utf-8'))
        for c in config_ch_mock.emit.call_args_list
        if len(c.args) >= 2 and c.args[0] == 'send-data'
    ]
    types = [m.get('type') for m in sent_messages]
    assert 'cameras' not in types
    mgr.end_session()


async def test_replacing_session_rewires_fc():
    webrtc = _make_webrtc_mock()
    fc = _FakeFCService()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send, fc_service=fc)
    mgr.start_session('gcs-1')
    first_cb = fc.packet_cb
    mgr.start_session('gcs-2')

    # After re-wire, fc.packet_cb should point to the NEW packet channel
    assert fc.packet_cb is not None
    assert fc.packet_cb is not first_cb
    mgr.end_session()
