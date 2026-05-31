"""Главный цикл GLib, размещённый в daemon-потоке.

Bus watch GStreamer (`Gst.Bus.add_watch`) и колбэки `GLib.idle_add`
срабатывают только пока крутится `GLib.MainLoop`. Event loop asyncio не
прокачивает дефолтный контекст GLib, поэтому без этого потока сигналы
`webrtcbin`, сообщения bus error/state-changed и каждый вызов `idle_add`
из кода пайплайна молча не выполняются.

Использование:

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
    """Владеет одним GLib.MainLoop и daemon-потоком, который его крутит."""

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
        # Немного ждём, чтобы вызывающий код мог полагаться на реально
        # запущенный цикл, прежде чем создавать bus watch / планировать idle_add.
        self._started_evt.wait(timeout=2.0)
        logger.info('[glib] поток главного цикла запущен')

    def _run(self) -> None:
        assert self._loop is not None
        self._started_evt.set()
        try:
            self._loop.run()
        except Exception as exc:
            logger.exception('[glib] главный цикл упал: %s', exc)

    def stop(self, join_timeout: float = 2.0) -> None:
        if self._loop is None:
            return
        if self._loop.is_running():
            self._loop.quit()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
        logger.info('[glib] поток главного цикла остановлен')
        self._loop = None
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()
