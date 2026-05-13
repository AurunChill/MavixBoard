"""Tests for the GLib main-loop thread lifecycle.

GLib/Gst are MagicMock'd in tests/conftest.py (real PyGObject is only
available on the Pi). These tests therefore exercise the *lifecycle*
of GLibMainLoopThread — start / stop / idempotence — not whether the
underlying GLib loop actually iterates callbacks (that's an
integration concern verified on real hardware).
"""
from __future__ import annotations

from mavixboard.core.glib_loop import GLibMainLoopThread


def test_start_creates_loop_and_thread():
    g = GLibMainLoopThread()
    assert g._loop is None
    assert g._thread is None
    g.start()
    assert g._loop is not None
    assert g._thread is not None
    g.stop()


def test_double_start_is_idempotent():
    """A second start() while already running must not spawn a second thread."""
    g = GLibMainLoopThread()
    g.start()
    first_thread = g._thread
    g.start()
    assert g._thread is first_thread
    g.stop()


def test_stop_without_start_is_noop():
    g = GLibMainLoopThread()
    # Must not raise even though nothing was started.
    g.stop()
    assert g._loop is None
    assert g._thread is None


def test_stop_clears_state():
    g = GLibMainLoopThread()
    g.start()
    g.stop()
    assert g._loop is None
    assert g._thread is None


def test_can_restart_after_stop():
    """start → stop → start should produce a fresh loop/thread, not crash."""
    g = GLibMainLoopThread()
    g.start()
    g.stop()
    g.start()
    assert g._loop is not None
    assert g._thread is not None
    g.stop()
