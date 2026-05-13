"""Tests for Coordinator's config-channel handling: reboot, bitrate, watcher wiring."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mavixboard.coordinator import SessionCoordinator


def _make_signal_client() -> MagicMock:
    sc = MagicMock()
    sc.connect = AsyncMock(return_value=True)
    sc.disconnect = AsyncMock()
    sc.send = AsyncMock()
    sc.listen = AsyncMock()
    return sc


def _make_camera(device_index: int, bitrate: int = 1000) -> MagicMock:
    cam = MagicMock()
    cam.device_index = device_index
    cam.bitrate_kbs = bitrate
    cam.save = MagicMock()
    return cam


def _make_pipeline_mock(cameras: list[MagicMock] | None = None) -> MagicMock:
    pipeline = MagicMock(name='pipeline')
    webrtc = MagicMock(name='webrtcbin')
    counter = {'n': 0}

    def fake_connect(_signal, _callback):
        counter['n'] += 1
        return counter['n']

    webrtc.connect.side_effect = fake_connect

    channels: list[MagicMock] = []

    def fake_emit(signal, *args, **kwargs):
        if signal == 'create-data-channel':
            ch = MagicMock(name=f'dc-{len(channels)}')
            ch.connect.side_effect = lambda *a, **kw: 1
            channels.append(ch)
            return ch
        return None

    webrtc.emit.side_effect = fake_emit
    pipeline.webrtc_elem = webrtc
    pipeline.start = MagicMock()
    pipeline.stop = MagicMock()
    pipeline.update_bitrate = MagicMock(return_value=True)
    pipeline.cameras = cameras if cameras is not None else [_make_camera(0)]
    pipeline._channels = channels
    return pipeline


# ============================================================================
# Reboot
# ============================================================================

async def test_config_reboot_calls_execv():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')

    with patch('mavixboard.coordinator.os.execv') as mock_execv:
        coord._on_config_message({'type': 'reboot'})
        # task is scheduled — wait
        await asyncio.sleep(0.05)
        mock_execv.assert_called_once()
    coord._teardown()


async def test_config_reboot_tears_down_session():
    sc = _make_signal_client()
    fc = MagicMock()
    fc.stop = AsyncMock()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), fc_service=fc)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')

    with patch('mavixboard.coordinator.os.execv'):
        coord._on_config_message({'type': 'reboot'})
        await asyncio.sleep(0.05)

    pipeline.stop.assert_called_once()
    fc.stop.assert_awaited()
    sc.disconnect.assert_awaited()


async def test_config_reboot_continues_even_if_cleanup_errors():
    sc = _make_signal_client()
    fc = MagicMock()
    fc.stop = AsyncMock(side_effect=RuntimeError('fc stop boom'))
    pipeline = _make_pipeline_mock()
    pipeline.stop = MagicMock(side_effect=RuntimeError('pipe boom'))
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), fc_service=fc)
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    with patch('mavixboard.coordinator.os.execv') as mock_execv:
        coord._on_config_message({'type': 'reboot'})
        await asyncio.sleep(0.05)
        mock_execv.assert_called_once()


# ============================================================================
# Bitrate
# ============================================================================

async def test_config_bitrate_routes_to_pipeline():
    sc = _make_signal_client()
    cam0 = _make_camera(0, bitrate=1000)
    cam1 = _make_camera(2, bitrate=500)
    pipeline = _make_pipeline_mock(cameras=[cam0, cam1])
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_config_message({
        'type': 'bitrate',
        'updates': [
            {'device_index': 0, 'bitrate_kbs': 800},
            {'device_index': 2, 'bitrate_kbs': 1200},
        ],
    })

    # pipe_idx 0 for device_index=0, pipe_idx 1 for device_index=2
    pipeline.update_bitrate.assert_any_call(0, 800)
    pipeline.update_bitrate.assert_any_call(1, 1200)
    assert cam0.bitrate_kbs == 800
    assert cam1.bitrate_kbs == 1200
    cam0.save.assert_called_once()
    cam1.save.assert_called_once()
    coord._teardown()


async def test_config_bitrate_ignores_unknown_device_index():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock(cameras=[_make_camera(0)])
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_config_message({
        'type': 'bitrate',
        'updates': [{'device_index': 99, 'bitrate_kbs': 800}],
    })

    pipeline.update_bitrate.assert_not_called()
    coord._teardown()


async def test_config_bitrate_ignores_bad_update_entries():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock(cameras=[_make_camera(0)])
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_config_message({
        'type': 'bitrate',
        'updates': [
            'not-a-dict',
            {'device_index': 0},  # missing bitrate_kbs
            {'bitrate_kbs': 500},  # missing device_index
            {'device_index': '0', 'bitrate_kbs': 500},  # wrong type
            {'device_index': 0, 'bitrate_kbs': 600},  # valid
        ],
    })

    pipeline.update_bitrate.assert_called_once_with(0, 600)
    coord._teardown()


async def test_config_bitrate_without_active_pipeline_is_noop():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()
    coord._on_config_message({
        'type': 'bitrate',
        'updates': [{'device_index': 0, 'bitrate_kbs': 800}],
    })
    # no exception


async def test_config_bitrate_swallows_save_errors():
    sc = _make_signal_client()
    cam = _make_camera(0)
    cam.save = MagicMock(side_effect=RuntimeError('disk full'))
    pipeline = _make_pipeline_mock(cameras=[cam])
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_config_message({
        'type': 'bitrate',
        'updates': [{'device_index': 0, 'bitrate_kbs': 700}],
    })
    pipeline.update_bitrate.assert_called_once_with(0, 700)
    coord._teardown()


# ============================================================================
# Config message dispatch
# ============================================================================

async def test_config_non_dict_payload_ignored():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()
    coord._on_config_message(['list', 'payload'])  # not dict
    # No exception, no actions


async def test_config_unknown_type_logged():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()
    coord._on_config_message({'type': 'unknown_thing'})


# ============================================================================
# Watcher wiring
# ============================================================================

async def test_watcher_started_on_connect():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock(cameras=[_make_camera(0), _make_camera(1)])
    watcher = MagicMock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), watcher=watcher)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')

    watcher.start.assert_called_once()
    initial_ids, callback = watcher.start.call_args.args
    assert initial_ids == {0, 1}
    assert callable(callback)
    coord._teardown()


async def test_watcher_stopped_on_teardown():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    watcher = MagicMock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), watcher=watcher)
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')
    coord._teardown()
    watcher.stop.assert_called()


async def test_watcher_callback_sends_cameras_changed_via_config():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock(cameras=[_make_camera(0), _make_camera(1)])
    watcher = MagicMock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), watcher=watcher)
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    config_ch = coord._manager.channels.config
    config_ch.send_json = MagicMock()

    coord._on_cameras_changed({0, 1, 2})

    config_ch.send_json.assert_called_once_with({
        'type': 'cameras_changed',
        'device_indices': [0, 1, 2],
    })


async def test_watcher_callback_tears_down_session():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    watcher = MagicMock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), watcher=watcher)
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')
    assert coord._manager is not None

    coord._on_cameras_changed({99})
    await asyncio.sleep(0.02)

    pipeline.stop.assert_called_once()
    assert coord._manager is None
    assert coord._pipeline is None


async def test_watcher_callback_sends_disconnect_session_to_server():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    watcher = MagicMock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline), watcher=watcher)
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_cameras_changed({0})
    await asyncio.sleep(0.02)

    sc.send.assert_any_await({'type': 'disconnect_session'})


# ============================================================================
# Pipeline error
# ============================================================================

async def test_pipeline_error_hook_is_installed_on_connect():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    assert pipeline.on_error == coord._on_pipeline_error
    coord._teardown()


async def test_pipeline_error_tears_down_session():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')
    assert coord._manager is not None

    coord._on_pipeline_error()
    await asyncio.sleep(0.02)

    pipeline.stop.assert_called_once()
    assert coord._manager is None
    assert coord._pipeline is None


async def test_pipeline_error_sends_disconnect_session_to_server():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    coord._on_pipeline_error()
    await asyncio.sleep(0.02)

    sc.send.assert_any_await({'type': 'disconnect_session'})


async def test_pipeline_error_without_session_is_noop():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()
    coord._on_pipeline_error()  # no exception
    sc.send.assert_not_called()


async def test_watcher_callback_no_session_is_noop():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock(), watcher=MagicMock())
    coord._loop = asyncio.get_running_loop()
    coord._on_cameras_changed({0, 1})  # should not raise


# ============================================================================
# Config channel wired through manager
# ============================================================================

async def test_config_channel_routes_to_coordinator_handler():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    coord = SessionCoordinator(sc, MagicMock(return_value=pipeline))
    coord._loop = asyncio.get_running_loop()
    await coord._handle_connect('gcs-1')

    received: list = []
    # Override the coordinator handler to capture what flows through
    original = coord._on_config_message
    coord._on_config_message = lambda p: received.append(p)
    # Re-wire to the new handler reference
    coord._manager.channels.config.on_message = coord._on_config_message

    # Simulate dc on_message firing
    import json
    buf = MagicMock()
    buf.get_data.return_value = json.dumps({'type': 'bitrate', 'updates': []}).encode('utf-8')
    coord._manager.channels.config._on_data(MagicMock(), buf)

    assert received == [{'type': 'bitrate', 'updates': []}]
    coord._teardown()
