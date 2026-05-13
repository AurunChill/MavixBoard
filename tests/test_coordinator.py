from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mavixboard.coordinator import SessionCoordinator


def _make_signal_client() -> MagicMock:
    sc = MagicMock()
    sc.connect = AsyncMock(return_value=True)
    sc.disconnect = AsyncMock()
    sc.send = AsyncMock()
    sc.listen = AsyncMock()
    return sc


def _make_pipeline_mock() -> MagicMock:
    pipeline = MagicMock(name='pipeline')
    webrtc = MagicMock(name='webrtcbin')
    counter = {'n': 0}

    def fake_connect(_signal, _callback):
        counter['n'] += 1
        return counter['n']

    webrtc.connect.side_effect = fake_connect
    pipeline.webrtc_elem = webrtc
    pipeline.start = MagicMock()
    pipeline.stop = MagicMock()
    return pipeline


async def test_handle_connect_with_invalid_gcs_id_does_nothing():
    sc = _make_signal_client()
    factory = MagicMock()
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect(None)
    await coord._handle_connect('')

    factory.assert_not_called()


async def test_handle_connect_with_no_pipeline_aborts():
    sc = _make_signal_client()
    factory = MagicMock(return_value=None)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')

    factory.assert_called_once()
    assert coord._manager is None
    assert coord._pipeline is None


async def test_handle_connect_with_pipeline_starts_session():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    factory = MagicMock(return_value=pipeline)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')

    pipeline.start.assert_called_once()
    assert coord._manager is not None
    assert coord._manager.active_gcs_id == 'gcs-1'
    coord._teardown()


async def test_handle_connect_replaces_existing_session():
    sc = _make_signal_client()
    pipeline_a = _make_pipeline_mock()
    pipeline_b = _make_pipeline_mock()
    factory = MagicMock(side_effect=[pipeline_a, pipeline_b])
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')
    await coord._handle_connect('gcs-2')

    pipeline_a.stop.assert_called_once()
    assert coord._manager is not None
    assert coord._manager.active_gcs_id == 'gcs-2'
    coord._teardown()


async def test_handle_disconnect_tears_down():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    factory = MagicMock(return_value=pipeline)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')
    await coord._on_message({'type': 'disconnect'})

    pipeline.stop.assert_called_once()
    assert coord._manager is None
    assert coord._pipeline is None


async def test_handle_sdp_without_session_does_nothing():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()

    await coord._handle_sdp({'gcs_id': 'gcs-1', 'sdp': {'type': 'answer', 'sdp': 'X'}})


async def test_handle_sdp_routes_to_manager():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    factory = MagicMock(return_value=pipeline)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')
    manager = coord._manager
    assert manager is not None
    manager.handle_sdp = AsyncMock()

    await coord._handle_sdp({'gcs_id': 'gcs-1', 'sdp': {'type': 'answer', 'sdp': 'v=0\n'}})

    manager.handle_sdp.assert_awaited_once_with('gcs-1', {'type': 'answer', 'sdp': 'v=0\n'})
    coord._teardown()


async def test_handle_ice_routes_to_manager():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    factory = MagicMock(return_value=pipeline)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._handle_connect('gcs-1')
    manager = coord._manager
    assert manager is not None
    manager.handle_ice = AsyncMock()

    await coord._handle_ice({'gcs_id': 'gcs-1', 'candidate': {'candidate': 'c', 'sdpMLineIndex': 0}})

    manager.handle_ice.assert_awaited_once()
    coord._teardown()


async def test_on_message_dispatches_by_type():
    sc = _make_signal_client()
    pipeline = _make_pipeline_mock()
    factory = MagicMock(return_value=pipeline)
    coord = SessionCoordinator(sc, factory)
    coord._loop = asyncio.get_running_loop()

    await coord._on_message({'type': 'connect', 'gcs_id': 'g-1'})
    assert coord._manager is not None

    await coord._on_message({'type': 'pong'})
    await coord._on_message({'type': 'error', 'message': 'oops'})
    await coord._on_message({'type': 'unknown'})

    coord._teardown()


async def test_run_reconnects_on_connection_loss():
    """Coordinator should reconnect after listen exits, but stop after stop() is called."""
    import websockets

    sc = _make_signal_client()
    sc.listen.side_effect = [
        websockets.exceptions.ConnectionClosed(None, None),
        websockets.exceptions.ConnectionClosed(None, None),
    ]
    coord = SessionCoordinator(sc, MagicMock(return_value=None), reconnect_delay=0.01)

    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.1)
    coord.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    assert sc.connect.await_count >= 2
    assert sc.disconnect.await_count >= 2
