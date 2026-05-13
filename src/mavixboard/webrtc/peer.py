from __future__ import annotations

import asyncio

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstSdp, GstWebRTC

from mavixboard.core.logger import logger


class PeerSession:
    def __init__(
        self,
        gcs_id: str,
        webrtc_elem: Gst.Element,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.gcs_id = gcs_id
        self._webrtc = webrtc_elem
        self._loop = loop
        self.ice_candidates: asyncio.Queue = asyncio.Queue()
        self.offer_sdp: str | None = None
        self._negotiation_handler = self._webrtc.connect('on-negotiation-needed', self._on_negotiation_needed)
        self._ice_handler = self._webrtc.connect('on-ice-candidate', self._on_ice_candidate)

    def close(self) -> None:
        try:
            self._webrtc.disconnect(self._negotiation_handler)
            self._webrtc.disconnect(self._ice_handler)
        except (TypeError, AttributeError):
            pass

    def _on_negotiation_needed(self, _element: Gst.Element) -> None:
        logger.info('[peer %s] negotiation needed, creating offer', self.gcs_id)
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, self._webrtc, None)
        self._webrtc.emit('create-offer', None, promise)

    def _on_offer_created(self, promise: Gst.Promise, _element: Gst.Element, _user_data) -> None:
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply else None
        if not offer or not offer.sdp:
            logger.warning('[peer %s] offer or offer.sdp is None', self.gcs_id)
            return
        set_promise = Gst.Promise.new()
        self._webrtc.emit('set-local-description', offer, set_promise)
        set_promise.interrupt()
        self.offer_sdp = offer.sdp.as_text()
        logger.info('[peer %s] offer created, set as local description', self.gcs_id)

    def _on_ice_candidate(self, _element: Gst.Element, mline_index: int, candidate: str) -> None:
        payload = {'candidate': candidate, 'sdpMLineIndex': mline_index, 'sdpMid': str(mline_index)}
        self._loop.call_soon_threadsafe(self.ice_candidates.put_nowait, payload)

    def apply_answer(self, sdp_data: dict) -> bool:
        if sdp_data.get('type') != 'answer':
            logger.warning('[peer %s] expected answer, got %s', self.gcs_id, sdp_data.get('type'))
            return False
        sdp_text = sdp_data.get('sdp')
        if not isinstance(sdp_text, str):
            logger.warning('[peer %s] missing sdp text', self.gcs_id)
            return False
        _, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        self._webrtc.emit('set-remote-description', answer, Gst.Promise.new())
        logger.info('[peer %s] answer set as remote description', self.gcs_id)
        return True

    def add_remote_ice(self, candidate: dict) -> bool:
        cand = candidate.get('candidate')
        mline_index = candidate.get('sdpMLineIndex')
        if not isinstance(cand, str) or not isinstance(mline_index, int):
            logger.warning('[peer %s] invalid ice payload: %s', self.gcs_id, candidate)
            return False
        self._webrtc.emit('add-ice-candidate', mline_index, cand)
        return True
