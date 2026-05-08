from __future__ import annotations

from enum import StrEnum

import aiohttp

from mavixboard.core.config import settings
from mavixboard.core.logger import logger


class API_ROUTES(StrEnum):
    HEALTH_CHECK = '/api/v1/health'
    DRONE_REGISTER = '/api/v1/drones/register'


class ApiSession:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    @classmethod
    async def create(cls) -> ApiSession:
        """Create a new ApiSession with a fresh aiohttp ClientSession.

        Returns:
            ApiSession instance ready for requests.
        """
        session = aiohttp.ClientSession()
        return cls(session)

    async def close(self) -> None:
        """Close the underlying aiohttp ClientSession."""
        await self.session.close()

    async def connection_check(self) -> bool:
        """Check if the server is reachable and healthy.

        Returns:
            True if server responds with status 'ok', False otherwise.
        """
        url = settings.signal_server_ip + API_ROUTES.HEALTH_CHECK
        try:
            async with self.session.get(url) as resp:
                data = await resp.json()
                return data.get('status') == 'ok'
        except aiohttp.ClientError:
            return False

    async def send_register(self, drone_token: str) -> bool:
        """Register the drone on the server using its token.

        Returns:
            True if server responded with 201 and all expected fields present.
        """
        url = settings.signal_server_ip + API_ROUTES.DRONE_REGISTER
        payload = {'user_id': settings.user_id, 'drone_id': drone_token}
        try:
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    ok = all(k in data for k in ('drone_id', 'user_id', 'drone_token'))
                    if not ok:
                        logger.warning("register: missing fields in response: %s", data)
                    return ok
                logger.warning("register failed: status %s", resp.status)
                return False
        except aiohttp.ClientError as e:
            logger.error("register request error: %s", e)
            return False
