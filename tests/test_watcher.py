from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from mavixboard.gstreamer.watcher import CameraWatcher


async def test_watcher_starts_and_stops():
    w = CameraWatcher(interval=0.01)
    assert w.is_running is False
    w.start({0}, lambda _: None)
    assert w.is_running is True
    w.stop()
    await asyncio.sleep(0)
    assert w.is_running is False


async def test_watcher_double_start_idempotent():
    w = CameraWatcher(interval=0.05)
    w.start({0}, lambda _: None)
    first = w._task
    w.start({0}, lambda _: None)
    assert w._task is first
    w.stop()


async def test_watcher_does_not_callback_when_unchanged():
    received: list = []
    w = CameraWatcher(interval=0.01)
    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', return_value={0, 1}):
        w.start({0, 1}, lambda ids: received.append(ids))
        await asyncio.sleep(0.05)
        w.stop()
    assert received == []


async def test_watcher_calls_back_on_camera_added():
    received: list = []
    w = CameraWatcher(interval=0.01)
    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', return_value={0, 1, 2}):
        w.start({0, 1}, lambda ids: received.append(ids))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        w.stop()
    assert received == [{0, 1, 2}]


async def test_watcher_calls_back_on_camera_removed():
    received: list = []
    w = CameraWatcher(interval=0.01)
    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', return_value={0}):
        w.start({0, 1}, lambda ids: received.append(ids))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        w.stop()
    assert received == [{0}]


async def test_watcher_callback_called_once_per_change():
    received: list = []
    w = CameraWatcher(interval=0.01)
    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', return_value={0, 2}):
        w.start({0, 1}, lambda ids: received.append(ids))
        await asyncio.sleep(0.1)
        w.stop()
    assert received == [{0, 2}]


async def test_watcher_swallows_scan_errors():
    received: list = []
    w = CameraWatcher(interval=0.01)
    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', side_effect=RuntimeError('boom')):
        w.start({0}, lambda ids: received.append(ids))
        await asyncio.sleep(0.05)
        w.stop()
    assert received == []


async def test_watcher_swallows_callback_errors():
    w = CameraWatcher(interval=0.01)

    def bad_cb(_ids):
        raise RuntimeError('cb boom')

    with patch('mavixboard.gstreamer.watcher._enumerate_capture_indices', return_value={99}):
        w.start({0}, bad_cb)
        await asyncio.sleep(0.05)
        w.stop()


async def test_watcher_stop_without_start_is_safe():
    w = CameraWatcher(interval=0.01)
    w.stop()  # no exception
