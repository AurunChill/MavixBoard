from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Загружаем env в порядке приоритета:
#   1. /etc/mavixboard/preset.env — устанавливается install.sh на реальном дроне;
#      содержит USER_ID и любые системные настройки, которые сервер зашил на
#      этапе сборки.
#   2. ./.env (локально в проекте) — переопределение для разработки.
_PRESET_PATH = Path('/etc/mavixboard/preset.env')
if _PRESET_PATH.is_file():
    load_dotenv(_PRESET_PATH, override=False)
load_dotenv(override=True)


#### Разрешение путей ##################################################################
def _find_project_root() -> Path | None:
    """Поднимается вверх от этого файла в поисках pyproject.toml dev-дерева.

    Возвращает None при запуске из установленного пакета (маркер не найден).
    """
    cur = Path(__file__).resolve().parent
    for _ in range(6):
        if (cur / 'pyproject.toml').is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


_PROJECT_ROOT = _find_project_root()


def _resolve_log_dir() -> Path:
    """Выбирает доступную для записи директорию под лог-файлы.

    Production-установка (install.sh) задаёт MAVIXBOARD_LOG_DIR=/var/log/mavixboard
    через systemd-юнит (доступно для записи пользователю mavixboard). Запуски из
    исходников получают <project>/_log. Откат на ~/.local/state покрывает
    случай `python -m mavixboard` из установленного пакета без systemd-окружения.
    """
    env_override = os.getenv('MAVIXBOARD_LOG_DIR')
    if env_override:
        return Path(env_override)
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT / '_log'
    return Path.home() / '.local' / 'state' / 'mavixboard'


def _resolve_data_dir() -> Path:
    """Та же логика, что и в _resolve_log_dir, но для runtime-данных
    (кэш калибровки камер и т.п.)."""
    env_override = os.getenv('MAVIXBOARD_DATA_DIR')
    if env_override:
        return Path(env_override)
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT / '_data'
    return Path.home() / '.local' / 'share' / 'mavixboard'


def _resolve_identity_env_path() -> Path:
    """Файл, куда board дописывает DRONE_ID/DRONE_TOKEN/DRONE_NAME после
    саморегистрации.

    На реальном дроне это preset.env (его install.sh делает доступным для
    записи сервису). При запуске из исходников — ./.env проекта.
    """
    if _PRESET_PATH.is_file():
        return _PRESET_PATH
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT / '.env'
    return Path.home() / '.config' / 'mavixboard' / 'preset.env'


#### Настройки #########################################################################
@dataclass
class Settings:
    signal_server_ip: str = field(default_factory=lambda: os.getenv('SIGNAL_SERVER_IP', 'http://localhost'))
    signal_ws_url: str = field(default_factory=lambda: os.getenv('SIGNAL_WS_URL', ''))
    # ADMIN_ID + ENROLLMENT_TOKEN вшиваются сервером в preset.env; по ним board
    # сам регистрируется при первом запуске и получает DRONE_ID/DRONE_TOKEN/NAME.
    admin_id: str = field(default_factory=lambda: os.getenv('ADMIN_ID', ''))
    enrollment_token: str = field(default_factory=lambda: os.getenv('ENROLLMENT_TOKEN', ''))
    drone_id: str = field(default_factory=lambda: os.getenv('DRONE_ID', ''))
    drone_token: str = field(default_factory=lambda: os.getenv('DRONE_TOKEN', ''))
    drone_name: str = field(default_factory=lambda: os.getenv('DRONE_NAME', ''))
    stun_server: str = field(default_factory=lambda: os.getenv('STUN_SERVER', 'stun://stun.l.google.com:19302'))
    turn_server: str = field(default_factory=lambda: os.getenv('TURN_SERVER', ''))
    turn_username: str = field(default_factory=lambda: os.getenv('TURN_USERNAME', ''))
    turn_password: str = field(default_factory=lambda: os.getenv('TURN_PASSWORD', ''))

    # SITL/симуляция: если задано — board подключается к MAVLink по этому URL
    # (`udpin:127.0.0.1:14540` для PX4 SITL Gazebo, `tcp:host:port` и т.п.)
    # вместо опроса /dev/tty*. Реальный полётник в этом режиме не ищется.
    mavlink_url: str = field(default_factory=lambda: os.getenv('MAVLINK_URL', ''))

    # SITL/симуляция: аналогично для CRSF. Прямой URL pyserial — без socat/pty.
    # Пример: `socket://127.0.0.1:5764` (TCP-порт UART4 в Betaflight SITL).
    # Если задано — board подключается сразу к этому URL и не сканит /dev/tty*.
    crsf_url: str = field(default_factory=lambda: os.getenv('CRSF_URL', ''))

    # Отладка/проблемный NAT: если True, webrtcbin получает
    # ice-transport-policy=relay и использует ТОЛЬКО relay-кандидаты
    # (host/srflx отбрасываются). Аналог force_relay в desktop. Нужно,
    # когда прямые и srflx-пары не поднимаются и связь должна гарантированно
    # идти через TURN. Включается env FORCE_RELAY=1/true/yes/on.
    force_relay: bool = field(
        default_factory=lambda: os.getenv('FORCE_RELAY', '').strip().lower() in ('1', 'true', 'yes', 'on')
    )

    log_path: Path = field(default_factory=lambda: _resolve_log_dir() / f'mavixboard_{date.today()}.log')
    data_path: Path = field(default_factory=_resolve_data_dir)
    identity_env_path: Path = field(default_factory=_resolve_identity_env_path)

    @property
    def ws_url(self) -> str:
        if self.signal_ws_url:
            return self.signal_ws_url
        base = self.signal_server_ip
        if base.startswith('https://'):
            return 'wss://' + base[len('https://'):].rstrip('/') + '/ws/drone'
        if base.startswith('http://'):
            return 'ws://' + base[len('http://'):].rstrip('/') + '/ws/drone'
        return base.rstrip('/') + '/ws/drone'


settings = Settings()
