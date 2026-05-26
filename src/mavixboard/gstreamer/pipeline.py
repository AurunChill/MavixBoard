from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mavixboard.core.config import settings

if TYPE_CHECKING:
    from mavixboard.gstreamer.camera import Camera, CameraParams

logger = logging.getLogger(__name__)


def _normalize_scheme(raw: str, prefix: str) -> str:
    """Привести `prefix:host:port` к `prefix://host:port`. webrtcbin
    парсит ICE URL'ы строго через `scheme://` (см. документацию). Если
    схема записана как `stun:host:port` (одно `:`) — webrtcbin молча
    игнорирует строку, никакая ICE-функция не работает."""
    if raw.startswith(f'{prefix}:') and not raw.startswith(f'{prefix}://'):
        return f'{prefix}://' + raw[len(prefix) + 1:]
    return raw


def _build_stun_url() -> str:
    """Нормализованный stun-URL для webrtcbin."""
    raw = settings.stun_server.strip()
    if not raw:
        return ''
    raw = _normalize_scheme(raw, 'stuns')
    raw = _normalize_scheme(raw, 'stun')
    return raw


def _build_turn_url() -> str:
    """webrtcbin требует turn-server в формате turn://user:pass@host:port.
    Берём отдельные TURN_USERNAME/TURN_PASSWORD из env и подставляем в URL
    как ПЛЕЙН-строки, без percent-encoding.

    Почему без encoding: webrtcbin -> libnice не URL-декодит userinfo при
    отправке на TURN-сервер; coturn хеширует Long-Term Credentials поверх
    исходной строки пароля. Если запихнуть `BxBF%2B...` вместо `BxBF+...`,
    хеши не совпадут, allocation вернёт 401 Unauthorized и relay-кандидат
    не появится -- молча. См. Bug 758389 в GStreamer bug tracker.

    `+` и `/` в userinfo легальны по RFC 3986. Опасны только `:` и `@`
    в самих creds -- если они там окажутся, парсер сломает структуру.
    Для генерируемых нами openssl-паролей таких символов нет."""
    raw = settings.turn_server.strip()
    if not raw:
        return ''
    raw = _normalize_scheme(raw, 'turns')
    raw = _normalize_scheme(raw, 'turn')
    if settings.turn_username and '@' not in raw.split('://', 1)[-1]:
        scheme, rest = raw.split('://', 1)
        raw = f'{scheme}://{settings.turn_username}:{settings.turn_password}@{rest}'
    return raw


def _redact_url(url: str) -> str:
    """Скрыть password в URL для лога: turn://user:PASS@host -> turn://user:***@host."""
    if '://' not in url or '@' not in url:
        return url
    scheme, rest = url.split('://', 1)
    if '@' not in rest:
        return url
    userinfo, hostpart = rest.rsplit('@', 1)
    if ':' in userinfo:
        user, _ = userinfo.split(':', 1)
        return f'{scheme}://{user}:***@{hostpart}'
    return url


class PipelineBuilder:
    @staticmethod
    def build_pipeline_description(cameras: list['Camera']) -> str:
        stun_url = _build_stun_url()
        turn_url = _build_turn_url()
        logger.info('[ice] stun-server=%s', stun_url or '(empty)')
        logger.info('[ice] turn-server=%s', _redact_url(turn_url) or '(empty)')
        stun = f' stun-server={stun_url}' if stun_url else ''
        turn = f' turn-server={turn_url}' if turn_url else ''
        webrtc_head = f'webrtcbin name=webrtc bundle-policy=max-bundle{stun}{turn}'
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
            f'x264enc name=enc{idx} bitrate={cam.bitrate_kbs} tune=zerolatency speed-preset=ultrafast key-int-max={max(1, p.fps // 2)} intra-refresh=true ! '
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
