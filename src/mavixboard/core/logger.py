from __future__ import annotations

import logging

from mavixboard.core.config import settings

_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)

logger = logging.getLogger('mavixboard')
logger.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)


def setup_file_logging() -> None:
    handler = logging.FileHandler(settings.log_path)
    handler.setFormatter(_fmt)
    logger.addHandler(handler)
