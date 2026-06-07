from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import websockets

from mavixboard.core.backoff import ExponentialBackoff
from mavixboard.core.config import settings
from mavixboard.core.logger import logger
from mavixboard.fc.service import FCService
from mavixboard.gstreamer.camera import CameraSource, get_default_registry
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.gstreamer.watcher import CameraWatcher
from mavixboard.server.signal_client import SignalClient
from mavixboard.webrtc.manager import WebRTCManager


class SessionCoordinator:
    def __init__(
        self,
        signal_client: SignalClient,
        pipeline_factory: Callable[[], GStreamerPipe | None],
        fc_service: FCService | None = None,
        watcher: CameraWatcher | None = None,
        backoff: ExponentialBackoff | None = None,
        camera_source: CameraSource | None = None,
    ) -> None:
        self._signal_client = signal_client
        self._pipeline_factory = pipeline_factory
        self._fc_service = fc_service
        self._watcher = watcher
        self._backoff = backoff if backoff is not None else ExponentialBackoff()
        self._cameras: CameraSource = camera_source if camera_source is not None else get_default_registry()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pipeline: GStreamerPipe | None = None
        self._manager: WebRTCManager | None = None
        self._stop_event: asyncio.Event | None = None
        # Накопитель GPS/курса: GPS-кадры и ATTITUDE приходят разными
        # сообщениями, объединяем их в одно telemetry-сообщение оператору.
        self._last_telemetry: dict = {}

    #### Жизненный цикл ####################################################################
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            connected = await self._signal_client.connect()
            if not connected:
                delay = self._backoff.next_delay()
                logger.info('[coord] не удалось подключиться, повтор через %.1fs', delay)
                await asyncio.sleep(delay)
                continue
            logger.info('[coord] подключено к сигнальному серверу')
            self._backoff.reset()
            try:
                await self._signal_client.listen(self._on_message)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning('[coord] сигнальное соединение закрыто: %s', exc)
            except Exception as exc:
                logger.error('[coord] неожиданная ошибка в listen: %s', exc)
            finally:
                self._teardown()
                await self._signal_client.disconnect()
            delay = self._backoff.next_delay()
            await asyncio.sleep(delay)

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self._teardown()
        if self._loop is not None:
            self._loop.create_task(self._signal_client.disconnect())

    def _teardown(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self._manager is not None:
            self._manager.end_session()
            self._manager = None
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            # stop() = set_state(NULL): освобождает v4l2-fd, отрабатывает
            # быстро и синхронно — следующая сборка пайплайна сразу получит
            # камеру.
            try:
                pipeline.stop()
            except Exception as exc:
                logger.warning('[coord] ошибка остановки пайплайна: %s', exc)
            # Финальный unref webrtcbin (разборка ICE-агента libnice) может
            # заблокироваться в gupnp-igd, если UPnP отключить не удалось. Чтобы
            # возможная блокировка НЕ повесила event-loop board, роняем
            # GObject-ссылки в отдельном daemon-потоке — там же отработает и
            # сборщик мусора обёртки. См. GStreamerPipe._disable_upnp.
            threading.Thread(
                target=self._release_pipeline, args=(pipeline,),
                name='pipeline-release', daemon=True,
            ).start()
        if self._fc_service is not None:
            self._fc_service.set_change_callback(None)
            self._fc_service.set_telemetry_callback(None)

    @staticmethod
    def _release_pipeline(pipeline: GStreamerPipe) -> None:
        """Роняет GObject-ссылки пайплайна вне потока asyncio-loop.

        Обнуление .pipeline/.webrtc_elem снимает последние Python-ссылки на
        GStreamer-элементы → их финальный unref происходит в этом одноразовом
        потоке, а не на event-loop. При отключённом UPnP отрабатывает мгновенно.
        """
        try:
            pipeline.webrtc_elem = None
            pipeline.pipeline = None  # type: ignore[assignment]
        except Exception as exc:
            logger.debug('[coord] release_pipeline: %s', exc)

    #### Колбэки FC ########################################################################
    def _on_fc_change(self, kind: str | None, name: str) -> None:
        """Срабатывает из FCService при появлении/исчезновении FC-контроллера.

        Отправляет свежее `fc`-сообщение в config-канал, чтобы UI GCS
        обновился без ожидания следующего перезапуска сессии.
        """
        if self._manager is None:
            return
        try:
            self._manager.notify_fc_changed()
        except Exception as exc:
            logger.warning('[coord] ошибка notify_fc_changed: %s', exc)

    def _on_fc_telemetry(self, decoded: dict) -> None:
        """Вызывается из read-циклов CRSF / MAVLink для каждого декодированного
        кадра телеметрии.

        Пробрасываем только сообщения, которые GCS реально отрисовывает
        """
        if self._manager is None or self._manager.channels is None:
            return
        kind = decoded.get('type')
        if kind in ('gps', 'attitude'):
            self._forward_telemetry(decoded)
            return
        cfg = self._manager.channels.config
        if cfg is None:
            return
        if kind == 'battery':
            try:
                cfg.send_json({
                    'type': 'battery',
                    'percent': int(decoded.get('remaining', 0)),
                    'voltage': float(decoded.get('voltage', 0.0)),
                    'current': float(decoded.get('current', 0.0)),
                })
            except Exception as exc:
                logger.debug('[coord] ошибка проброса battery: %s', exc)
        elif kind == 'command_ack':
            try:
                cfg.send_json({
                    'type': 'command_ack',
                    'command': decoded.get('command_name', ''),
                    'result': decoded.get('result_name', ''),
                })
            except Exception as exc:
                logger.debug('[coord] ошибка проброса command_ack: %s', exc)
        elif kind == 'heartbeat':
            try:
                cfg.send_json({
                    'type': 'fc_armed',
                    'armed': bool(decoded.get('armed', False)),
                    'custom_mode': int(decoded.get('custom_mode', 0)),
                })
            except Exception as exc:
                logger.debug('[coord] ошибка проброса fc_armed: %s', exc)

    def _forward_telemetry(self, decoded: dict) -> None:
        """Объединяет GPS- и ATTITUDE-кадры и шлёт их по telemetry-каналу.

        GPS даёт координаты, высоту, спутники и (если есть) курс из вектора
        движения; ATTITUDE даёт курс из yaw гироскопа. Курс из ATTITUDE
        обновляем всегда, а GPS-курс не затираем нулём.
        """
        if self._manager is None or self._manager.channels is None:
            return
        tel = getattr(self._manager.channels, 'telemetry', None)
        if tel is None:
            return
        kind = decoded.get('type')
        try:
            if kind == 'gps':
                lat = float(decoded.get('lat', 0.0))
                lon = float(decoded.get('lon', 0.0))
                # Нет GPS-фикса: полётник до захвата спутников шлёт нулевые
                # координаты (0°,0° — «Null Island» у берегов Африки). Не
                # сохраняем и не пробрасываем такую позицию — иначе дрон
                # «телепортируется» в океан. Признак — нулевые lat/lon
                # (кросс-протокольно: MAVLink GLOBAL_POSITION_INT не несёт
                # число спутников, поэтому по sats гейтить нельзя). ATTITUDE-
                # курс при этом обновляется отдельной веткой.
                if abs(lat) < 1e-6 and abs(lon) < 1e-6:
                    return
                self._last_telemetry['lat'] = lat
                self._last_telemetry['lon'] = lon
                self._last_telemetry['alt'] = float(decoded.get('alt', 0.0))
                self._last_telemetry['sats'] = int(decoded.get('sats', 0))
                heading = float(decoded.get('heading', 0.0))
                if heading:
                    self._last_telemetry['heading'] = heading
            elif kind == 'attitude':
                self._last_telemetry['heading'] = float(decoded.get('heading', 0.0))
            if 'lat' not in self._last_telemetry or 'lon' not in self._last_telemetry:
                return
            tel.send_json({
                'type': 'telemetry',
                'lat': self._last_telemetry['lat'],
                'lon': self._last_telemetry['lon'],
                'alt': self._last_telemetry.get('alt', 0.0),
                'heading': self._last_telemetry.get('heading', 0.0),
                'sats': self._last_telemetry.get('sats', 0),
            })
        except Exception as exc:
            logger.debug('[coord] ошибка проброса telemetry: %s', exc)

    #### Диспетчеризация сигналинга ########################################################
    async def _on_message(self, msg: dict) -> None:
        kind = msg.get('type')
        match kind:
            case 'connect':
                await self._handle_connect(msg.get('gcs_id'))
            case 'disconnect':
                logger.info('[coord] сервер сообщает об отключении GCS')
                self._teardown()
            case 'sdp':
                await self._handle_sdp(msg)
            case 'ice':
                await self._handle_ice(msg)
            case 'error':
                logger.warning('[coord] ошибка сервера: %s', msg.get('message'))
            case 'ping':
                await self._signal_client.send({'type': 'pong'})
            case 'pong':
                pass
            case _:
                logger.warning('[coord] неизвестный тип сообщения: %s', kind)

    async def _handle_connect(self, gcs_id: str | None) -> None:
        if not isinstance(gcs_id, str) or not gcs_id:
            logger.warning('[coord] connect с некорректным gcs_id')
            return
        if self._pipeline is not None:
            logger.info('[coord] активна предыдущая сессия, завершаем перед новой')
            self._teardown()
        pipeline = await asyncio.to_thread(self._pipeline_factory)
        if pipeline is None or pipeline.webrtc_elem is None:
            logger.error('[coord] фабрика пайплайна не вернула пайплайн, прерываем сессию')
            try:
                await self._signal_client.send({'type': 'disconnect_session'})
            except Exception as exc:
                logger.warning('[coord] ошибка отправки disconnect_session: %s', exc)
            return
        assert self._loop is not None
        self._pipeline = pipeline
        pipeline.on_error = self._on_pipeline_error
        self._manager = WebRTCManager(
            pipeline.webrtc_elem,
            self._loop,
            self._signal_client.send,
            fc_service=self._fc_service,
        )
        if not await asyncio.to_thread(pipeline.start):
            logger.error('[coord] пайплайн не достиг PLAYING, прерываем сессию')
            try:
                pipeline.stop()
            except Exception as exc:
                logger.debug('[coord] ошибка остановки пайплайна после неудачного старта: %s', exc)
            self._pipeline = None
            self._manager = None
            self._cameras.clear_cache()
            return
        self._manager.start_session(gcs_id, cameras=pipeline.cameras)
        if self._manager.channels is not None:
            self._manager.channels.config.on_message = self._on_config_message
        if self._fc_service is not None:
            self._fc_service.set_change_callback(self._on_fc_change)
            self._fc_service.set_telemetry_callback(self._on_fc_telemetry)
        if self._watcher is not None:
            initial_ids = {cam.device_index for cam in pipeline.cameras}
            self._watcher.start(initial_ids, self._on_cameras_changed)

    async def _handle_sdp(self, msg: dict) -> None:
        if self._manager is None:
            return
        gcs_id = msg.get('gcs_id')
        sdp = msg.get('sdp')
        if isinstance(gcs_id, str) and isinstance(sdp, dict):
            await self._manager.handle_sdp(gcs_id, sdp)

    async def _handle_ice(self, msg: dict) -> None:
        if self._manager is None:
            return
        gcs_id = msg.get('gcs_id')
        cand = msg.get('candidate')
        if isinstance(gcs_id, str) and isinstance(cand, dict):
            await self._manager.handle_ice(gcs_id, cand)

    #### Обработка config-канала ###########################################################
    def _on_config_message(self, payload: dict | list) -> None:
        if not isinstance(payload, dict):
            logger.warning('[coord] config-сообщение должно быть объектом, получено %s', type(payload).__name__)
            return
        match payload.get('type'):
            case 'calibrate':
                self._force_calibrate()
            case 'bitrate':
                self._apply_bitrate_updates(payload.get('updates', []))
            case 'params':
                self._apply_params_updates(payload.get('updates', []))
            case _:
                logger.warning('[coord] неизвестный тип config-сообщения: %s', payload.get('type'))

    def _apply_params_updates(self, updates: object) -> None:
        """Сохраняет новый param_index для каждой камеры, затем завершает сессию.

        Resolution/FPS зашиваются в caps GStreamer на этапе сборки пайплайна;
        чисто поменять их на работающем пайплайне нельзя. Сохранение + сброс
        пира заставляют GCS переподключиться, а следующая сборка пайплайна
        подхватывает новый param_index из Camera.get в CameraRegistry._scan.
        """
        if not isinstance(updates, list) or self._pipeline is None:
            return
        cameras = self._pipeline.cameras
        changed = False
        for update in updates:
            if not isinstance(update, dict):
                continue
            device_index = update.get('device_index')
            param_index = update.get('param_index')
            if not isinstance(device_index, int) or not isinstance(param_index, int):
                continue
            cam = next((c for c in cameras if c.device_index == device_index), None)
            if cam is None:
                logger.warning('[coord] params-обновление для неизвестного device_index=%s', device_index)
                continue
            if param_index < 0 or param_index >= len(cam.params):
                logger.warning('[coord] params-обновление с param_index=%s вне диапазона для device_index=%s',
                               param_index, device_index)
                continue
            if cam.param_index == param_index:
                continue
            cam.param_index = param_index
            try:
                cam.save()
            except Exception as exc:
                logger.warning('[coord] ошибка сохранения камеры: %s', exc)
            changed = True
        if not changed:
            return
        logger.info('[coord] параметры изменились, завершаем сессию для пересогласования')
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    def _apply_bitrate_updates(self, updates: object) -> None:
        if not isinstance(updates, list) or self._pipeline is None:
            return
        cameras = self._pipeline.cameras
        for update in updates:
            if not isinstance(update, dict):
                continue
            device_index = update.get('device_index')
            bitrate_kbs = update.get('bitrate_kbs')
            if not isinstance(device_index, int) or not isinstance(bitrate_kbs, int):
                continue
            pipe_idx = next(
                (i for i, cam in enumerate(cameras) if cam.device_index == device_index),
                None,
            )
            if pipe_idx is None:
                logger.warning('[coord] bitrate-обновление для неизвестного device_index=%s', device_index)
                continue
            self._pipeline.update_bitrate(pipe_idx, bitrate_kbs)
            cameras[pipe_idx].bitrate_kbs = bitrate_kbs
            try:
                cameras[pipe_idx].save()
            except Exception as exc:
                logger.warning('[coord] ошибка сохранения камеры: %s', exc)

    def _force_calibrate(self) -> None:
        """Удаляет сохранённые калибровки камер и завершает сессию, чтобы
        следующая сборка пайплайна заново прогнала полный цикл зондирования
        GStreamer для каждого устройства.

        GCS автоматически переподключается → _build_pipeline → _scan;
        Camera.get возвращает None для каждого устройства (.json удалён) → уходит
        на CameraCalibrator.calibrate.
        """
        logger.info('[coord] запрошена полная перекалибровка через config-канал')
        cameras = list(self._pipeline.cameras) if self._pipeline is not None else []
        if not cameras:
            cameras = self._cameras.get_cameras()
        for cam in cameras:
            path = settings.data_path / f'{cam.name}.json'
            try:
                path.unlink(missing_ok=True)
                logger.info('[coord] удалена сохранённая калибровка: %s', path)
            except OSError as exc:
                logger.warning('[coord] не удалось удалить %s: %s', path, exc)
        self._cameras.clear_cache()
        if self._manager is None:
            return
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    #### События камер и пайплайна #########################################################
    def _on_cameras_changed(self, new_ids: set[int]) -> None:
        logger.info('[coord] набор камер изменился: %s, завершаем сессию', sorted(new_ids))
        self._cameras.clear_cache()
        if self._manager is None:
            return
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    def _on_pipeline_error(self) -> None:
        """Вызывается bus watch у GStreamerPipe, когда пайплайн шлёт ERROR.

        Полностью сбрасывает сессию пира. Новый пайплайн соберётся при
        переподключении GCS (сервер снова шлёт 'connect').
        """
        logger.warning('[coord] ошибка пайплайна, завершаем сессию')
        # На практике самая частая ошибка пайплайна — падение чтения v4l2
        # из-за того, что камеру выдернули
        self._cameras.clear_cache()
        if self._manager is None:
            return
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()
