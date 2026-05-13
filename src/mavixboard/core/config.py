import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load env in priority order:
#   1. /etc/mavixboard/preset.env — installed by the .deb on a real drone;
#      contains USER_ID and any system-wide settings the server baked in
#      at build time. Loaded WITHOUT override so a local .env on a dev
#      machine still wins for everything except what's locked.
#   2. ./.env (project local) — development override.
_PRESET_PATH = Path('/etc/mavixboard/preset.env')
if _PRESET_PATH.is_file():
    load_dotenv(_PRESET_PATH, override=False)
load_dotenv(override=True)

_BASE = Path.home() / '.config' / 'mavixboard'


def _find_project_root() -> Path | None:
    """Walk up from this file to find the dev source tree's pyproject.toml.
    Returns None when run from an installed package (no marker found)."""
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
    """Pick a writable directory for log files.

    .deb installations set MAVIXBOARD_LOG_DIR=/var/log/mavixboard via the
    systemd unit (writable for the mavixboard user). Dev runs from the
    source tree get <project>/_log. Falling back to ~/.local/state
    handles the case of `python -m mavixboard` from an installed package
    without the systemd env.
    """
    env_override = os.getenv('MAVIXBOARD_LOG_DIR')
    if env_override:
        return Path(env_override)
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT / '_log'
    return Path.home() / '.local' / 'state' / 'mavixboard'


def _resolve_data_dir() -> Path:
    """Same logic as _resolve_log_dir but for runtime data
    (camera calibration cache, etc)."""
    env_override = os.getenv('MAVIXBOARD_DATA_DIR')
    if env_override:
        return Path(env_override)
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT / '_data'
    return Path.home() / '.local' / 'share' / 'mavixboard'


@dataclass
class Settings:
    signal_server_ip: str = field(default_factory=lambda: os.getenv('SIGNAL_SERVER_IP', 'http://localhost'))
    signal_ws_url: str = field(default_factory=lambda: os.getenv('SIGNAL_WS_URL', ''))
    user_id: str = field(default_factory=lambda: os.getenv('USER_ID', ''))
    # DRONE_ID / DRONE_TOKEN are baked into /etc/mavixboard/preset.env at
    # .deb build time by the server. When both are set, the board uses
    # DRONE_TOKEN for WS auth and skips on-boot registration (the server
    # has already enrolled the drone). For dev runs without the .deb,
    # both are empty and __main__ falls back to the local token file.
    drone_id: str = field(default_factory=lambda: os.getenv('DRONE_ID', ''))
    drone_token: str = field(default_factory=lambda: os.getenv('DRONE_TOKEN', ''))
    stun_server: str = field(default_factory=lambda: os.getenv('STUN_SERVER', 'stun://localhost:3478'))
    turn_server: str = field(default_factory=lambda: os.getenv('TURN_SERVER', ''))

    token_path: Path = _BASE / 'token'
    log_path: Path = field(default_factory=lambda: _resolve_log_dir() / f'mavixboard_{date.today()}.log')
    data_path: Path = field(default_factory=_resolve_data_dir)

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
