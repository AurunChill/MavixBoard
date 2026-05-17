from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

import dataclasses

from mavixboard.core.logger import logger
from mavixboard.fc.service import FCService
from mavixboard.webrtc.channels import DataChannelHub
from mavixboard.webrtc.peer import PeerSession


class WebRTCManager:
    def __init__(
        self,
        webrtc_elem: Gst.Element,
        loop: asyncio.AbstractEventLoop,
        send: Callable[[dict], Awaitable[None]],
        fc_service: FCService | None = None,
    ) -> None:
        self._webrtc = webrtc_elem
        self._loop = loop
        self._send = send
        self._fc_service = fc_service
        self._peer: PeerSession | None = None
        self._channels: DataChannelHub | None = None
        self._cameras: list = []
        self._ice_pump_task: asyncio.Task | None = None
        self._offer_pump_task: asyncio.Task | None = None
        self.on_session_ended: Callable[[], None] | None = None

    @property
    def active_gcs_id(self) -> str | None:
        return self._peer.gcs_id if self._peer else None

    @property
    def channels(self) -> DataChannelHub | None:
        return self._channels

    def start_session(self, gcs_id: str, cameras: list | None = None) -> None:
        if self._peer is not None:
            logger.warning('[manager] session already active (gcs=%s), ending before new one', self._peer.gcs_id)
            self.end_session()
        logger.info('[manager] starting session with gcs=%s', gcs_id)
        self._peer = PeerSession(gcs_id, self._webrtc, self._loop)
        self._channels = DataChannelHub(self._webrtc)
        self._cameras = list(cameras) if cameras else []
        self._fc_fwd_count = 0  # reset per-session counter for forward log
        self._wire_channels()
        self._ice_pump_task = self._loop.create_task(self._pump_ice())
        self._offer_pump_task = self._loop.create_task(self._pump_offer())

    def end_session(self) -> None:
        if self._peer is None:
            return
        logger.info('[manager] ending session with gcs=%s', self._peer.gcs_id)
        for task in (self._ice_pump_task, self._offer_pump_task):
            if task and not task.done():
                task.cancel()
        self._ice_pump_task = None
        self._offer_pump_task = None
        self._unwire_channels()
        if self._channels is not None:
            self._channels.close()
            self._channels = None
        self._peer.close()
        self._peer = None
        if self.on_session_ended:
            self.on_session_ended()

    def _wire_channels(self) -> None:
        if self._channels is None:
            return
        # FC ↔ GCS bidirectional pipe through packet data-channel (if FC is up)
        if self._fc_service is not None:
            self._fc_service.set_packet_callback(self._channels.packet.send_bytes)
            self._channels.packet.on_packet = self._forward_to_fc
        # Send FC info + cameras list as soon as config channel opens
        self._channels.config.on_open = self._send_config_open

    def _unwire_channels(self) -> None:
        if self._fc_service is not None:
            self._fc_service.set_packet_callback(None)
        if self._channels is not None:
            self._channels.packet.on_packet = None
            self._channels.config.on_open = None
            self._channels.config.on_message = None

    def _forward_to_fc(self, data: bytes) -> None:
        if self._fc_service is None:
            return
        # Throttled debug log: first packet of each session + every ~50th
        # (so ~1 line per second at the 50 Hz joystick stream). Lets us
        # confirm packets actually arrive from the GCS without burying
        # the log under 50 lines/sec of identical entries.
        cnt = getattr(self, '_fc_fwd_count', 0) + 1
        self._fc_fwd_count = cnt
        if cnt == 1 or cnt % 50 == 0:
            logger.info('[manager] →FC packet #%d len=%d head=%s',
                        cnt, len(data), data[:6].hex())
        asyncio.run_coroutine_threadsafe(self._fc_service.send(data), self._loop)

    def _send_config_open(self) -> None:
        """Called when the config data-channel transitions to OPEN. Pushes
        the initial state the GCS needs: FC info + camera list."""
        self._send_fc_info()
        self._send_cameras()

    def _send_fc_info(self) -> None:
        if self._channels is None:
            return
        if self._fc_service is None or not self._fc_service.is_connected:
            self._channels.config.send_json({'type': 'fc', 'kind': 'none', 'name': ''})
            return
        self._channels.config.send_json({
            'type': 'fc',
            'kind': self._fc_service.kind or 'none',
            'name': self._fc_service.name,
        })

    def notify_fc_changed(self) -> None:
        """Public hook so the coordinator can push a fresh `fc` config
        message after a hot-plug/unplug — _send_config_open only fires
        once when the data-channel opens."""
        self._send_fc_info()

    def _send_cameras(self) -> None:
        if self._channels is None or not self._cameras:
            return
        try:
            payload = [dataclasses.asdict(cam) for cam in self._cameras]
        except TypeError:
            # In case a non-dataclass camera object sneaks in
            payload = [getattr(cam, '__dict__', {}) for cam in self._cameras]
        self._channels.config.send_json({'type': 'cameras', 'cameras': payload})

    async def handle_sdp(self, gcs_id: str, sdp_data: dict) -> None:
        if not self._guard(gcs_id):
            return
        assert self._peer is not None
        self._peer.apply_answer(sdp_data)

    async def handle_ice(self, gcs_id: str, candidate: dict) -> None:
        if not self._guard(gcs_id):
            return
        assert self._peer is not None
        self._peer.add_remote_ice(candidate)

    def _guard(self, gcs_id: str) -> bool:
        if self._peer is None:
            logger.warning('[manager] message for gcs=%s but no active session', gcs_id)
            return False
        if self._peer.gcs_id != gcs_id:
            logger.warning('[manager] message for gcs=%s but active is gcs=%s', gcs_id, self._peer.gcs_id)
            return False
        return True

    async def _pump_ice(self) -> None:
        assert self._peer is not None
        peer = self._peer
        try:
            while peer is self._peer:
                candidate = await peer.ice_candidates.get()
                await self._send({'type': 'ice', 'gcs_id': peer.gcs_id, 'candidate': candidate})
        except asyncio.CancelledError:
            return

    async def _pump_offer(self) -> None:
        assert self._peer is not None
        peer = self._peer
        sent: str | None = None
        try:
            while peer is self._peer:
                if peer.offer_sdp and peer.offer_sdp != sent:
                    await self._send({
                        'type': 'sdp',
                        'gcs_id': peer.gcs_id,
                        'sdp': {'type': 'offer', 'sdp': peer.offer_sdp},
                    })
                    sent = peer.offer_sdp
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
