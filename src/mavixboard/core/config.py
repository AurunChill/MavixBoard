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
    signal_ws_url: str = field(default_factory=lambda: os.getenv('SIGNAL_WS_URL', ''))
    user_id: str = field(default_factory=lambda: os.getenv('USER_ID', ''))
    stun_server: str = field(default_factory=lambda: os.getenv('STUN_SERVER', 'stun://localhost:3478'))
    turn_server: str = field(default_factory=lambda: os.getenv('TURN_SERVER', ''))

    token_path: Path = _BASE / 'token'
    log_path: Path = field(default_factory=lambda: _PROJECT_ROOT / '_log' / f'mavixboard_{date.today()}.log')
    data_path: Path = _PROJECT_ROOT / '_data'

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
