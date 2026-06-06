"""Саморегистрация дрона при первом запуске.

board вшит только ADMIN_ID + ENROLLMENT_TOKEN. При первом старте он сам
генерирует DRONE_ID, регистрируется на сервере (POST /api/v1/drones/enroll,
аутентификация — не JWT, а ENROLLMENT_TOKEN админа) и получает персональный
DRONE_TOKEN и имя, которые дописываются в env-файл — на следующих запусках
саморегистрация пропускается.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import aiohttp

from mavixboard.core.logger import logger

_ENROLL_ATTEMPTS = 5
_IDENTITY_KEYS = ('DRONE_ID', 'DRONE_TOKEN', 'DRONE_NAME')


class EnrollError(RuntimeError):
    """Невосстановимая ошибка саморегистрации (нет креды, отказ сервера)."""


class EnrollConflict(RuntimeError):
    """drone_id уже занят — нужно перегенерировать и повторить."""


#### HTTP ##############################################################################
async def request_enroll(base_url: str, admin_id: str, enrollment_token: str, drone_id: str) -> dict[str, str]:
    """POST /api/v1/drones/enroll. Возвращает {drone_id, drone_token, name}."""
    url = base_url.rstrip('/') + '/api/v1/drones/enroll'
    headers = {'Authorization': f'Enroll {enrollment_token}'}
    body = {'admin_id': admin_id, 'drone_id': drone_id}
    async with aiohttp.ClientSession() as session, session.post(url, json=body, headers=headers) as resp:
        if resp.status == 409:
            raise EnrollConflict('drone_id занят')
        if resp.status != 201:
            text = await resp.text()
            raise EnrollError(f'enroll отклонён сервером: {resp.status} {text}')
        data: dict[str, str] = await resp.json()
        return data


#### Персистентность ###################################################################
def persist_identity(env_path: Path, drone_id: str, drone_token: str, name: str) -> None:
    """Дописывает/обновляет DRONE_ID/DRONE_TOKEN/DRONE_NAME в env-файле."""
    values = {'DRONE_ID': drone_id, 'DRONE_TOKEN': drone_token, 'DRONE_NAME': name}
    lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line else None
        if key in values:
            out.append(f'{key}={values[key]}')
            seen.add(key)
        else:
            out.append(line)
    for key in _IDENTITY_KEYS:
        if key not in seen:
            out.append(f'{key}={values[key]}')
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text('\n'.join(out) + '\n', encoding='utf-8')


#### Оркестрация #######################################################################
async def ensure_enrolled(
    *,
    base_url: str,
    admin_id: str,
    enrollment_token: str,
    drone_id: str,
    drone_token: str,
    drone_name: str,
    env_path: Path,
    gen_id: Callable[[], str] = lambda: uuid4().hex,
) -> tuple[str, str, str]:
    """Гарантирует, что у board есть DRONE_ID/DRONE_TOKEN.

    Если они уже заданы — возвращает как есть. Иначе генерирует drone_id,
    регистрируется (с повтором при конфликте id) и сохраняет результат.
    Возвращает (drone_id, drone_token, drone_name).
    """
    if drone_id and drone_token:
        logger.info('[enroll] уже зарегистрирован, drone_id=%s', drone_id)
        return drone_id, drone_token, drone_name
    if not admin_id or not enrollment_token:
        raise EnrollError('нет ADMIN_ID/ENROLLMENT_TOKEN для саморегистрации')
    for _ in range(_ENROLL_ATTEMPTS):
        candidate = gen_id()
        try:
            data = await request_enroll(base_url, admin_id, enrollment_token, candidate)
        except EnrollConflict:
            logger.warning('[enroll] drone_id занят, пробую другой')
            continue
        new_id = data['drone_id']
        new_token = data['drone_token']
        new_name = data.get('name') or ''
        persist_identity(env_path, new_id, new_token, new_name)
        logger.info('[enroll] зарегистрирован drone_id=%s name=%s', new_id, new_name)
        return new_id, new_token, new_name
    raise EnrollError('не удалось подобрать свободный drone_id')
