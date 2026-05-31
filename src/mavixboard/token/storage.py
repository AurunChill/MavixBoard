from __future__ import annotations

from mavixboard.core.config import settings

TOKEN_PATH = settings.token_path


def get() -> str:
    """Читает сохранённый токен с диска, возвращая его при наличии, иначе ''."""
    return TOKEN_PATH.read_text() if TOKEN_PATH.exists() else ''


def write(token: str) -> None:
    """Записывает токен на диск, создавая директорию при необходимости.

    Бросает TypeError, если token не str.
    """
    if not isinstance(token, str):
        raise TypeError(f'token должен быть str, получен {type(token).__name__}')
    TOKEN_PATH.write_text(token)
