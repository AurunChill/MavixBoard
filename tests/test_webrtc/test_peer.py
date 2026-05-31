from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mavixboard.webrtc.peer import PeerSession


def _make_webrtc_mock() -> MagicMock:
    """Build a webrtcbin mock; connect() returns ascending handler ids."""
    webrtc = MagicMock(name='webrtcbin')
    counter = {'n': 0}

    def fake_connect(_signal, _callback):
        counter['n'] += 1
        return counter['n']

    webrtc.connect.side_effect = fake_connect
    return webrtc


async def test_peer_registers_signal_handlers():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    PeerSession('gcs-1', webrtc, loop)
    signals = [call.args[0] for call in webrtc.connect.call_args_list]
    assert 'on-negotiation-needed' in signals
    assert 'on-ice-candidate' in signals


async def test_peer_close_disconnects_handlers():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    peer.close()
    # PeerSession подключает 4 сигнала (negotiation, ice-candidate,
    # ice-gathering-state, ice-connection-state) — close() снимает все 4.
    assert webrtc.disconnect.call_count == 4


async def test_peer_ice_candidate_callback_enqueues():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)

    peer._on_ice_candidate(webrtc, 0, 'candidate:foo')
    await asyncio.sleep(0)
    item = peer.ice_candidates.get_nowait()
    assert item == {'candidate': 'candidate:foo', 'sdpMLineIndex': 0, 'sdpMid': '0'}


async def test_peer_apply_answer_wrong_type_returns_false():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    assert peer.apply_answer({'type': 'offer', 'sdp': 'X'}) is False


async def test_peer_apply_answer_missing_sdp_returns_false():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    assert peer.apply_answer({'type': 'answer'}) is False


async def test_peer_apply_answer_success_emits_set_remote(monkeypatch):
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    webrtc.emit.reset_mock()

    assert peer.apply_answer({'type': 'answer', 'sdp': 'v=0\n'}) is True
    emitted_signals = [call.args[0] for call in webrtc.emit.call_args_list]
    assert 'set-remote-description' in emitted_signals


async def test_peer_add_remote_ice_invalid_returns_false():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    assert peer.add_remote_ice({'candidate': 'c', 'sdpMLineIndex': 'not-int'}) is False
    assert peer.add_remote_ice({'sdpMLineIndex': 0}) is False


async def test_peer_add_remote_ice_success_emits():
    webrtc = _make_webrtc_mock()
    loop = asyncio.get_running_loop()
    peer = PeerSession('gcs-1', webrtc, loop)
    webrtc.emit.reset_mock()

    assert peer.add_remote_ice({'candidate': 'candidate:foo', 'sdpMLineIndex': 0}) is True
    webrtc.emit.assert_called_once_with('add-ice-candidate', 0, 'candidate:foo')
