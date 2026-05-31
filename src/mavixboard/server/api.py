from __future__ import annotations

from enum import StrEnum

import aiohttp

from mavixboard.core.config import settings
from mavixboard.core.logger import logger


class API_ROUTES(StrEnum):
    HEALTH_CHECK = '/api/v1/health'
    DRONE_REGISTER = '/api/v1/drones/register'


class ApiSession:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    @classmethod
    async def create(cls) -> ApiSession:
        """Создаёт новый ApiSession со свежим aiohttp ClientSession, готовым к запросам."""
        session = aiohttp.ClientSession()
        return cls(session)

    async def close(self) -> None:
        """Закрывает нижележащий aiohttp ClientSession."""
        await self.session.close()

    async def connection_check(self) -> bool:
        """Проверяет, доступен ли сервер и здоров ли он.

        Возвращает True, если сервер отвечает со статусом 'ok', иначе False.
        """
        url = settings.signal_server_ip + API_ROUTES.HEALTH_CHECK
        try:
            async with self.session.get(url) as resp:
                data = await resp.json()
                return data.get('status') == 'ok'
        except aiohttp.ClientError:
            return False

    async def send_register(self, drone_token: str) -> bool:
        """Регистрирует этот дрон на сервере.

        ТОЛЬКО DEV-ПУТЬ. В production-сценарии (установка через install.sh) дрон
        уже зарегистрирован на стороне сервера на этапе сборки — сервер зашивает его DRONE_ID и
        DRONE_TOKEN в preset.env, а board читает их при старте (см.
        __main__._resolve_drone_token).

        В dev preset.env нет, поэтому board откатывается на локально
        сгенерированный токен. Он идентифицирует себя этим токеном как drone_id
        и доверяет серверу выпустить для него серверный drone_token — вот только
        новый защищённый авторизацией эндпоинт отклонит этот вызов (нет
        пользовательского JWT). Ветка сохранена для совместимости со старыми
        развёртываниями и CI-фикстурами, которые явно заранее создают строку
        Drone; в production она не выполняется.

        Возвращает True, только если сервер ответил 201 с полным payload.
        """
        url = settings.signal_server_ip + API_ROUTES.DRONE_REGISTER
        # Используем settings.drone_id, если задан (раннее принятие preset.env),
        # иначе сгенерированный в dev токен заодно служит идентификатором.
        drone_id = settings.drone_id or drone_token
        payload = {'user_id': settings.user_id, 'drone_id': drone_id}
        try:
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    ok = all(k in data for k in ('drone_id', 'user_id', 'drone_token'))
                    if not ok:
                        logger.warning('[api] register: в ответе нет нужных полей: %s', data)
                    return ok
                logger.warning('[api] register не удался: статус %s (ожидаемо для dev-пути без пользовательского JWT)', resp.status)
                return False
        except aiohttp.ClientError as exc:
            logger.error('[api] ошибка запроса register: %s', exc)
            return False
