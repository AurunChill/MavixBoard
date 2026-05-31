from __future__ import annotations

import json
from collections.abc import Callable

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GLib, Gst, GstWebRTC

from mavixboard.core.logger import logger

PacketHandler = Callable[[bytes], None]
StringHandler = Callable[[str], None]


#### Базовый канал #####################################################################
def _channel_init(spec: str):
    structure, _ = Gst.Structure.from_string(spec)
    return structure


class _BaseChannel:
    def __init__(self, channel, label: str) -> None:
        self._ch = channel
        self._label = label
        self._open = False
        channel.connect('notify::ready-state', self._on_state)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return self._label

    def _on_state(self, channel, _pspec) -> None:
        state = channel.get_property('ready-state')
        self._open = state == GstWebRTC.WebRTCDataChannelState.OPEN
        nick = getattr(state, 'value_nick', str(state))
        logger.info('[dc:%s] state=%s', self._label, nick)
        if self._open:
            self._on_open()

    def _on_open(self) -> None:
        return

    def close(self) -> None:
        self._open = False


#### Каналы данных #####################################################################
class PacketChannel(_BaseChannel):
    INIT_SPEC = 'application/x-data-channel-init,ordered=true,max-retransmits=2,bitrate=6000000'

    def __init__(self, channel) -> None:
        super().__init__(channel, label='packet')
        self.on_packet: PacketHandler | None = None
        channel.connect('on-message-data', self._on_data)

    def send_bytes(self, data: bytes) -> None:
        if not self._open:
            return
        GLib.idle_add(self._emit, data)

    def _emit(self, data: bytes) -> bool:
        try:
            self._ch.emit('send-data', GLib.Bytes.new(data))
        except Exception as exc:
            logger.warning('[dc:packet] send error: %s', exc)
        return False

    def _on_data(self, _channel, buf) -> None:
        if buf is None or self.on_packet is None:
            return
        raw = buf.get_data()
        if not raw:
            return
        try:
            self.on_packet(bytes(raw))
        except Exception as exc:
            logger.warning('[dc:packet] handler error: %s', exc)


class PingChannel(_BaseChannel):
    INIT_SPEC = 'application/x-data-channel-init,ordered=true,max-retransmits=2'

    def __init__(self, channel) -> None:
        super().__init__(channel, label='ping')
        channel.connect('on-message-data', self._on_data)

    def _on_data(self, _channel, buf) -> None:
        if buf is None or not self._open:
            return
        raw = buf.get_data()
        if not raw:
            return
        GLib.idle_add(self._echo, raw)

    def _echo(self, raw) -> bool:
        try:
            self._ch.emit('send-data', GLib.Bytes.new(raw))
        except Exception as exc:
            logger.warning('[dc:ping] echo error: %s', exc)
        return False


class ConfigChannel(_BaseChannel):
    INIT_SPEC = 'application/x-data-channel-init,ordered=true'

    def __init__(self, channel) -> None:
        super().__init__(channel, label='config')
        self.on_message: Callable[[dict | list], None] | None = None
        self.on_open: Callable[[], None] | None = None
        channel.connect('on-message-data', self._on_data)

    def send_json(self, payload: dict | list) -> None:
        if not self._open:
            return
        try:
            text = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            logger.warning('[dc:config] encode error: %s', exc)
            return
        GLib.idle_add(self._emit, text.encode('utf-8'))

    def _emit(self, data: bytes) -> bool:
        try:
            self._ch.emit('send-data', GLib.Bytes.new(data))
        except Exception as exc:
            logger.warning('[dc:config] send error: %s', exc)
        return False

    def _on_data(self, _channel, buf) -> None:
        if buf is None or self.on_message is None:
            return
        raw = buf.get_data()
        if not raw:
            return
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning('[dc:config] decode error: %s', exc)
            return
        try:
            self.on_message(payload)
        except Exception as exc:
            logger.warning('[dc:config] handler error: %s', exc)

    def _on_open(self) -> None:
        if self.on_open:
            try:
                self.on_open()
            except Exception as exc:
                logger.warning('[dc:config] on_open error: %s', exc)


#### Хаб каналов #######################################################################
class DataChannelHub:
    """Создаёт и владеет всеми тремя data-каналами одной сессии пира."""

    def __init__(self, webrtc_elem) -> None:
        self.packet = PacketChannel(self._create(webrtc_elem, 'packet-channel', PacketChannel.INIT_SPEC))
        self.ping = PingChannel(self._create(webrtc_elem, 'ping-channel', PingChannel.INIT_SPEC))
        self.config = ConfigChannel(self._create(webrtc_elem, 'config-channel', ConfigChannel.INIT_SPEC))

    @staticmethod
    def _create(webrtc_elem, name: str, init_spec: str):
        init = _channel_init(init_spec)
        channel = webrtc_elem.emit('create-data-channel', name, init)
        if channel is None:
            raise RuntimeError(f'не удалось создать data-канал: {name}')
        return channel

    def close(self) -> None:
        self.packet.close()
        self.ping.close()
        self.config.close()
