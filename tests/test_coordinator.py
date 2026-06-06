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


#### _on_fc_telemetry / telemetry-канал ################################################
def _coord_with_fake_channels() -> tuple[SessionCoordinator, MagicMock]:
    coord = SessionCoordinator(_make_signal_client(), MagicMock())
    manager = MagicMock(name='manager')
    channels = MagicMock(name='channels')
    channels.telemetry = MagicMock(name='telemetry')
    channels.config = MagicMock(name='config')
    manager.channels = channels
    coord._manager = manager
    return coord, channels


async def test_on_fc_telemetry_gps_and_attitude_merged_to_telemetry():
    coord, channels = _coord_with_fake_channels()

    coord._on_fc_telemetry({'type': 'gps', 'lat': 55.7, 'lon': 37.6, 'alt': 150.0,
                            'heading': 0.0, 'sats': 9})
    coord._on_fc_telemetry({'type': 'attitude', 'heading': 123.0})

    # GPS пришёл первым -> уже отправили telemetry; attitude обновил heading.
    assert channels.telemetry.send_json.call_count == 2
    last = channels.telemetry.send_json.call_args.args[0]
    assert last == {'type': 'telemetry', 'lat': 55.7, 'lon': 37.6, 'alt': 150.0,
                    'heading': 123.0, 'sats': 9}
    # battery/config не задействован для gps/attitude.
    channels.config.send_json.assert_not_called()


async def test_on_fc_telemetry_gps_heading_not_overwritten_by_zero():
    coord, channels = _coord_with_fake_channels()
    coord._on_fc_telemetry({'type': 'attitude', 'heading': 200.0})
    # attitude без lat/lon ничего не шлёт
    channels.telemetry.send_json.assert_not_called()
    coord._on_fc_telemetry({'type': 'gps', 'lat': 1.0, 'lon': 2.0, 'alt': 0.0,
                            'heading': 0.0, 'sats': 5})
    sent = channels.telemetry.send_json.call_args.args[0]
    # GPS-курс 0.0 не затёр heading из attitude.
    assert sent['heading'] == 200.0
    assert sent['lat'] == 1.0


async def test_on_fc_telemetry_no_telemetry_channel_is_silent():
    coord = SessionCoordinator(_make_signal_client(), MagicMock())
    manager = MagicMock(name='manager')
    channels = MagicMock(name='channels')
    channels.telemetry = None
    manager.channels = channels
    coord._manager = manager
    # Не должно бросать исключение.
    coord._on_fc_telemetry({'type': 'gps', 'lat': 1.0, 'lon': 2.0, 'sats': 3})


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


async def test_on_message_ping_replies_with_pong():
    sc = _make_signal_client()
    coord = SessionCoordinator(sc, MagicMock())
    coord._loop = asyncio.get_running_loop()

    await coord._on_message({'type': 'ping'})

    sc.send.assert_awaited_once_with({'type': 'pong'})


async def test_run_reconnects_on_connection_loss():
    """Coordinator should reconnect after listen exits, but stop after stop() is called."""
    import websockets

    from mavixboard.core.backoff import ExponentialBackoff

    sc = _make_signal_client()
    sc.listen.side_effect = [
        websockets.exceptions.ConnectionClosed(None, None),
        websockets.exceptions.ConnectionClosed(None, None),
    ]
    backoff = ExponentialBackoff(initial=0.01, multiplier=1.0, cap=0.01)
    coord = SessionCoordinator(sc, MagicMock(return_value=None), backoff=backoff)

    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.1)
    coord.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    assert sc.connect.await_count >= 2
    assert sc.disconnect.await_count >= 2


async def test_run_uses_backoff_on_connect_failure():
    """Failed connects should bump the backoff delay."""
    from mavixboard.core.backoff import ExponentialBackoff

    sc = _make_signal_client()
    sc.connect = AsyncMock(return_value=False)  # always fail
    backoff = ExponentialBackoff(initial=0.02, multiplier=2.0, cap=0.05)
    coord = SessionCoordinator(sc, MagicMock(return_value=None), backoff=backoff)

    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.2)
    coord.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    assert sc.connect.await_count >= 2
    # After several failures, backoff should be at the cap
    assert backoff.current == 0.05


async def test_run_resets_backoff_on_success():
    """A successful connect resets the backoff."""
    import websockets

    from mavixboard.core.backoff import ExponentialBackoff

    sc = _make_signal_client()
    sc.connect = AsyncMock(return_value=True)

    async def listen_then_close(_cb):
        raise websockets.exceptions.ConnectionClosed(None, None)

    sc.listen = AsyncMock(side_effect=listen_then_close)
    backoff = ExponentialBackoff(initial=0.02, multiplier=2.0, cap=1.0)
    backoff._current = 0.5  # pretend we already escalated
    coord = SessionCoordinator(sc, MagicMock(return_value=None), backoff=backoff)

    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.05)
    coord.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    # After successful connect, backoff was reset to initial (0.02), then next_delay
    # bumped it to 0.04. Should be far below the original 0.5.
    assert backoff.current < 0.5
