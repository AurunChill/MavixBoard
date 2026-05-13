import dataclasses
import json
import shutil
import subprocess
from dataclasses import dataclass

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from mavixboard.gstreamer.pipeline import PipelineBuilder
from mavixboard.core.config import settings
from mavixboard.core.logger import logger


@dataclass
class CameraParams:
    width: int
    height: int
    fps: int
    format: str  # now YUYV and MJPG is available


@dataclass
class Camera:
    device_index: int
    name: str
    params: list[CameraParams]
    param_index: int
    bitrate_kbs: int = 1000

    def save(self) -> None:
        path = settings.data_path / f'{self.name}.json'
        path.write_text(json.dumps(dataclasses.asdict(self), indent=2))

    @classmethod
    def get(cls, name: str) -> 'Camera | None':
        path = settings.data_path / f'{name}.json'
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            data['params'] = [CameraParams(**p) for p in data['params']]
            return cls(**data)
        except (ValueError, OSError, KeyError, TypeError):
            return None


class V4l2Scanner:
    def __init__(self, command: str | None = None) -> None:
        self.command: str | None = command if command is not None else shutil.which('v4l2-ctl')

    def is_available(self) -> bool:
        return self.command is not None

    def get_device_names(self) -> dict[str, str]:
        if not self.command:
            return {}
        result = subprocess.run([self.command, '--list-devices'], capture_output=True, text=True)
        if result.returncode != 0 and '/dev/video' not in result.stderr:
            logger.error(f'USB camera list error: {result.stderr}')
            return {}
        device_names: dict[str, str] = {}
        cur_name: str | None = None
        for line in result.stdout.split('\n'):
            line = line.rstrip()
            if line and not line.startswith('\t') and not line.startswith(' '):
                cur_name = line.strip().rstrip(':')
            elif line.strip().startswith('/dev/video') and cur_name:
                device_path = line.strip()
                device_names[device_path] = cur_name
        return device_names

    def filter_capture_devices(self, device_names: dict[str, str]) -> list[str]:
        if not self.command:
            return []
        capture_devices: list[str] = []
        for device_path in device_names.keys():
            result = subprocess.run([self.command, '-d', device_path, '--all'], capture_output=True, text=True)
            if result.returncode != 0:
                continue
            in_device_caps = False
            for line in result.stdout.split('\n'):
                if 'Device Caps' in line:
                    in_device_caps = True
                elif in_device_caps:
                    if 'Video Capture' in line and 'Metadata' not in line:
                        capture_devices.append(device_path)
                        break
                    elif line and not line.startswith('\t') and not line.startswith(' '):
                        break
        return capture_devices

    def parse_camera_params(self, device_path: str) -> set[tuple[int, int, int, str]]:
        if not self.command:
            return set()
        result = subprocess.run([self.command, '-d', device_path, '--list-formats-ext'], capture_output=True, text=True)
        if result.returncode != 0:
            return set()
        params: set[tuple[int, int, int, str]] = set()
        width: int | None = None
        height: int | None = None
        fps: int | None = None
        format_: str | None = None
        for line in result.stdout.split('\n'):
            try:
                if all(char in line for char in ['[', ']']):
                    format_ = line.split(' ')[1].strip("'")
                    fps, width, height = None, None, None
                if 'Size' in line:
                    width, height = [int(num) for num in line.split(' ')[-1].split('x')]
                elif 'Interval' in line:
                    fps = int(float(line.split(' ')[-2].lstrip('(')))
                if width and height and fps and format_:
                    params.add((width, height, fps, format_))
            except ValueError:
                continue
        return params


