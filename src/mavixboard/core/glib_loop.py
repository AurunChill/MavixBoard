"""GLib main loop hosted in a daemon thread.

GStreamer's bus watch (`Gst.Bus.add_watch`) and `GLib.idle_add` callbacks
only fire while a `GLib.MainLoop` is iterating. asyncio's event loop
does not pump GLib's default context, so without this thread the
`webrtcbin` signals, bus error/state-changed messages, and every
`idle_add` call from the pipeline code silently never run.

Usage:

    from mavixboard.core.glib_loop import GLibMainLoopThread

    glib = GLibMainLoopThread()
    glib.start()
    try:
        ...
    finally:
        glib.stop()
"""
from __future__ import annotations

import threading

from gi.repository import GLib

from mavixboard.core.logger import logger


class GLibMainLoopThread:
    """Owns one GLib.MainLoop and the daemon thread that iterates it."""

    def __init__(self) -> None:
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._started_evt = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(
            target=self._run,
            name='glib-mainloop',
            daemon=True,
        )
        self._thread.start()
        # Wait briefly so callers can rely on the loop actually running
        # before they create bus watches / schedule idle_add calls.
        self._started_evt.wait(timeout=2.0)
        logger.info('[glib] main loop thread started')

    def _run(self) -> None:
        assert self._loop is not None
        self._started_evt.set()
        try:
            self._loop.run()
        except Exception as exc:
            logger.exception('[glib] main loop crashed: %s', exc)

    def stop(self, join_timeout: float = 2.0) -> None:
        if self._loop is None:
            return
        if self._loop.is_running():
            self._loop.quit()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
        logger.info('[glib] main loop thread stopped')
        self._loop = None
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()
