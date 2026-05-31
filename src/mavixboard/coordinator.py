from __future__ import annotations

import asyncio
from collections.abc import Callable

import websockets

from mavixboard.core.backoff import ExponentialBackoff
from mavixboard.core.config import settings
from mavixboard.core.logger import logger
from mavixboard.fc.service import FCService
from mavixboard.gstreamer.camera import CameraManager
from mavixboard.gstreamer.gstreamer import GStreamerPipe
from mavixboard.gstreamer.watcher import CameraWatcher
from mavixboard.server.signal_client import SignalClient
from mavixboard.webrtc.manager import WebRTCManager

PipelineFactory = Callable[[], GStreamerPipe | None]


class SessionCoordinator:
    def __init__(
        self,
        signal_client: SignalClient,
        pipeline_factory: PipelineFactory,
        fc_service: FCService | None = None,
        watcher: CameraWatcher | None = None,
        backoff: ExponentialBackoff | None = None,
    ) -> None:
        self._signal_client = signal_client
        self._pipeline_factory = pipeline_factory
        self._fc_service = fc_service
        self._watcher = watcher
        self._backoff = backoff if backoff is not None else ExponentialBackoff()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pipeline: GStreamerPipe | None = None
        self._manager: WebRTCManager | None = None
        self._stop_event: asyncio.Event | None = None

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
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception as exc:
                logger.warning('[coord] ошибка остановки пайплайна: %s', exc)
            self._pipeline = None
        if self._fc_service is not None:
            self._fc_service.set_change_callback(None)

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

        Пробрасываем только сообщения, которые GCS реально отрисовывает:
        battery (оверлей) и command_ack (лог отладки arm). Высокочастотное
        (attitude / gps) насытило бы JSON-канал; при необходимости позже оно
        пойдёт через бинарный packet-канал.
        """
        if self._manager is None or self._manager.channels is None:
            return
        cfg = self._manager.channels.config
        if cfg is None:
            return
        kind = decoded.get('type')
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
        # pipeline_factory может крутить многосекундный цикл калибровки
        # GStreamer; pipeline.start блокируется на get_state(PLAYING). Оба
        # синхронны и НЕ должны выполняться прямо в asyncio loop — иначе
        # _pump_offer не отправит SDP, а signal_client.listen не подтвердит
        # ping, и сервер выкинет WS дрона по WS_PING_TIMEOUT (45s), решив, что
        # дрон умер. Запускаем их через asyncio.to_thread, чтобы не занимать loop.
        pipeline = await asyncio.to_thread(self._pipeline_factory)
        if pipeline is None or pipeline.webrtc_elem is None:
            logger.error('[coord] фабрика пайплайна не вернула пайплайн, прерываем сессию')
            # Нет камер / пайплайн сломан: просим сервер сбросить пару пиров,
            # чтобы GCS быстро получил drone_disconnected, а не ждал SDP, который
            # никогда не придёт до таймаута WS-ping.
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
        # Ждём, пока пайплайн реально перейдёт в PLAYING, прежде чем создавать
        # data-каналы. Если v4l2_open падает (камеру выдернули между сканом и
        # pipeline.start, /dev/videoN ещё не готов после udev), шина шлёт ERROR,
        # который планирует _on_pipeline_error в потоке GLib. Запуск этого
        # параллельно с manager.start_session уронил бы
        # `gst_webrtc_bin_create_data_channel: is_closed != TRUE`, потому что
        # webrtcbin сносится во время создания каналов.
        if not await asyncio.to_thread(pipeline.start):
            logger.error('[coord] пайплайн не достиг PLAYING, прерываем сессию')
            try:
                pipeline.stop()
            except Exception as exc:
                logger.debug('[coord] ошибка остановки пайплайна после неудачного старта: %s', exc)
            self._pipeline = None
            self._manager = None
            CameraManager.clear_cache()
            return
        self._manager.start_session(gcs_id, cameras=pipeline.cameras)
        if self._manager.channels is not None:
            self._manager.channels.config.on_message = self._on_config_message
        if self._fc_service is not None:
            # FCService срабатывает каждый раз, когда FC-контроллер появляется
            # или исчезает посреди сессии (например, пользователь подключил FC
            # уже после поднятия WebRTC-сессии). manager._send_fc_info
            # выполняется лишь раз при открытии data-канала — без повторного
            # вызова здесь GCS никогда не узнал бы о горячем подключении FC.
            self._fc_service.set_change_callback(self._on_fc_change)
            # Декодированные кадры телеметрии (battery / attitude / flight_mode)
            # — координатор выбирает те, что нужны UI, и пробрасывает компактным
            # JSON через config data-канал.
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
        if self._manager is not None and self._manager.channels is not None:
            cfg = self._manager.channels.config
            if cfg is not None:
                cfg.send_json({'type': 'cameras_changed',
                               'device_indices': sorted(c.device_index for c in cameras)})
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
            cameras = CameraManager.get_cameras()
        for cam in cameras:
            path = settings.data_path / f'{cam.name}.json'
            try:
                path.unlink(missing_ok=True)
                logger.info('[coord] удалена сохранённая калибровка: %s', path)
            except OSError as exc:
                logger.warning('[coord] не удалось удалить %s: %s', path, exc)
        CameraManager.clear_cache()
        if self._manager is None:
            return
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    def _on_cameras_changed(self, new_ids: set[int]) -> None:
        logger.info('[coord] набор камер изменился: %s, завершаем сессию', sorted(new_ids))
        # СНАЧАЛА инвалидируем in-memory кэш камер, до любого short-circuit ниже:
        # _on_pipeline_error мог уже снести manager (камеру выдернули → чтение
        # v4l2 падает → ошибка пайплайна срабатывает раньше 5-секундного опроса
        # watcher). Если clear_cache спрятать за проверкой ненулевого manager,
        # следующая сборка пайплайна увидит устаревший кэш, попробует снова
        # открыть выдернутое устройство, опять упадёт, и сессия зациклится без
        # восстановления. _scan переиспользует *.json на диске для любого
        # устройства, чьё имя ещё совпадает; перекалибруются только реально
        # новые устройства.
        CameraManager.clear_cache()
        if self._manager is None:
            return
        if self._manager.channels is not None:
            self._manager.channels.config.send_json({
                'type': 'cameras_changed',
                'device_indices': sorted(new_ids),
            })
        # Просим сервер сбросить пару пиров и уведомить GCS, затем завершаем
        # локально. Новый пайплайн соберётся при следующем 'connect' от сервера
        # с актуальными камерами.
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    def _on_pipeline_error(self) -> None:
        """Вызывается bus watch у GStreamerPipe, когда пайплайн шлёт ERROR.

        Сначала отправляет уведомление об ошибке в GCS через config-канал,
        чтобы UI мог сообщить пилоту, затем полностью сбрасывает сессию пира.
        Новый пайплайн соберётся при переподключении GCS (сервер снова шлёт
        'connect').
        """
        logger.warning('[coord] ошибка пайплайна, завершаем сессию')
        # На практике самая частая ошибка пайплайна — падение чтения v4l2
        # из-за того, что камеру выдернули. Сбрасываем in-memory кэш камер,
        # чтобы пересборка после авто-переподключения заново просканировала
        # /dev/video* и пропустила отсутствующее устройство — иначе
        # force_update=False вернёт устаревший кэш, и пайплайн снова упадёт в
        # тесном цикле.
        CameraManager.clear_cache()
        if self._manager is None:
            return
        if self._manager.channels is not None and self._manager.channels.config is not None:
            self._manager.channels.config.send_json({
                'type': 'error',
                'message': 'pipeline_error',
            })
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()
