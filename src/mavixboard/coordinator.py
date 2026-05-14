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
                logger.info('[coord] connect failed, retry in %.1fs', delay)
                await asyncio.sleep(delay)
                continue
            logger.info('[coord] connected to signal server')
            self._backoff.reset()
            try:
                await self._signal_client.listen(self._on_message)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning('[coord] signal connection closed: %s', exc)
            except Exception as exc:
                logger.error('[coord] unexpected error in listen: %s', exc)
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
                logger.warning('[coord] pipeline stop error: %s', exc)
            self._pipeline = None
        if self._fc_service is not None:
            self._fc_service.set_change_callback(None)

    def _on_fc_change(self, kind: str | None, name: str) -> None:
        """Fired by FCService when the FC controller appears/disappears.
        Push a fresh `fc` config message so the GCS UI updates without
        waiting for the next session restart."""
        if self._manager is None:
            return
        try:
            self._manager.notify_fc_changed()
        except Exception as exc:
            logger.warning('[coord] notify_fc_changed error: %s', exc)

    async def _on_message(self, msg: dict) -> None:
        kind = msg.get('type')
        match kind:
            case 'connect':
                await self._handle_connect(msg.get('gcs_id'))
            case 'disconnect':
                logger.info('[coord] server says GCS disconnected')
                self._teardown()
            case 'sdp':
                await self._handle_sdp(msg)
            case 'ice':
                await self._handle_ice(msg)
            case 'error':
                logger.warning('[coord] server error: %s', msg.get('message'))
            case 'ping':
                await self._signal_client.send({'type': 'pong'})
            case 'pong':
                pass
            case _:
                logger.warning('[coord] unknown message type: %s', kind)

    async def _handle_connect(self, gcs_id: str | None) -> None:
        if not isinstance(gcs_id, str) or not gcs_id:
            logger.warning('[coord] connect with invalid gcs_id')
            return
        if self._pipeline is not None:
            logger.info('[coord] existing session active, tearing down before new one')
            self._teardown()
        # pipeline_factory may run a multi-second GStreamer calibration loop;
        # pipeline.start blocks on get_state(PLAYING). Both are sync and must
        # NOT run on the asyncio loop directly — otherwise _pump_offer can't
        # ship the SDP and signal_client.listen can't ack pings, and the
        # server kicks the drone WS after WS_PING_TIMEOUT (45s) thinking the
        # drone died. Run them via asyncio.to_thread to keep the loop free.
        pipeline = await asyncio.to_thread(self._pipeline_factory)
        if pipeline is None or pipeline.webrtc_elem is None:
            logger.error('[coord] pipeline factory returned no pipeline; aborting session')
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
        # Wait for the pipeline to actually reach PLAYING before creating
        # data channels. If v4l2_open fails (camera unplugged between scan
        # and pipeline.start, /dev/videoN not yet ready after udev), the
        # bus posts ERROR which schedules _on_pipeline_error on the GLib
        # thread. Running it concurrently with manager.start_session would
        # trip `gst_webrtc_bin_create_data_channel: is_closed != TRUE`
        # because webrtcbin gets torn down while channels are being created.
        if not await asyncio.to_thread(pipeline.start):
            logger.error('[coord] pipeline failed to reach PLAYING; aborting session')
            try:
                pipeline.stop()
            except Exception as exc:
                logger.debug('[coord] pipeline stop after failed start: %s', exc)
            self._pipeline = None
            self._manager = None
            CameraManager.clear_cache()
            return
        self._manager.start_session(gcs_id, cameras=pipeline.cameras)
        if self._manager.channels is not None:
            self._manager.channels.config.on_message = self._on_config_message
        if self._fc_service is not None:
            # FCService fires this whenever the FC controller appears or
            # disappears mid-session (e.g. user plugs the FC in after the
            # WebRTC session is up). manager._send_fc_info only runs once
            # on data-channel open — without re-firing it here, the GCS
            # would never learn about a hot-plugged FC.
            self._fc_service.set_change_callback(self._on_fc_change)
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
            logger.warning('[coord] config message must be an object, got %s', type(payload).__name__)
            return
        match payload.get('type'):
            case 'calibrate':
                self._force_calibrate()
            case 'bitrate':
                self._apply_bitrate_updates(payload.get('updates', []))
            case 'params':
                self._apply_params_updates(payload.get('updates', []))
            case _:
                logger.warning('[coord] unknown config message type: %s', payload.get('type'))

    def _apply_params_updates(self, updates) -> None:
        """Persist a new param_index per camera, then tear down the session.

        Resolution/FPS are baked into the GStreamer caps at pipeline build
        time; there's no clean way to change them on a running pipeline.
        Persisting + dropping the peer makes the GCS reconnect, and the
        next pipeline build picks up the new param_index from Camera.get
        in CameraRegistry._scan.
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
                logger.warning('[coord] params update for unknown device_index=%s', device_index)
                continue
            if param_index < 0 or param_index >= len(cam.params):
                logger.warning('[coord] params update with out-of-range param_index=%s for device_index=%s',
                               param_index, device_index)
                continue
            if cam.param_index == param_index:
                continue
            cam.param_index = param_index
            try:
                cam.save()
            except Exception as exc:
                logger.warning('[coord] camera save error: %s', exc)
            changed = True
        if not changed:
            return
        logger.info('[coord] params changed, tearing down session for renegotiation')
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

    def _apply_bitrate_updates(self, updates) -> None:
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
                logger.warning('[coord] bitrate update for unknown device_index=%s', device_index)
                continue
            self._pipeline.update_bitrate(pipe_idx, bitrate_kbs)
            cameras[pipe_idx].bitrate_kbs = bitrate_kbs
            try:
                cameras[pipe_idx].save()
            except Exception as exc:
                logger.warning('[coord] camera save error: %s', exc)

    def _force_calibrate(self) -> None:
        """Drop saved camera calibrations and tear down the session so the
        next pipeline build re-runs the full GStreamer probing loop for
        every device. The GCS auto-reconnects → _build_pipeline → _scan;
        Camera.get returns None for each device (.json removed) → falls
        through to CameraCalibrator.calibrate."""
        logger.info('[coord] full re-calibration requested via config channel')
        cameras = list(self._pipeline.cameras) if self._pipeline is not None else []
        if not cameras:
            cameras = CameraManager.get_cameras()
        for cam in cameras:
            path = settings.data_path / f'{cam.name}.json'
            try:
                path.unlink(missing_ok=True)
                logger.info('[coord] removed saved calibration: %s', path)
            except OSError as exc:
                logger.warning('[coord] failed to remove %s: %s', path, exc)
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
        logger.info('[coord] cameras changed: %s, tearing down session', sorted(new_ids))
        # Invalidate the in-memory camera cache FIRST, before any short-circuit
        # below — _on_pipeline_error may have torn down the manager already
        # (camera unplugged → v4l2 read fails → pipeline error fires before
        # the watcher's 5s poll). If clear_cache is gated behind a non-None
        # manager check, the next pipeline build sees stale cache, tries to
        # reopen the unplugged device, errors again, and the session loops
        # without ever recovering. _scan reuses *.json on disk for any
        # device whose name still matches; only genuinely new devices get
        # re-calibrated.
        CameraManager.clear_cache()
        if self._manager is None:
            return
        if self._manager.channels is not None:
            self._manager.channels.config.send_json({
                'type': 'cameras_changed',
                'device_indices': sorted(new_ids),
            })
        # Tell server to drop the peer pair and notify GCS, then teardown locally.
        # New pipeline is built on next 'connect' from server with current cameras.
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(
            self._signal_client.send({'type': 'disconnect_session'}),
            self._loop,
        )
        self._teardown()

    def _on_pipeline_error(self) -> None:
        """Called by GStreamerPipe bus watch when the pipeline emits ERROR.

        First send an error notice to the GCS via the config-channel so the
        UI can tell the pilot, then drop the peer session entirely. The new
        pipeline will be built when the GCS reconnects (server sends
        'connect' again).
        """
        logger.warning('[coord] pipeline error, tearing down session')
        # The most common pipeline error in practice is a v4l2 read failing
        # because the camera got unplugged. Drop the in-memory camera cache
        # so the rebuild after auto-reconnect re-scans /dev/video* and skips
        # the missing device — otherwise force_update=False returns stale
        # cache and the pipeline errors again in a tight loop.
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
