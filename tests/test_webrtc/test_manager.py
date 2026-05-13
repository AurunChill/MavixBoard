from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mavixboard.webrtc.manager import WebRTCManager


def _make_webrtc_mock() -> MagicMock:
    webrtc = MagicMock(name='webrtcbin')
    counter = {'n': 0}

    def fake_connect(_signal, _callback):
        counter['n'] += 1
        return counter['n']

    webrtc.connect.side_effect = fake_connect
    return webrtc


async def test_start_session_creates_peer():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)

    mgr.start_session('gcs-1')
    assert mgr.active_gcs_id == 'gcs-1'

    mgr.end_session()


async def test_start_session_replaces_existing_peer():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)

    mgr.start_session('gcs-1')
    mgr.start_session('gcs-2')
    assert mgr.active_gcs_id == 'gcs-2'

    mgr.end_session()


async def test_end_session_clears_peer_and_fires_callback():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    callback = MagicMock()
    mgr.on_session_ended = callback

    mgr.start_session('gcs-1')
    mgr.end_session()

    assert mgr.active_gcs_id is None
    callback.assert_called_once()


async def test_end_session_without_active_is_noop():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.end_session()
    assert mgr.active_gcs_id is None


async def test_handle_sdp_with_wrong_gcs_id_ignored():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')

    await mgr.handle_sdp('gcs-other', {'type': 'answer', 'sdp': 'X'})
    emit_signals = [c.args[0] for c in webrtc.emit.call_args_list]
    assert 'set-remote-description' not in emit_signals

    mgr.end_session()


async def test_handle_sdp_routes_to_peer():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')
    webrtc.emit.reset_mock()

    await mgr.handle_sdp('gcs-1', {'type': 'answer', 'sdp': 'v=0\n'})
    emit_signals = [c.args[0] for c in webrtc.emit.call_args_list]
    assert 'set-remote-description' in emit_signals

    mgr.end_session()


async def test_handle_ice_routes_to_peer():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')
    webrtc.emit.reset_mock()

    await mgr.handle_ice('gcs-1', {'candidate': 'c', 'sdpMLineIndex': 0})
    webrtc.emit.assert_called_with('add-ice-candidate', 0, 'c')

    mgr.end_session()


async def test_handle_message_without_session_ignored():
    webrtc = _make_webrtc_mock()
    send = AsyncMock()
    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    await mgr.handle_sdp('gcs-1', {'type': 'answer', 'sdp': 'X'})
    await mgr.handle_ice('gcs-1', {'candidate': 'c', 'sdpMLineIndex': 0})
    emit_signals = [c.args[0] for c in webrtc.emit.call_args_list]
    assert 'set-remote-description' not in emit_signals
    assert 'add-ice-candidate' not in emit_signals


async def test_ice_pump_forwards_candidates():
    webrtc = _make_webrtc_mock()
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')

    peer = mgr._peer
    assert peer is not None
    await peer.ice_candidates.put({'candidate': 'c1', 'sdpMLineIndex': 0, 'sdpMid': '0'})
    await peer.ice_candidates.put({'candidate': 'c2', 'sdpMLineIndex': 1, 'sdpMid': '1'})
    await asyncio.sleep(0.05)

    assert sent == [
        {'type': 'ice', 'gcs_id': 'gcs-1', 'candidate': {'candidate': 'c1', 'sdpMLineIndex': 0, 'sdpMid': '0'}},
        {'type': 'ice', 'gcs_id': 'gcs-1', 'candidate': {'candidate': 'c2', 'sdpMLineIndex': 1, 'sdpMid': '1'}},
    ]
    mgr.end_session()


async def test_offer_pump_sends_sdp_once_per_change():
    webrtc = _make_webrtc_mock()
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    mgr = WebRTCManager(webrtc, asyncio.get_running_loop(), send)
    mgr.start_session('gcs-1')

    peer = mgr._peer
    assert peer is not None
    peer.offer_sdp = 'v=0\n'
    await asyncio.sleep(0.2)
    peer.offer_sdp = 'v=1\n'
    await asyncio.sleep(0.2)

    sdp_msgs = [m for m in sent if m['type'] == 'sdp']
    assert len(sdp_msgs) == 2
    assert sdp_msgs[0]['sdp'] == {'type': 'offer', 'sdp': 'v=0\n'}
    assert sdp_msgs[1]['sdp'] == {'type': 'offer', 'sdp': 'v=1\n'}

    mgr.end_session()
