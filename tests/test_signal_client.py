"""Unit + integration tests for SignalClient.

Integration tests spin up a real websockets server in-process to exercise
the full connect/send/listen/disconnect lifecycle.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from mavixboard.server.signal_client import SignalClient


#### unit tests with mocked ws_connect #################################################
async def test_connect_returns_true_on_success():
    fake_conn = AsyncMock()
    with patch(
        'mavixboard.server.signal_client.ws_connect',
        AsyncMock(return_value=fake_conn),
    ) as mock_connect:
        client = SignalClient('ws://test', 'token-xyz')
        result = await client.connect()

    assert result is True
    assert client.is_connected
    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs['uri'] == 'ws://test'
    assert call_kwargs['additional_headers'] == {'Authorization': 'Bearer token-xyz'}


async def test_connect_returns_false_on_oserror():
    with patch(
        'mavixboard.server.signal_client.ws_connect',
        AsyncMock(side_effect=OSError('refused')),
    ):
        client = SignalClient('ws://test', 'token')
        result = await client.connect()

    assert result is False
    assert client.is_connected is False


async def test_send_raises_when_not_connected():
    client = SignalClient('ws://test', 't')
    with pytest.raises(RuntimeError):
        await client.send({'type': 'x'})


async def test_listen_raises_when_not_connected():
    client = SignalClient('ws://test', 't')
    with pytest.raises(RuntimeError):
        await client.listen(AsyncMock())


async def test_disconnect_noop_when_not_connected():
    client = SignalClient('ws://test', 't')
    await client.disconnect()
    assert client.is_connected is False


#### integration with a real websockets server #########################################
class _StubServer:
    def __init__(self) -> None:
        self.received_headers: dict = {}
        self.received_messages: list[dict] = []
        self.outbound: list[dict] = []

    async def handler(self, websocket) -> None:
        self.received_headers = dict(websocket.request.headers)
        for msg in self.outbound:
            await websocket.send(json.dumps(msg))
        try:
            async for raw in websocket:
                self.received_messages.append(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            return


async def test_full_lifecycle_against_real_server():
    stub = _StubServer()
    stub.outbound = [{'type': 'pong'}, {'type': 'connect', 'gcs_id': 'g-1'}]
    async with websockets.serve(stub.handler, 'localhost', 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = SignalClient(f'ws://localhost:{port}', 'drone-token-A')

        assert await client.connect() is True
        headers_lower = {k.lower(): v for k, v in stub.received_headers.items()}
        assert headers_lower.get('authorization') == 'Bearer drone-token-A'

        await client.send({'type': 'sdp', 'gcs_id': 'g-1'})

        got: list[dict] = []
        async def collect(msg: dict) -> None:
            got.append(msg)
            if len(got) >= 2:
                await client.disconnect()

        try:
            await asyncio.wait_for(client.listen(collect), timeout=2.0)
        except websockets.exceptions.ConnectionClosed:
            pass

        assert got == stub.outbound
        # Allow server task to capture the message
        await asyncio.sleep(0.05)
        assert stub.received_messages == [{'type': 'sdp', 'gcs_id': 'g-1'}]


async def test_listen_skips_invalid_json():
    async def handler(websocket) -> None:
        await websocket.send('not-json')
        await websocket.send(json.dumps({'type': 'pong'}))
        await asyncio.sleep(0.05)

    async with websockets.serve(handler, 'localhost', 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = SignalClient(f'ws://localhost:{port}', 't')
        await client.connect()

        received: list[dict] = []
        async def cb(msg: dict) -> None:
            received.append(msg)

        try:
            await asyncio.wait_for(client.listen(cb), timeout=1.5)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            pass
        finally:
            await client.disconnect()

        assert received == [{'type': 'pong'}]
