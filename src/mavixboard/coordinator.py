from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable

import websockets

from mavixboard.core.logger import logger
from mavixboard.fc.service import FCService
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
        reconnect_delay: float = 1.0,
    ) -> None:
        self._signal_client = signal_client
        self._pipeline_factory = pipeline_factory
        self._fc_service = fc_service
        self._watcher = watcher
        self._reconnect_delay = reconnect_delay
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
                await asyncio.sleep(self._reconnect_delay)
                continue
            logger.info('[coord] connected to signal server')
            try:
                await self._signal_client.listen(self._on_message)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning('[coord] signal connection closed: %s', exc)
            except Exception as exc:
                logger.error('[coord] unexpected error in listen: %s', exc)
            finally:
                self._teardown()
                await self._signal_client.disconnect()
            await asyncio.sleep(self._reconnect_delay)

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self._teardown()

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
        pipeline = self._pipeline_factory()
        if pipeline is None or pipeline.webrtc_elem is None:
            logger.error('[coord] pipeline factory returned no pipeline; aborting session')
            return
        assert self._loop is not None
        self._pipeline = pipeline
        self._manager = WebRTCManager(
            pipeline.webrtc_elem,
            self._loop,
            self._signal_client.send,
            fc_service=self._fc_service,
        )
        pipeline.start()
        self._manager.start_session(gcs_id)
        if self._manager.channels is not None:
            self._manager.channels.config.on_message = self._on_config_message
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
            case 'reboot':
                assert self._loop is not None
                self._loop.create_task(self._reboot())
            case 'bitrate':
                self._apply_bitrate_updates(payload.get('updates', []))
            case _:
                logger.warning('[coord] unknown config message type: %s', payload.get('type'))

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

    async def _reboot(self) -> None:
        logger.info('[coord] reboot requested via config channel')
        try:
            self._teardown()
            if self._fc_service is not None:
                await self._fc_service.stop()
            await self._signal_client.disconnect()
        except Exception as exc:
            logger.warning('[coord] reboot cleanup error: %s', exc)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _on_cameras_changed(self, new_ids: set[int]) -> None:
        logger.info('[coord] cameras changed: %s, tearing down session', sorted(new_ids))
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
