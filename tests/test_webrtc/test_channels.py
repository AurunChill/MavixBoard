from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mavixboard.webrtc.channels import (
    ConfigChannel,
    DataChannelHub,
    PacketChannel,
    PingChannel,
)


def _make_channel_mock(open_state: bool = False) -> MagicMock:
    ch = MagicMock(name='datachannel')
    counter = {'n': 0}

    def fake_connect(_signal, _cb):
        counter['n'] += 1
        return counter['n']

    ch.connect.side_effect = fake_connect

    state = MagicMock(name='state')
    # The conftest sets GstWebRTC.WebRTCDataChannelState.OPEN = 'OPEN'
    state.__eq__ = lambda self, other: other == 'OPEN' and open_state
    state.value_nick = 'open' if open_state else 'closed'
    ch.get_property = MagicMock(return_value=state)
    return ch


def _trigger_state(channel: PacketChannel | PingChannel | ConfigChannel, ch_mock: MagicMock, open_state: bool):
    state = MagicMock(name='state2')
    state.value_nick = 'open' if open_state else 'closed'
    state.__eq__ = lambda self, other: other == 'OPEN' and open_state
    ch_mock.get_property.return_value = state
    channel._on_state(ch_mock, None)


@pytest.fixture(autouse=True)
def immediate_glib_idle():
    """Run GLib.idle_add callbacks synchronously."""
    def call_now(fn, *args):
        fn(*args)
        return 0

    with patch('mavixboard.webrtc.channels.GLib.idle_add', side_effect=call_now) as m:
        yield m


@pytest.fixture
def immediate_glib_bytes():
    """Make GLib.Bytes.new return the input bytes verbatim."""
    with patch('mavixboard.webrtc.channels.GLib.Bytes.new', side_effect=lambda x: x) as m:
        yield m


# ============================================================================
# PacketChannel
# ============================================================================

def test_packet_channel_starts_closed():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    assert pc.is_open is False
    assert pc.label == 'packet'


def test_packet_channel_signals_connected():
    ch = _make_channel_mock()
    PacketChannel(ch)
    signals = [c.args[0] for c in ch.connect.call_args_list]
    assert 'notify::ready-state' in signals
    assert 'on-message-data' in signals


def test_packet_channel_opens_on_state_change():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    _trigger_state(pc, ch, True)
    assert pc.is_open is True


def test_packet_channel_send_bytes_when_closed_is_noop(immediate_glib_bytes):
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    pc.send_bytes(b'data')
    ch.emit.assert_not_called()


def test_packet_channel_send_bytes_when_open(immediate_glib_bytes):
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    _trigger_state(pc, ch, True)
    pc.send_bytes(b'\xAA\xBB')
    ch.emit.assert_called_with('send-data', b'\xAA\xBB')


def test_packet_channel_send_swallows_emit_errors(immediate_glib_bytes):
    ch = _make_channel_mock()
    ch.emit.side_effect = RuntimeError('boom')
    pc = PacketChannel(ch)
    _trigger_state(pc, ch, True)
    pc.send_bytes(b'X')  # should not raise


def test_packet_channel_receives_message_and_calls_handler():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    received: list[bytes] = []
    pc.on_packet = received.append

    buf = MagicMock()
    buf.get_data.return_value = b'\xCC\xDD'
    pc._on_data(ch, buf)
    assert received == [b'\xCC\xDD']


def test_packet_channel_ignores_message_without_handler():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    buf = MagicMock()
    buf.get_data.return_value = b'data'
    pc._on_data(ch, buf)  # no exception


def test_packet_channel_ignores_empty_buffer():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    received: list[bytes] = []
    pc.on_packet = received.append
    pc._on_data(ch, None)
    assert received == []
    buf = MagicMock()
    buf.get_data.return_value = b''
    pc._on_data(ch, buf)
    assert received == []


def test_packet_channel_handler_errors_swallowed():
    ch = _make_channel_mock()
    pc = PacketChannel(ch)
    pc.on_packet = lambda _: (_ for _ in ()).throw(RuntimeError('boom'))
    buf = MagicMock()
    buf.get_data.return_value = b'data'
    pc._on_data(ch, buf)  # should not raise


# ============================================================================
# PingChannel
# ============================================================================

def test_ping_echoes_messages_when_open(immediate_glib_bytes):
    ch = _make_channel_mock()
    pc = PingChannel(ch)
    _trigger_state(pc, ch, True)

    buf = MagicMock()
    buf.get_data.return_value = b'\x01\x02\x03'
    pc._on_data(ch, buf)

    ch.emit.assert_called_with('send-data', b'\x01\x02\x03')


