"""Сканирование и калибровка USB-камер через v4l2 и пробные GStreamer-пайплайны."""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

from mavixboard.core.config import settings
from mavixboard.core.logger import logger
from mavixboard.gstreamer.pipeline import PipelineBuilder


#### Структуры данных ##################################################################
@dataclass
class CameraParams:
    width: int
    height: int
    fps: int
    format: str  # сейчас доступны YUYV и MJPG


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
    def get(cls, name: str) -> Camera | None:
        path = settings.data_path / f'{name}.json'
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            data['params'] = [CameraParams(**p) for p in data['params']]
            return cls(**data)
        except (ValueError, OSError, KeyError, TypeError):
            return None


#### Сканер v4l2 #######################################################################
_USB_PATH_RE = re.compile(r'\s*\(usb-[^)]*\)')


def _strip_usb_path(name: str) -> str:
    """Убирает из имени камеры хвост `(usb-...)` — путь USB-порта.

    Имя служит ключом кэша калибровки. Без USB-пути калибровка привязана к
    самой камере (модели), а не к конкретному порту, поэтому перетыкание в
    другой USB-порт не вызывает рекалибровку. Card name и VID:PID при этом
    сохраняются — две камеры одной модели на разных портах дадут одно имя.
    """
    return _USB_PATH_RE.sub('', name).strip()


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
            logger.error('[camera] ошибка получения списка USB-камер: %s', result.stderr)
            return {}
        device_names: dict[str, str] = {}
        cur_name: str | None = None
        for line in result.stdout.split('\n'):
            line = line.rstrip()
            if line and not line.startswith('\t') and not line.startswith(' '):
                cur_name = _strip_usb_path(line.strip().rstrip(':'))
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


#### Калибратор ########################################################################
class CameraCalibrator:
    @staticmethod
    def calibrate(device_index: int, raw_params: set[tuple[int, int, int, str]]) -> list[CameraParams]:
        logger.info('[camera] запускаю калибровку камеры')
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
            except Exception as exc:
                logger.debug('[camera] не удалось %dx%d@%d (%s): %s',
                             width, height, fps, format_, exc)
            finally:
                if pipeline:
                    pipeline.set_state(Gst.State.NULL)
                    pipeline.get_state(Gst.SECOND * 1)  # ждём освобождения v4l2 fd
        return supported_params


#### Реестр камер ######################################################################
class CameraSource(Protocol):
    """Источник камер с кэшем — абстракция для инъекции в координатор.

    Структурно реализуется CameraRegistry. Позволяет SessionCoordinator не
    зависеть от глобального синглтона и подменять источник (фейк в тестах,
    демо-камеры) — Dependency Inversion.
    """

    def get_cameras(self, force_update: bool = False) -> list[Camera]: ...

    def clear_cache(self) -> None: ...


class CameraRegistry:
    def __init__(
        self,
        scanner: V4l2Scanner | None = None,
        calibrator: CameraCalibrator | None = None,
    ) -> None:
        self.scanner: V4l2Scanner = scanner if scanner is not None else V4l2Scanner()
        self.calibrator: CameraCalibrator = calibrator if calibrator is not None else CameraCalibrator()
        self._cached_cams: list[Camera] | None = None

    def clear_cache(self) -> None:
        self._cached_cams = None

    def get_cameras(self, force_update: bool = False) -> list[Camera]:
        if not force_update and self._cached_cams:
            return self._cached_cams
        cameras = self._scan(force_update=force_update)
        for camera in cameras:
            camera.save()
        self._cached_cams = cameras
        return cameras

    def _scan(self, force_update: bool = False) -> list[Camera]:
        if not self.scanner.is_available():
            logger.warning('[camera] v4l-utils не установлен')
            return []
        logger.warning('[camera] начинается калибровка!')
        device_names = self.scanner.get_device_names()
        capture_devices = self.scanner.filter_capture_devices(device_names)
        cameras: list[Camera] = []
        for device_path in capture_devices:
            device_index = int(device_path.split('video')[1])
            name = device_names[device_path]
            saved = Camera.get(name)
            if saved and not force_update:
                # В сохранённом JSON лежит device_index с прошлого запуска;
                # после переподключения USB ядро может назначить камере
                # другой /dev/videoN. Обновляем индекс на *текущий* путь,
                # чтобы пайплайн открыл нужный узел, и сохраняем, чтобы
                # последующие загрузки имели актуальное значение.
                if saved.device_index != device_index:
                    saved.device_index = device_index
                    try:
                        saved.save()
                    except OSError as exc:
                        logger.warning('[camera] ошибка сохранения камеры: %s', exc)
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


#### Дефолтный реестр камер ############################################################
# Ленивый синглтон: один общий CameraRegistry на процесс, то есть общий кэш
# камер (источник правды). Создаётся при первом обращении, а НЕ инстанцируется
# на уровне модуля (`= CameraRegistry()`) намеренно: конструктор тянет
# V4l2Scanner → shutil.which('v4l2-ctl'), и не хочется дёргать ФС на импорте.
# Так импорт остаётся без side-effect'ов, регистр строится только если реально
# нужен, и тестам проще (объект не создаётся на каждый import модуля).
_default_registry: CameraRegistry | None = None


def get_default_registry() -> CameraRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = CameraRegistry()
    return _default_registry
