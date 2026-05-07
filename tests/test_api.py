import aiohttp
import pytest
import pytest_asyncio
from aioresponses import aioresponses

from mavixboard.core.config import settings
from mavixboard.server.api import API_ROUTES, ApiSession

BASE_URL = "http://test"

FULL_REGISTER_RESPONSE = {
    "status": "registered",
    "drone_id": "abc123",
    "registered_at": "2024-01-01T00:00:00",
}


@pytest.fixture(autouse=True)
def patch_url(monkeypatch):
    monkeypatch.setattr(settings, "signal_server_ip", BASE_URL)


@pytest_asyncio.fixture
async def api():
    s = aiohttp.ClientSession()
    yield ApiSession(s)
    await s.close()


class TestConnectionCheck:
    async def test_returns_true_when_server_ok(self, api):
        with aioresponses() as m:
            m.get(BASE_URL + API_ROUTES.HEALTH_CHECK, payload={"status": "ok"})
            assert await api.connection_check() is True

    async def test_returns_false_when_status_not_ok(self, api):
        with aioresponses() as m:
            m.get(BASE_URL + API_ROUTES.HEALTH_CHECK, payload={"status": "error"})
            assert await api.connection_check() is False

    async def test_returns_false_on_client_error(self, api):
        with aioresponses() as m:
            m.get(BASE_URL + API_ROUTES.HEALTH_CHECK, exception=aiohttp.ClientError())
            assert await api.connection_check() is False


class TestSendRegister:
    async def test_returns_true_on_201_with_all_fields(self, api):
        with aioresponses() as m:
            m.post(BASE_URL + API_ROUTES.DRONE_REGISTER, status=201, payload=FULL_REGISTER_RESPONSE)
            assert await api.send_register("abc123") is True

    async def test_returns_false_on_201_missing_fields(self, api):
        with aioresponses() as m:
            m.post(BASE_URL + API_ROUTES.DRONE_REGISTER, status=201, payload={"status": "ok"})
            assert await api.send_register("abc123") is False

    async def test_returns_false_on_non_201(self, api):
        with aioresponses() as m:
            m.post(BASE_URL + API_ROUTES.DRONE_REGISTER, status=400, payload={"error": "bad request"})
            assert await api.send_register("abc123") is False

    async def test_returns_false_on_client_error(self, api):
        with aioresponses() as m:
            m.post(BASE_URL + API_ROUTES.DRONE_REGISTER, exception=aiohttp.ClientError())
            assert await api.send_register("abc123") is False