def test_ping_does_not_echo_when_closed(immediate_glib_bytes):
    ch = _make_channel_mock()
    pc = PingChannel(ch)
    buf = MagicMock()
    buf.get_data.return_value = b'X'
    pc._on_data(ch, buf)
    ch.emit.assert_not_called()


def test_ping_ignores_none_and_empty():
    ch = _make_channel_mock()
    pc = PingChannel(ch)
    _trigger_state(pc, ch, True)
    pc._on_data(ch, None)
    buf = MagicMock()
    buf.get_data.return_value = b''
    pc._on_data(ch, buf)
    ch.emit.assert_not_called()


# ============================================================================
# ConfigChannel
# ============================================================================

def test_config_send_json_when_closed_is_noop(immediate_glib_bytes):
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    cc.send_json({'type': 'x'})
    ch.emit.assert_not_called()


def test_config_send_json_when_open_encodes_utf8(immediate_glib_bytes):
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    _trigger_state(cc, ch, True)
    cc.send_json({'a': 1})
    args = ch.emit.call_args.args
    assert args[0] == 'send-data'
    assert json.loads(args[1].decode('utf-8')) == {'a': 1}


def test_config_send_json_unencodable_does_not_raise(immediate_glib_bytes):
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    _trigger_state(cc, ch, True)

    class _Bad:
        pass

    cc.send_json({'x': _Bad()})  # should not raise


def test_config_on_message_decodes_json():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    received: list = []
    cc.on_message = received.append

    payload = json.dumps({'type': 'reboot'}).encode('utf-8')
    buf = MagicMock()
    buf.get_data.return_value = payload
    cc._on_data(ch, buf)
    assert received == [{'type': 'reboot'}]


def test_config_on_message_handles_list():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    received: list = []
    cc.on_message = received.append

    payload = json.dumps([{'device_index': 0, 'bitrate_kbs': 800}]).encode('utf-8')
    buf = MagicMock()
    buf.get_data.return_value = payload
    cc._on_data(ch, buf)
    assert received == [[{'device_index': 0, 'bitrate_kbs': 800}]]


def test_config_on_message_skips_invalid_json():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    received: list = []
    cc.on_message = received.append

    buf = MagicMock()
    buf.get_data.return_value = b'not-json{{'
    cc._on_data(ch, buf)
    assert received == []


def test_config_on_message_skips_non_utf8():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    received: list = []
    cc.on_message = received.append

    buf = MagicMock()
    buf.get_data.return_value = b'\xFF\xFE\xFD'
    cc._on_data(ch, buf)
    assert received == []


def test_config_on_open_callback_fires():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    opens: list = []
    cc.on_open = lambda: opens.append(True)
    _trigger_state(cc, ch, True)
    assert opens == [True]


def test_config_on_open_does_not_fire_when_closed():
    ch = _make_channel_mock()
    cc = ConfigChannel(ch)
    opens: list = []
    cc.on_open = lambda: opens.append(True)
    _trigger_state(cc, ch, False)
    assert opens == []


# ============================================================================
# DataChannelHub
# ============================================================================

def test_hub_creates_three_channels():
    webrtc = MagicMock()
    counter = {'n': 0}

    def emit_create(signal, name, init):
        if signal == 'create-data-channel':
            counter['n'] += 1
            return _make_channel_mock()
        return None

    webrtc.emit.side_effect = emit_create

    hub = DataChannelHub(webrtc)
    assert counter['n'] == 3
    assert isinstance(hub.packet, PacketChannel)
    assert isinstance(hub.ping, PingChannel)
    assert isinstance(hub.config, ConfigChannel)


def test_hub_raises_when_channel_creation_fails():
    webrtc = MagicMock()
    webrtc.emit.return_value = None
    with pytest.raises(RuntimeError, match='packet-channel'):
        DataChannelHub(webrtc)


def test_hub_close_marks_all_channels_closed():
    webrtc = MagicMock()
    webrtc.emit.side_effect = lambda *a, **kw: _make_channel_mock()
    hub = DataChannelHub(webrtc)
    # Force all open first
    for ch in (hub.packet, hub.ping, hub.config):
        _trigger_state(ch, ch._ch, True)
        assert ch.is_open is True

    hub.close()
    assert hub.packet.is_open is False
    assert hub.ping.is_open is False
    assert hub.config.is_open is False
