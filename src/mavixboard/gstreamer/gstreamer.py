from __future__ import annotations
from typing import Callable

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GLib, GstVideo

from mavixboard.gstreamer.pipeline import PipelineBuilder
from mavixboard.gstreamer.camera import Camera
from mavixboard.core.logger import logger


class GStreamerPipe:
    def __init__(self, cameras: list['Camera']) -> None:
        self.pipeline: Gst.Pipeline = Gst.parse_launch(
            pipeline_description=PipelineBuilder.build_pipeline_description(cameras)
        )
        self.webrtc_elem: Gst.Element | None = self.pipeline.get_by_name('webrtc')
        self.on_playing: Callable[[], bool] | None = None
        self.on_error: Callable[[], None] | None = None
        self.bus_: Gst.Bus = self.pipeline.get_bus()
        self.bus_.add_watch(GLib.PRIORITY_DEFAULT, self.on_bus_message, self)
        self.stopped_: bool = False

    def start(self) -> None:
        if self.webrtc_elem:
            self.webrtc_elem.set_property('latency', 0)
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self.stopped_:
            return
        self.stopped_ = True
        self.bus_.remove_watch()  # drop ref the watch holds on the pipeline so v4l2src can finalize
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(Gst.SECOND * 2)  # block until v4l2 fd is actually released

    def update_bitrate(self, cam_idx: int, bitrate_kbs: int) -> bool:
        enc = self.pipeline.get_by_name(f'enc{cam_idx}')
        if enc is None:
            logger.warning(f'[transmit] enc{cam_idx} not found in pipeline')
            return False
        if enc.get_factory().get_name() != 'x264enc':
            logger.warning(f'[transmit] enc{cam_idx} is not x264enc, skipping dynamic bitrate update')
            return False
        enc.set_property('bitrate', bitrate_kbs)
        event = GstVideo.video_event_new_upstream_force_key_unit(
            Gst.CLOCK_TIME_NONE, True, 0
        )
        rtp = self.pipeline.get_by_name(f'rtp{cam_idx}')
        if rtp:
            rtp.send_event(event)
        logger.info(f'[transmit] Bitrate updated: cam{cam_idx} -> {bitrate_kbs} kbps')
        return True

    @staticmethod
    def on_bus_message(bus, message: Gst.Structure, gst_pipe: 'GStreamerPipe') -> bool:
        match message.type:
            case Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                logger.error(f'[transmit] GStreamer ERROR: {err} | {debug}')
                if gst_pipe.on_error:
                    GLib.idle_add(gst_pipe.on_error)
            case Gst.MessageType.WARNING:
                warn, debug = message.parse_warning()
                logger.warning(f'[transmit] GStreamer WARNING: {warn} | {debug}')
            case Gst.MessageType.STATE_CHANGED:
                if message.src == gst_pipe.pipeline:
                    old, new, _ = message.parse_state_changed()
                    logger.info(f'[transmit] Pipeline state: {old.value_nick} -> {new.value_nick}')
                    if new == Gst.State.PLAYING and gst_pipe.on_playing:
                        GLib.idle_add(gst_pipe.on_playing)
        return True
