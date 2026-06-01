from __future__ import annotations

import asyncio

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstSdp, GstWebRTC

from mavixboard.core.config import settings
from mavixboard.core.logger import logger
from mavixboard.gstreamer.pipeline import _build_turn_url, _redact_url


class PeerSession:
    #### Жизненный цикл ####################################################################
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
        self._cand_counts = {'host': 0, 'srflx': 0, 'relay': 0, 'prflx': 0, 'other': 0}
        self._negotiation_handler = self._webrtc.connect('on-negotiation-needed', self._on_negotiation_needed)
        self._ice_handler = self._webrtc.connect('on-ice-candidate', self._on_ice_candidate)
        # Диагностика состояний ICE — без неё невозможно понять, почему не идёт медиа
        # за симметричным NAT (особенно когда relay-кандидат не собирается).
        self._gather_handler = self._webrtc.connect('notify::ice-gathering-state', self._on_gather_state)
        self._iceconn_handler = self._webrtc.connect('notify::ice-connection-state', self._on_iceconn_state)
        # Дополнительно добавляем TURN-сервер через сигнал add-turn-server.
        # Это надёжнее, чем свойство turn-server в gst-launch строке: сигнал
        # возвращает gboolean — сразу видно, принят ли URL libnice. Если
        # TURN указан и в строке, и через сигнал — webrtcbin принимает оба
        # и собирает relay-кандидаты с каждого.
        self._register_turn_server()

    def _register_turn_server(self) -> None:
        turn_url = _build_turn_url()
        if not turn_url:
            return
        try:
            ok = self._webrtc.emit('add-turn-server', turn_url)
        except Exception as exc:
            logger.warning('[peer %s] add-turn-server бросил исключение: %s', self.gcs_id, exc)
            return
        logger.info('[peer %s] add-turn-server(%s) = %s',
                    self.gcs_id, _redact_url(turn_url), bool(ok))

    def close(self) -> None:
        try:
            self._webrtc.disconnect(self._negotiation_handler)
            self._webrtc.disconnect(self._ice_handler)
            self._webrtc.disconnect(self._gather_handler)
            self._webrtc.disconnect(self._iceconn_handler)
        except (TypeError, AttributeError):
            pass

    #### Обработчики WebRTC ################################################################
    def _on_negotiation_needed(self, _element: Gst.Element) -> None:
        logger.info('[peer %s] требуется согласование, создаём offer', self.gcs_id)
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, self._webrtc, None)
        self._webrtc.emit('create-offer', None, promise)

    def _on_offer_created(self, promise: Gst.Promise, _element: Gst.Element, _user_data) -> None:
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply else None
        if not offer or not offer.sdp:
            logger.warning('[peer %s] offer или offer.sdp равно None', self.gcs_id)
            return
        set_promise = Gst.Promise.new()
        self._webrtc.emit('set-local-description', offer, set_promise)
        set_promise.interrupt()
        self.offer_sdp = offer.sdp.as_text()
        logger.info('[peer %s] offer создан и установлен как local description', self.gcs_id)

    def _on_ice_candidate(self, _element: Gst.Element, mline_index: int, candidate: str) -> None:
        # Считаем кандидатов по типам — на симметричном NAT критично видеть
        # наличие хотя бы одного relay-кандидата.
        cand_type = 'other'
        for t in ('host', 'srflx', 'relay', 'prflx'):
            if f'typ {t}' in candidate:
                cand_type = t
                break
        self._cand_counts[cand_type] = self._cand_counts.get(cand_type, 0) + 1
        logger.info('[peer %s] локальный ICE-кандидат type=%s mline=%d', self.gcs_id, cand_type, mline_index)
        payload = {'candidate': candidate, 'sdpMLineIndex': mline_index, 'sdpMid': str(mline_index)}
        self._loop.call_soon_threadsafe(self.ice_candidates.put_nowait, payload)

    def _on_gather_state(self, element: Gst.Element, _param) -> None:
        state = element.get_property('ice-gathering-state')
        logger.info('[peer %s] ICE gathering state -> %s, кандидатов пока: %s',
                    self.gcs_id, state.value_nick if hasattr(state, 'value_nick') else state,
                    self._cand_counts)
        # Когда gathering завершён (complete), это последний момент проверить,
        # есть ли relay-кандидаты. Их отсутствие = TURN-сервер не сработал
        # (неправильный URL, неверные creds, недоступен через текущий transport).
        try:
            from gi.repository import GstWebRTC as _Gw  # type: ignore
            complete_state = _Gw.WebRTCICEGatheringState.COMPLETE
        except Exception:
            complete_state = None
        if complete_state is not None and state == complete_state:
            if settings.turn_server and self._cand_counts.get('relay', 0) == 0:
                logger.warning('[peer %s] gathering завершён, но НЕТ relay-кандидатов — '
                               'TURN не работает; клиенты за симметричным NAT не подключатся. '
                               'Проверь формат TURN URL (turn://user:pass@host:port?transport=udp), '
                               'учётные данные и доступность.', self.gcs_id)

    def _on_iceconn_state(self, element: Gst.Element, _param) -> None:
        state = element.get_property('ice-connection-state')
        logger.info('[peer %s] ICE connection state -> %s',
                    self.gcs_id, state.value_nick if hasattr(state, 'value_nick') else state)

    #### Публичный API #####################################################################
    def apply_answer(self, sdp_data: dict) -> bool:
        if sdp_data.get('type') != 'answer':
            logger.warning('[peer %s] ожидался answer, получен %s', self.gcs_id, sdp_data.get('type'))
            return False
        sdp_text = sdp_data.get('sdp')
        if not isinstance(sdp_text, str):
            logger.warning('[peer %s] отсутствует текст sdp', self.gcs_id)
            return False
        _, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        self._webrtc.emit('set-remote-description', answer, Gst.Promise.new())
        logger.info('[peer %s] answer установлен как remote description', self.gcs_id)
        return True

    def add_remote_ice(self, candidate: dict) -> bool:
        cand = candidate.get('candidate')
        mline_index = candidate.get('sdpMLineIndex')
        if not isinstance(cand, str) or not isinstance(mline_index, int):
            logger.warning('[peer %s] некорректный ice payload: %s', self.gcs_id, candidate)
            return False
        self._webrtc.emit('add-ice-candidate', mline_index, cand)
        return True
