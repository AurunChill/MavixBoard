import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from mavixboard.core.config import settings
from mavixboard.gstreamer.camera import (
    Camera,
    CameraCalibrator,
    CameraParams,
    CameraRegistry,
    V4l2Scanner,
    _strip_usb_path,
)
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.gstreamer.pipeline import PipelineBuilder


#### fixtures ##########################################################################
@pytest.fixture
def cam_params():
    return CameraParams(width=640, height=480, fps=30, format='YUYV')


@pytest.fixture
def camera(cam_params):
    return Camera(device_index=0, name='Test Camera', params=[cam_params], param_index=0, bitrate_kbs=2000)


@pytest.fixture(autouse=True)
def patch_data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_path', tmp_path)


@pytest.fixture(autouse=True)
def patch_stun_turn(monkeypatch):
    monkeypatch.setattr(settings, 'stun_server', 'stun://test-stun:3478')
    monkeypatch.setattr(settings, 'turn_server', '')


#### CameraParams ######################################################################
class TestCameraParams:
    def test_fields_are_set(self, cam_params):
        assert cam_params.width == 640
        assert cam_params.height == 480
        assert cam_params.fps == 30
        assert cam_params.format == 'YUYV'

    def test_equality(self):
        a = CameraParams(1920, 1080, 30, 'MJPG')
        b = CameraParams(1920, 1080, 30, 'MJPG')
        assert a == b

    def test_inequality(self):
        a = CameraParams(640, 480, 30, 'YUYV')
        b = CameraParams(1280, 720, 30, 'YUYV')
        assert a != b

    def test_asdict(self, cam_params):
        d = dataclasses.asdict(cam_params)
        assert d == {'width': 640, 'height': 480, 'fps': 30, 'format': 'YUYV'}


#### Camera ############################################################################
class TestCamera:
    def test_default_bitrate(self, cam_params):
        cam = Camera(device_index=1, name='Cam', params=[cam_params], param_index=0)
        assert cam.bitrate_kbs == 1000

    def test_save_creates_file(self, camera, tmp_path):
        camera.save()
        assert (tmp_path / 'Test Camera.json').exists()

    def test_save_writes_correct_json(self, camera, tmp_path):
        camera.save()
        data = json.loads((tmp_path / 'Test Camera.json').read_text())
        assert data['device_index'] == 0
        assert data['name'] == 'Test Camera'
        assert data['bitrate_kbs'] == 2000
        assert data['params'][0]['width'] == 640

    def test_save_get_roundtrip(self, camera):
        camera.save()
        loaded = Camera.get('Test Camera')
        assert loaded == camera

    def test_get_returns_none_for_missing_file(self):
        assert Camera.get('nonexistent') is None

    def test_get_returns_none_for_corrupt_json(self, tmp_path):
        (tmp_path / 'bad.json').write_text('not json {{{')
        assert Camera.get('bad') is None

    def test_get_returns_none_for_wrong_fields(self, tmp_path):
        (tmp_path / 'wrong.json').write_text(json.dumps({'foo': 'bar'}))
        assert Camera.get('wrong') is None

    def test_get_restores_camera_params(self, camera):
        camera.save()
        loaded = Camera.get('Test Camera')
        assert isinstance(loaded.params[0], CameraParams)
        assert loaded.params[0].fps == 30


#### V4l2Scanner #######################################################################
V4L2_LIST_DEVICES_OUTPUT = """\
USB Camera (usb-0000:01:00.0-1.2) (usb-...):
\t/dev/video0
\t/dev/video1

Integrated Camera:
\t/dev/video2
"""

V4L2_ALL_OUTPUT_CAPTURE = """\
Driver Info:
\tDevice Caps : 0x04200001
\t\tVideo Capture
\t\tStreaming
"""

V4L2_ALL_OUTPUT_NO_CAPTURE = """\
Driver Info:
\tDevice Caps : 0x04200001
\t\tMetadata Capture
"""

V4L2_FORMATS_OUTPUT = """\
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.100s (10.000 fps)
\t[1]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
"""


