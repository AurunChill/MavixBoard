"""Обёртка над GStreamer-пайплайном WebRTC: запуск, остановка, шина сообщений."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import GLib, Gst, GstVideo

from mavixboard.core.logger import logger
from mavixboard.gstreamer.camera import Camera
from mavixboard.gstreamer.pipeline import PipelineBuilder


class GStreamerPipe:
    def __init__(self, cameras: list[Camera]) -> None:
        self.cameras: list[Camera] = cameras
        self.pipeline: Gst.Pipeline = Gst.parse_launch(
            pipeline_description=PipelineBuilder.build_pipeline_description(cameras)
        )
        self.webrtc_elem: Gst.Element | None = self.pipeline.get_by_name('webrtc')
        if self.webrtc_elem is not None:
            self._disable_upnp(self.webrtc_elem)
        self.on_playing: Callable[[], bool] | None = None
        self.on_error: Callable[[], None] | None = None
        self._bus: Gst.Bus = self.pipeline.get_bus()
        self._bus.add_watch(GLib.PRIORITY_DEFAULT, self.on_bus_message, self)
        self._stopped: bool = False

    @staticmethod
    def _disable_upnp(webrtc: Gst.Element) -> None:
        """Отключает UPnP (gupnp-igd) в ICE-агенте libnice внутри webrtcbin.

        ПОЧЕМУ это критично: при разборке пайплавна финальный unref webrtcbin
        освобождает ICE-агента libnice, чей финализатор gupnp-igd встаёт в
        `g_cond_wait` БЕЗ таймаута, ожидая снятия UPnP-проброса портов с
        роутера. Если роутер не отвечает (или UPnP-шлюза нет) — поток виснет
        навсегда, а вместе с ним event-loop, на котором сработал сборщик мусора
        (подтверждено py-spy дампом зависшего board).

        UPnP-проброс портов нам не нужен — связь идёт через STUN/TURN
        (см. PipelineBuilder), поэтому отключение безопасно и снимает дедлок
        у корня. Делаем best-effort по нескольким путям, т.к. набор свойств
        зависит от версии GStreamer/libnice; неудача не фатальна (на этот
        случай teardown дополнительно изолирован в отдельном потоке).
        """
        try:
            ice = webrtc.get_property('ice')
        except Exception:
            ice = None
        for obj, label in ((ice, 'GstWebRTCICE'), (webrtc, 'webrtcbin')):
            if obj is None:
                continue
            try:
                obj.set_property('upnp', False)
                logger.info('[ice] UPnP отключён (%s)', label)
                return
            except Exception:
                pass
        # Последняя попытка — добраться до самого NiceAgent.
        try:
            agent = ice.get_property('agent') if ice is not None else None
            if agent is not None:
                agent.set_property('upnp', False)
                logger.info('[ice] UPnP отключён (NiceAgent)')
                return
        except Exception:
            pass
        logger.warning('[ice] не удалось отключить UPnP через свойства — '
                       'полагаемся на изоляцию teardown в отдельном потоке')

    #### Жизненный цикл пайплайна ##########################################################
    def start(self, timeout_seconds: float = 3.0) -> bool:
        """Переводит пайплайн в PLAYING и блокирует до завершения смены состояния.

        Возвращает True при успехе. Вызывающий ОБЯЗАН проверять результат:
        если пайплайн не доходит до PLAYING (например, v4l2-устройство
        отключили между сканом и стартом), создание WebRTC data-каналов на
        полуразрушенном webrtcbin приведёт к ассерту GStreamer
        (`is_closed != TRUE`).
        """
        if self.webrtc_elem:
            self.webrtc_elem.set_property('latency', 0)
        self.pipeline.set_state(Gst.State.PLAYING)
        ret, state, _ = self.pipeline.get_state(int(timeout_seconds * Gst.SECOND))
        return ret == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Снимаем watch, чтобы он отпустил ссылку на пайплайн и v4l2src смог финализироваться.
        self._bus.remove_watch()
        self.pipeline.set_state(Gst.State.NULL)
        # Блокируемся, пока v4l2 fd реально не освободится.
        self.pipeline.get_state(Gst.SECOND * 2)

    #### Управление битрейтом ##############################################################
    def update_bitrate(self, cam_idx: int, bitrate_kbs: int) -> bool:
        enc = self.pipeline.get_by_name(f'enc{cam_idx}')
        if enc is None:
            logger.warning('[transmit] enc%d не найден в пайплайне', cam_idx)
            return False
        if enc.get_factory().get_name() != 'x264enc':
            logger.warning('[transmit] enc%d не x264enc, пропускаю динамическое обновление bitrate', cam_idx)
            return False
        enc.set_property('bitrate', bitrate_kbs)
        event = GstVideo.video_event_new_upstream_force_key_unit(
            Gst.CLOCK_TIME_NONE, True, 0
        )
        rtp = self.pipeline.get_by_name(f'rtp{cam_idx}')
        if rtp:
            rtp.send_event(event)
        logger.info('[transmit] bitrate обновлён: cam%d -> %d kbps', cam_idx, bitrate_kbs)
        return True

    #### Обработка шины сообщений ##########################################################
    @staticmethod
    def on_bus_message(bus: Gst.Bus, message: Gst.Message, gst_pipe: GStreamerPipe) -> bool:
        match message.type:
            case Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                logger.error('[transmit] GStreamer ERROR: %s | %s', err, debug)
                if gst_pipe.on_error:
                    GLib.idle_add(gst_pipe.on_error)
            case Gst.MessageType.WARNING:
                warn, debug = message.parse_warning()
                logger.warning('[transmit] GStreamer WARNING: %s | %s', warn, debug)
            case Gst.MessageType.STATE_CHANGED:
                if message.src == gst_pipe.pipeline:
                    old, new, _ = message.parse_state_changed()
                    logger.info('[transmit] состояние пайплайна: %s -> %s',
                                old.value_nick, new.value_nick)
                    if new == Gst.State.PLAYING and gst_pipe.on_playing:
                        GLib.idle_add(gst_pipe.on_playing)
        return True