class CameraCalibrator:
    @staticmethod
    def calibrate(device_index: int, raw_params: set[tuple[int, int, int, str]]) -> list[CameraParams]:
        logger.info('Starting camera calibration')
        supported_params: list[CameraParams] = []
        seen: set[tuple[int, int, int]] = set()
        for width, height, fps, format_ in raw_params:
            key = (width, height, fps)
            if key in seen:
                continue
            camera_param = CameraParams(width=width, height=height, fps=fps, format=format_)
            pipeline = None
            try:
                pipeline_desc = PipelineBuilder.build_available_param(cam_index=device_index, params=camera_param)
                pipeline = Gst.parse_launch(pipeline_desc)
                result = pipeline.set_state(Gst.State.PLAYING)
                if result == Gst.StateChangeReturn.FAILURE:
                    continue
                ret, state, _ = pipeline.get_state(Gst.SECOND * 2)
                if ret == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING:
                    seen.add(key)
                    supported_params.append(camera_param)
            except Exception as e:
                logger.debug(f"Failed {width}x{height}@{fps} ({format_}): {e}")
            finally:
                if pipeline:
                    pipeline.set_state(Gst.State.NULL)
                    pipeline.get_state(Gst.SECOND * 1)  # wait for v4l2 fd release
        return supported_params


class CameraRegistry:
    def __init__(
        self,
        scanner: V4l2Scanner | None = None,
        calibrator: CameraCalibrator | None = None,
    ) -> None:
        self.scanner: V4l2Scanner = scanner if scanner is not None else V4l2Scanner()
        self.calibrator: CameraCalibrator = calibrator if calibrator is not None else CameraCalibrator()
        self.cached_cams_: list[Camera] | None = None

    def clear_cache(self) -> None:
        self.cached_cams_ = None

    def get_cameras(self, force_update: bool = False) -> list[Camera]:
        if not force_update and self.cached_cams_:
            return self.cached_cams_
        cameras = self._scan(force_update=force_update)
        for camera in cameras:
            camera.save()
        self.cached_cams_ = cameras
        return cameras

    def get_by_index(self, index: int) -> Camera | None:
        return next((cam for cam in self.get_cameras() if cam.device_index == index), None)

    def _scan(self, force_update: bool = False) -> list[Camera]:
        if not self.scanner.is_available():
            logger.warning('v4l-utils is not installed')
            return []
        logger.warning('Calibration is starting!')
        device_names = self.scanner.get_device_names()
        capture_devices = self.scanner.filter_capture_devices(device_names)
        cameras: list[Camera] = []
        for device_path in capture_devices:
            device_index = int(device_path.split('video')[1])
            name = device_names[device_path]
            saved = Camera.get(name)
            if saved and not force_update:
                # The saved JSON has the device_index from a previous run;
                # after a USB unplug+replug the kernel may reassign the
                # camera to a different /dev/videoN. Refresh the index to
                # the *current* path so the pipeline opens the right node,
                # and persist so subsequent loads have the up-to-date value.
                if saved.device_index != device_index:
                    saved.device_index = device_index
                    try:
                        saved.save()
                    except OSError as exc:
                        logger.warning('camera save error: %s', exc)
                cameras.append(saved)
                continue
            raw_params = self.scanner.parse_camera_params(device_path)
            if not raw_params:
                continue
            supported_params = self.calibrator.calibrate(device_index, raw_params)
            if supported_params:
                param_index = saved.param_index if saved and saved.param_index < len(supported_params) else len(supported_params) // 2
                bitrate_kbs = saved.bitrate_kbs if saved else 2500
                cameras.append(Camera(
                    device_index=device_index,
                    name=name,
                    params=supported_params,
                    param_index=param_index,
                    bitrate_kbs=bitrate_kbs
                ))
        return cameras


_default_registry: CameraRegistry | None = None


def get_default_registry() -> CameraRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = CameraRegistry()
    return _default_registry


class CameraManager:
    @staticmethod
    def clear_cache() -> None:
        get_default_registry().clear_cache()

    @staticmethod
    def get_cameras(force_update: bool = False) -> list[Camera]:
        return get_default_registry().get_cameras(force_update=force_update)

    @staticmethod
    def get_by_index(index: int) -> Camera | None:
        return get_default_registry().get_by_index(index)

    @staticmethod
    def _get_device_names(command: str) -> dict[str, str]:
        return V4l2Scanner(command=command).get_device_names()

    @staticmethod
    def _parse_camera_params(command: str, device_path: str) -> set[tuple[int, int, int, str]]:
        return V4l2Scanner(command=command).parse_camera_params(device_path)
