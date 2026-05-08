import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

_BASE = Path.home() / '.config' / 'mavixboard'
_PROJECT_ROOT = Path(__file__).parents[2]


@dataclass
class Settings:
    signal_server_ip: str = field(default_factory=lambda: os.getenv('SIGNAL_SERVER_IP', 'http://localhost'))
    user_id: str = field(default_factory=lambda: os.getenv('USER_ID', ''))
    stun_server: str = field(default_factory=lambda: os.getenv('STUN_SERVER', 'stun://localhost:3478'))
    turn_server: str = field(default_factory=lambda: os.getenv('TURN_SERVER', ''))

    token_path: Path = _BASE / 'token'
    log_path: Path = field(default_factory=lambda: _PROJECT_ROOT / '_log' / f'mavixboard_{date.today()}.log')
    data_path: Path = _PROJECT_ROOT / '_data'


settings = Settings()
