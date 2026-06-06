"""Тесты саморегистрации дрона (server/enroll)."""
from __future__ import annotations

import pytest
from aioresponses import aioresponses

from mavixboard.server.enroll import (
    EnrollConflict,
    EnrollError,
    ensure_enrolled,
    persist_identity,
    request_enroll,
)

_URL = 'http://test/api/v1/drones/enroll'


#### persist_identity ##################################################################
def test_persist_creates_file_with_identity(tmp_path) -> None:
    env = tmp_path / '.env'
    persist_identity(env, 'drone-1', 'tok-1', 'весёлый-кит')
    text = env.read_text(encoding='utf-8')
    assert 'DRONE_ID=drone-1' in text
    assert 'DRONE_TOKEN=tok-1' in text
    assert 'DRONE_NAME=весёлый-кит' in text


def test_persist_updates_existing_and_preserves_others(tmp_path) -> None:
    env = tmp_path / '.env'
    env.write_text('ADMIN_ID=a1\nDRONE_ID=old\nSIGNAL_SERVER_IP=http://x\n', encoding='utf-8')
    persist_identity(env, 'new-id', 'new-tok', 'имя')
    lines = env.read_text(encoding='utf-8').splitlines()
    assert 'ADMIN_ID=a1' in lines
    assert 'SIGNAL_SERVER_IP=http://x' in lines
    assert 'DRONE_ID=new-id' in lines
    assert lines.count('DRONE_ID=new-id') == 1  # обновлено, не задвоено


#### request_enroll ####################################################################
async def test_request_enroll_returns_identity_on_201() -> None:
    with aioresponses() as m:
        m.post(_URL, status=201, payload={'drone_id': 'd1', 'drone_token': 't1', 'name': 'имя'})
        data = await request_enroll('http://test', 'admin-1', 'enroll-tok', 'd1')
    assert data == {'drone_id': 'd1', 'drone_token': 't1', 'name': 'имя'}


async def test_request_enroll_409_raises_conflict() -> None:
    with aioresponses() as m:
        m.post(_URL, status=409)
        with pytest.raises(EnrollConflict):
            await request_enroll('http://test', 'admin-1', 'enroll-tok', 'd1')


async def test_request_enroll_error_status_raises() -> None:
    with aioresponses() as m:
        m.post(_URL, status=401, body='bad token')
        with pytest.raises(EnrollError):
            await request_enroll('http://test', 'admin-1', 'enroll-tok', 'd1')


#### ensure_enrolled ###################################################################
async def test_ensure_enrolled_returns_existing_without_http(tmp_path) -> None:
    # уже есть токен — HTTP не нужен (aioresponses не зарегистрирован → упал бы)
    result = await ensure_enrolled(
        base_url='http://test', admin_id='a', enrollment_token='e',
        drone_id='d', drone_token='t', drone_name='имя', env_path=tmp_path / '.env',
    )
    assert result == ('d', 't', 'имя')


async def test_ensure_enrolled_without_credentials_raises(tmp_path) -> None:
    with pytest.raises(EnrollError):
        await ensure_enrolled(
            base_url='http://test', admin_id='', enrollment_token='',
            drone_id='', drone_token='', drone_name='', env_path=tmp_path / '.env',
        )


async def test_ensure_enrolled_registers_and_persists(tmp_path) -> None:
    env = tmp_path / '.env'
    with aioresponses() as m:
        m.post(_URL, status=201, payload={'drone_id': 'gen-1', 'drone_token': 'tok', 'name': 'кит'})
        drone_id, token, name = await ensure_enrolled(
            base_url='http://test', admin_id='a', enrollment_token='e',
            drone_id='', drone_token='', drone_name='', env_path=env,
            gen_id=lambda: 'gen-1',
        )
    assert (drone_id, token, name) == ('gen-1', 'tok', 'кит')
    assert 'DRONE_TOKEN=tok' in env.read_text(encoding='utf-8')


async def test_ensure_enrolled_retries_on_conflict(tmp_path) -> None:
    ids = iter(['busy', 'free'])
    with aioresponses() as m:
        m.post(_URL, status=409)  # первый id занят
        m.post(_URL, status=201, payload={'drone_id': 'free', 'drone_token': 'tok', 'name': 'имя'})
        drone_id, token, _ = await ensure_enrolled(
            base_url='http://test', admin_id='a', enrollment_token='e',
            drone_id='', drone_token='', drone_name='', env_path=tmp_path / '.env',
            gen_id=lambda: next(ids),
        )
    assert (drone_id, token) == ('free', 'tok')
