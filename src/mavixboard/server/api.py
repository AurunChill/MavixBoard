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
        """Register this drone on the server.

        DEV-PATH ONLY. The production .deb flow has the drone already
        registered server-side at build time — the server bakes its
        DRONE_ID and DRONE_TOKEN into preset.env, and the board reads
        them on boot (see __main__._resolve_drone_token).

        In dev there's no preset.env, so the board falls back to a
        locally-generated token. It identifies itself with that token
        as the drone_id and trusts the server to mint a server-side
        drone_token for it — except the new auth-protected endpoint
        will reject this call (no user JWT). This branch is kept for
        compatibility with old deployments and CI fixtures that
        explicitly pre-create the Drone row; production won't hit it.

        Returns True only if server responded 201 with full payload.
        """
        url = settings.signal_server_ip + API_ROUTES.DRONE_REGISTER
        # Use settings.drone_id if set (early preset.env adoption), else
        # the dev-generated token doubles as the identifier.
        drone_id = settings.drone_id or drone_token
        payload = {'user_id': settings.user_id, 'drone_id': drone_id}
        try:
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    ok = all(k in data for k in ('drone_id', 'user_id', 'drone_token'))
                    if not ok:
                        logger.warning("register: missing fields in response: %s", data)
                    return ok
                logger.warning("register failed: status %s (expected for dev path without user JWT)", resp.status)
                return False
        except aiohttp.ClientError as e:
            logger.error("register request error: %s", e)
            return False
