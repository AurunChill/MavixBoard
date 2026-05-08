from __future__ import annotations

from typing import TYPE_CHECKING

from mavixboard.core.config import settings

if TYPE_CHECKING:
    from mavixboard.gstreamer.camera import Camera, CameraParams


class PipelineBuilder:
    @staticmethod
    def build_pipeline_description(cameras: list['Camera']) -> str:
        turn = f' turn-server={settings.turn_server}' if settings.turn_server else ''
        webrtc_head = f'webrtcbin name=webrtc bundle-policy=max-bundle stun-server={settings.stun_server}{turn}'
        sources = ' '.join(PipelineBuilder._camera_branch(cam, idx) for idx, cam in enumerate(cameras))
        return f'{webrtc_head} {sources}'

    @staticmethod
    def _camera_branch(cam: 'Camera', idx: int) -> str:
        p = cam.params[cam.param_index]
        pt = 96 + idx
        source = (
            f'v4l2src device=/dev/video{cam.device_index} ! '
            f'queue max-size-buffers=2 leaky=downstream ! '
            f'videoconvert ! videoscale ! '
            f'video/x-raw,width={p.width},height={p.height},framerate={p.fps}/1 ! '
            f'x264enc name=enc{idx} bitrate={cam.bitrate_kbs} tune=zerolatency speed-preset=ultrafast key-int-max={p.fps} ! '
            f'video/x-h264,profile=constrained-baseline ! h264parse'
        )
        q = 'queue max-size-buffers=2 leaky=downstream silent=true'
        rtp = f'rtph264pay name=rtp{idx} config-interval=-1 aggregate-mode=zero-latency pt={pt}'
        caps = f'application/x-rtp,media=video,encoding-name=H264,payload={pt},clock-rate=90000,packetization-mode=(string)1'
        return f'{source} ! {q} ! {rtp} ! {caps} ! webrtc.sink_{idx}'

    @staticmethod
    def build_available_param(cam_index: int, params: 'CameraParams') -> str:
        return (
            f'v4l2src device=/dev/video{cam_index} ! '
            f'{PipelineBuilder._build_format(params.format)},'
            f'width={params.width},height={params.height},framerate={params.fps}/1 ! '
            f'{PipelineBuilder._build_decoder(params.format)} '
            f'videoconvert ! '
            f'fakesink'
        )

    @staticmethod
    def _build_format(format: str) -> str:
        match format:
            case 'YUYV':
                return 'video/x-raw,format=YUY2'
            case 'MJPG':
                return 'image/jpeg'
            case _:
                return 'video/x-raw'

    @staticmethod
    def _build_decoder(format: str) -> str:
        match format:
            case 'MJPG':
                return 'jpegdec !'
            case _:
                return ''