class TestV4l2Scanner:
    def test_is_available_true_when_command_set(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        assert scanner.is_available() is True

    def test_is_available_false_when_command_none(self):
        scanner = V4l2Scanner(command=None)
        with patch('shutil.which', return_value=None):
            scanner2 = V4l2Scanner()
        assert scanner2.is_available() is False

    def test_get_device_names_returns_empty_when_no_command(self):
        scanner = V4l2Scanner(command=None)
        scanner.command = None
        assert scanner.get_device_names() == {}

    def test_get_device_names_parses_output(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = V4L2_LIST_DEVICES_OUTPUT
        with patch('subprocess.run', return_value=mock_result):
            names = scanner.get_device_names()
        assert names['/dev/video0'] == 'USB Camera'  # хвосты (usb-...) срезаны
        assert names['/dev/video2'] == 'Integrated Camera'

    def test_get_device_names_returns_empty_on_error(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ''
        mock_result.stderr = 'some error'
        with patch('subprocess.run', return_value=mock_result):
            assert scanner.get_device_names() == {}

    def test_strip_usb_path_removes_port(self):
        assert _strip_usb_path('HD Webcam: HD Webcam (usb-0000:00:14.0-10)') == 'HD Webcam: HD Webcam'

    def test_strip_usb_path_keeps_vid_pid(self):
        assert _strip_usb_path('UVC Camera (046d:0825) (usb-0000:00:14.0-1)') == 'UVC Camera (046d:0825)'

    def test_strip_usb_path_no_usb_unchanged(self):
        assert _strip_usb_path('Integrated Camera') == 'Integrated Camera'

    def test_get_device_names_identical_cameras_share_name(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        output = (
            'HD Webcam: HD Webcam (usb-0000:00:14.0-10):\n'
            '    /dev/video0\n'
            'HD Webcam: HD Webcam (usb-0000:00:14.0-11):\n'
            '    /dev/video2\n'
        )
        mock_result = MagicMock(returncode=0, stdout=output)
        with patch('subprocess.run', return_value=mock_result):
            names = scanner.get_device_names()
        # одинаковые камеры на разных портах → одно имя (общий кэш калибровки)
        assert names['/dev/video0'] == names['/dev/video2'] == 'HD Webcam: HD Webcam'

    def test_filter_capture_devices_returns_empty_when_no_command(self):
        scanner = V4l2Scanner(command=None)
        scanner.command = None
        assert scanner.filter_capture_devices({'/dev/video0': 'cam'}) == []

    def test_filter_capture_devices_includes_video_capture(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        ok_result = MagicMock(returncode=0, stdout=V4L2_ALL_OUTPUT_CAPTURE)
        with patch('subprocess.run', return_value=ok_result):
            result = scanner.filter_capture_devices({'/dev/video0': 'cam'})
        assert '/dev/video0' in result

    def test_filter_capture_devices_excludes_metadata_only(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        bad_result = MagicMock(returncode=0, stdout=V4L2_ALL_OUTPUT_NO_CAPTURE)
        with patch('subprocess.run', return_value=bad_result):
            result = scanner.filter_capture_devices({'/dev/video0': 'cam'})
        assert result == []

    def test_filter_capture_devices_skips_on_error(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        err_result = MagicMock(returncode=1, stdout='')
        with patch('subprocess.run', return_value=err_result):
            result = scanner.filter_capture_devices({'/dev/video0': 'cam'})
        assert result == []

    def test_parse_camera_params_returns_empty_when_no_command(self):
        scanner = V4l2Scanner(command=None)
        scanner.command = None
        assert scanner.parse_camera_params('/dev/video0') == set()

    def test_parse_camera_params_returns_empty_on_subprocess_error(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        with patch('subprocess.run', return_value=MagicMock(returncode=1)):
            assert scanner.parse_camera_params('/dev/video0') == set()

    def test_parse_camera_params_parses_yuyv_and_mjpg(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        mock_result = MagicMock(returncode=0, stdout=V4L2_FORMATS_OUTPUT)
        with patch('subprocess.run', return_value=mock_result):
            params = scanner.parse_camera_params('/dev/video0')
        assert (640, 480, 30, 'YUYV') in params
        assert (1280, 720, 10, 'YUYV') in params
        assert (1920, 1080, 30, 'MJPG') in params

    def test_parse_camera_params_returns_set(self):
        scanner = V4l2Scanner(command='/usr/bin/v4l2-ctl')
        mock_result = MagicMock(returncode=0, stdout=V4L2_FORMATS_OUTPUT)
        with patch('subprocess.run', return_value=mock_result):
            params = scanner.parse_camera_params('/dev/video0')
        assert isinstance(params, set)


#### CameraCalibrator ##################################################################
def _make_gst_mock(set_state_return, get_state_return):
    import mavixboard.gstreamer.camera as cam_module
    Gst = cam_module.Gst
    pipeline = MagicMock()
    Gst.parse_launch.return_value = pipeline
    pipeline.set_state.return_value = set_state_return
    pipeline.get_state.return_value = get_state_return
    return pipeline


class TestCameraCalibrator:
    def test_calibrate_adds_supported_param(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        pipeline = MagicMock()
        Gst.parse_launch.return_value = pipeline
        pipeline.set_state.return_value = 'not_failure'
        pipeline.get_state.return_value = (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PLAYING,
            None,
        )
        result = CameraCalibrator.calibrate(0, {(640, 480, 30, 'YUYV')})
        assert len(result) == 1
        assert result[0] == CameraParams(640, 480, 30, 'YUYV')

    def test_calibrate_skips_when_pipeline_fails_to_start(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        pipeline = MagicMock()
        Gst.parse_launch.return_value = pipeline
        pipeline.set_state.return_value = Gst.StateChangeReturn.FAILURE
        result = CameraCalibrator.calibrate(0, {(640, 480, 30, 'YUYV')})
        assert result == []

    def test_calibrate_skips_when_pipeline_not_playing(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        pipeline = MagicMock()
        Gst.parse_launch.return_value = pipeline
        pipeline.set_state.return_value = 'not_failure'
        pipeline.get_state.return_value = ('not_success', 'not_playing', None)
        result = CameraCalibrator.calibrate(0, {(640, 480, 30, 'YUYV')})
        assert result == []

    def test_calibrate_skips_duplicate_resolution_fps(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        pipeline = MagicMock()
        Gst.parse_launch.return_value = pipeline
        pipeline.set_state.return_value = 'not_failure'
        pipeline.get_state.return_value = (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PLAYING,
            None,
        )
        raw = {(640, 480, 30, 'YUYV'), (640, 480, 30, 'MJPG')}
        result = CameraCalibrator.calibrate(0, raw)
        assert len(result) == 1

    def test_calibrate_handles_exception_gracefully(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        Gst.parse_launch.side_effect = Exception('gst error')
        result = CameraCalibrator.calibrate(0, {(640, 480, 30, 'YUYV')})
        assert result == []
        Gst.parse_launch.side_effect = None

    def test_calibrate_stops_pipeline_in_finally(self):
        import mavixboard.gstreamer.camera as cam_module
        Gst = cam_module.Gst
        pipeline = MagicMock()
        Gst.parse_launch.return_value = pipeline
        pipeline.set_state.return_value = 'not_failure'
        pipeline.get_state.return_value = (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PLAYING,
            None,
        )
        CameraCalibrator.calibrate(0, {(640, 480, 30, 'YUYV')})
        pipeline.set_state.assert_any_call(Gst.State.NULL)


#### CameraRegistry ####################################################################
class TestCameraRegistry:
    def test_get_cameras_calls_scan(self, camera):
        scanner = MagicMock()
        calibrator = MagicMock()
        scanner.is_available.return_value = False
        registry = CameraRegistry(scanner=scanner, calibrator=calibrator)
        result = registry.get_cameras()
        assert result == []

    def test_get_cameras_uses_cache_on_second_call(self, camera, tmp_path):
        camera.save()
        scanner = MagicMock()
        scanner.is_available.return_value = True
        scanner.get_device_names.return_value = {'/dev/video0': 'Test Camera'}
        scanner.filter_capture_devices.return_value = ['/dev/video0']
        registry = CameraRegistry(scanner=scanner)
        registry.get_cameras()
        registry.get_cameras()
        assert scanner.get_device_names.call_count == 1

    def test_get_cameras_force_update_bypasses_cache(self, camera):
        scanner = MagicMock()
        scanner.is_available.return_value = False
        registry = CameraRegistry(scanner=scanner)
        registry.get_cameras()
        registry.get_cameras(force_update=True)
        assert scanner.is_available.call_count == 2

    def test_clear_cache(self, camera):
        scanner = MagicMock()
        scanner.is_available.return_value = False
        registry = CameraRegistry(scanner=scanner)
        registry.get_cameras()
        registry.clear_cache()
        registry.get_cameras()
        assert scanner.is_available.call_count == 2

    def test_scan_returns_empty_when_no_v4l2(self):
        scanner = MagicMock()
        scanner.is_available.return_value = False
        registry = CameraRegistry(scanner=scanner)
        assert registry._scan() == []

    def test_scan_uses_saved_camera_when_not_force(self, camera, tmp_path):
        camera.save()
        scanner = MagicMock()
        scanner.is_available.return_value = True
        scanner.get_device_names.return_value = {'/dev/video0': 'Test Camera'}
        scanner.filter_capture_devices.return_value = ['/dev/video0']
        registry = CameraRegistry(scanner=scanner)
        result = registry._scan(force_update=False)
        assert len(result) == 1
        assert result[0].name == 'Test Camera'
        scanner.parse_camera_params.assert_not_called()

    def test_scan_rescans_when_force_update(self, camera, tmp_path):
        camera.save()
        scanner = MagicMock()
        calibrator = MagicMock()
        calibrator.calibrate.return_value = [camera.params[0]]
        scanner.is_available.return_value = True
        scanner.get_device_names.return_value = {'/dev/video0': 'Test Camera'}
        scanner.filter_capture_devices.return_value = ['/dev/video0']
        scanner.parse_camera_params.return_value = {(640, 480, 30, 'YUYV')}
        registry = CameraRegistry(scanner=scanner, calibrator=calibrator)
        registry._scan(force_update=True)
        scanner.parse_camera_params.assert_called_once()


#### PipelineBuilder ###################################################################
class TestPipelineBuilder:
    def test_build_description_uses_stun_from_settings(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'stun://test-stun:3478' in desc

    def test_build_description_no_turn_when_empty(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'turn-server' not in desc

    def test_build_description_omits_turn_property_even_when_set(self, camera, monkeypatch):
        # TURN намеренно НЕ кладётся в строку пайплайна как property: он
        # регистрируется позже сигналом add-turn-server в PeerSession (иначе
        # webrtcbin отвергает дубль). Поэтому turn-server не должен попадать
        # в описание даже при заданном TURN_SERVER.
        monkeypatch.setattr(settings, 'turn_server', 'turn://user:pass@test:3478')
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'turn-server' not in desc

    def test_build_description_has_webrtcbin(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'webrtcbin name=webrtc' in desc

    def test_build_description_has_camera_branch(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'v4l2src device=/dev/video0' in desc

    def test_camera_branch_pt_increments(self, cam_params):
        cam0 = Camera(0, 'cam0', [cam_params], 0)
        cam1 = Camera(1, 'cam1', [cam_params], 0)
        desc = PipelineBuilder.build_pipeline_description([cam0, cam1])
        assert 'pt=96' in desc
        assert 'pt=97' in desc

    def test_camera_branch_uses_correct_resolution(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'width=640' in desc
        assert 'height=480' in desc
        assert 'framerate=30/1' in desc

    def test_camera_branch_uses_bitrate(self, camera):
        desc = PipelineBuilder.build_pipeline_description([camera])
        assert 'bitrate=2000' in desc

    def test_build_available_param_yuyv(self, cam_params):
        desc = PipelineBuilder.build_available_param(0, cam_params)
        assert 'video/x-raw,format=YUY2' in desc
        assert 'fakesink' in desc
        assert 'jpegdec' not in desc

    def test_build_available_param_mjpg(self):
        params = CameraParams(1920, 1080, 30, 'MJPG')
        desc = PipelineBuilder.build_available_param(0, params)
        assert 'image/jpeg' in desc
        assert 'jpegdec !' in desc

    def test_build_format_yuyv(self):
        assert PipelineBuilder._build_format('YUYV') == 'video/x-raw,format=YUY2'

    def test_build_format_mjpg(self):
        assert PipelineBuilder._build_format('MJPG') == 'image/jpeg'

    def test_build_format_unknown_defaults_to_raw(self):
        assert PipelineBuilder._build_format('H264') == 'video/x-raw'

    def test_build_decoder_mjpg(self):
        assert PipelineBuilder._build_decoder('MJPG') == 'jpegdec !'

    def test_build_decoder_other_returns_empty(self):
        assert PipelineBuilder._build_decoder('YUYV') == ''


#### GStreamerPipe #####################################################################
@pytest.fixture
def gst_pipe(camera):
    import mavixboard.gstreamer.gstreamer as gs_module
    Gst = gs_module.Gst
    GLib = gs_module.GLib

    mock_pipeline = MagicMock()
    mock_webrtc = MagicMock()
    mock_bus = MagicMock()
    mock_pipeline.get_by_name.return_value = mock_webrtc
    mock_pipeline.get_bus.return_value = mock_bus
    # start() блокирующе ждёт get_state и распаковывает 3 значения
    # (ret, state, pending) — отдаём корректный кортеж «успех + PLAYING».
    mock_pipeline.get_state.return_value = (
        Gst.StateChangeReturn.SUCCESS,
        Gst.State.PLAYING,
        None,
    )
    Gst.parse_launch.return_value = mock_pipeline

    with patch('mavixboard.gstreamer.pipeline.PipelineBuilder.build_pipeline_description', return_value='desc'):
        pipe = GStreamerPipe([camera])

    pipe._mock_pipeline = mock_pipeline
    pipe._mock_webrtc = mock_webrtc
    pipe._mock_bus = mock_bus
    return pipe


class TestGStreamerPipe:
    def test_start_sets_latency_and_playing(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        gst_pipe.start()
        gst_pipe._mock_webrtc.set_property.assert_called_once_with('latency', 0)
        gst_pipe._mock_pipeline.set_state.assert_called_with(Gst.State.PLAYING)

    def test_start_without_webrtc_elem(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        gst_pipe.webrtc_elem = None
        gst_pipe.start()
        gst_pipe._mock_pipeline.set_state.assert_called_with(Gst.State.PLAYING)

    def test_stop_sets_null_state(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        gst_pipe.stop()
        gst_pipe._mock_pipeline.set_state.assert_called_with(Gst.State.NULL)

    def test_stop_is_idempotent(self, gst_pipe):
        gst_pipe.stop()
        gst_pipe.stop()
        assert gst_pipe._mock_pipeline.set_state.call_count == 1

    def test_update_bitrate_returns_false_when_encoder_not_found(self, gst_pipe):
        gst_pipe._mock_pipeline.get_by_name.return_value = None
        assert gst_pipe.update_bitrate(0, 1000) is False

    def test_update_bitrate_returns_false_for_non_x264(self, gst_pipe):
        enc = MagicMock()
        enc.get_factory().get_name.return_value = 'vp8enc'
        gst_pipe._mock_pipeline.get_by_name.return_value = enc
        assert gst_pipe.update_bitrate(0, 1000) is False

    def test_update_bitrate_sets_property_and_returns_true(self, gst_pipe):
        enc = MagicMock()
        enc.get_factory().get_name.return_value = 'x264enc'
        gst_pipe._mock_pipeline.get_by_name.return_value = enc
        assert gst_pipe.update_bitrate(0, 2000) is True
        enc.set_property.assert_called_once_with('bitrate', 2000)

    def test_on_bus_message_handles_error(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        GLib = gs_module.GLib
        on_error = MagicMock()
        gst_pipe.on_error = on_error
        message = MagicMock()
        message.type = Gst.MessageType.ERROR
        message.parse_error.return_value = ('err msg', 'debug info')
        GStreamerPipe.on_bus_message(None, message, gst_pipe)
        GLib.idle_add.assert_called_with(on_error)

    def test_on_bus_message_handles_warning(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        message = MagicMock()
        message.type = Gst.MessageType.WARNING
        message.parse_warning.return_value = ('warn msg', 'debug info')
        result = GStreamerPipe.on_bus_message(None, message, gst_pipe)
        assert result is True

    def test_on_bus_message_handles_state_changed_to_playing(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        GLib = gs_module.GLib
        on_playing = MagicMock()
        gst_pipe.on_playing = on_playing
        message = MagicMock()
        message.type = Gst.MessageType.STATE_CHANGED
        message.src = gst_pipe.pipeline
        old_state = MagicMock()
        old_state.value_nick = 'null'
        message.parse_state_changed.return_value = (old_state, Gst.State.PLAYING, None)
        GStreamerPipe.on_bus_message(None, message, gst_pipe)
        GLib.idle_add.assert_called_with(on_playing)

    def test_on_bus_message_ignores_state_changed_from_other_elements(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        GLib = gs_module.GLib
        GLib.idle_add.reset_mock()
        on_playing = MagicMock()
        gst_pipe.on_playing = on_playing
        message = MagicMock()
        message.type = Gst.MessageType.STATE_CHANGED
        message.src = MagicMock()  # different element, not the pipeline
        GStreamerPipe.on_bus_message(None, message, gst_pipe)
        GLib.idle_add.assert_not_called()

    def test_on_bus_message_always_returns_true(self, gst_pipe):
        import mavixboard.gstreamer.gstreamer as gs_module
        Gst = gs_module.Gst
        message = MagicMock()
        message.type = 'unknown_type'
        result = GStreamerPipe.on_bus_message(None, message, gst_pipe)
        assert result is True
