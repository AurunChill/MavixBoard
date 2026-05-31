from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import websockets
from websockets.asyncio.client import connect as ws_connect

from mavixboard.core.logger import logger

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection


class SignalClient:
    def __init__(self, url: str, drone_token: str) -> None:
        self._url = url
        self._drone_token = drone_token
        self._conn: ClientConnection | None = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> bool:
        try:
            self._conn = await ws_connect(
                uri=self._url,
                additional_headers={'Authorization': f'Bearer {self._drone_token}'},
            )
            return True
        except (OSError, websockets.exceptions.InvalidURI, websockets.exceptions.InvalidHandshake) as exc:
            logger.info('[signal] connect error: %s', exc)
            self._conn = None
            return False

    async def disconnect(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.close()
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            logger.debug('[signal] disconnect error: %s', exc)
        finally:
            self._conn = None

    async def send(self, payload: dict) -> None:
        if self._conn is None:
            raise RuntimeError('сигнальный клиент не подключён')
        await self._conn.send(json.dumps(payload))

    async def listen(self, on_message: Callable[[dict], Awaitable[None]]) -> None:
        if self._conn is None:
            raise RuntimeError('сигнальный клиент не подключён')
        async for raw in self._conn:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning('[signal] bad json: %s', exc)
                continue
            await on_message(msg)
