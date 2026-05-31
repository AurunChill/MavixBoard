from __future__ import annotations

from secrets import token_hex


def generate(length: int) -> str:
    """Генерирует криптографически стойкий hex-токен.

    Возвращает hex-строку ровно из `length` символов, либо '' при length <= 0.
    Бросает TypeError, если `length` не int.
    """
    if not isinstance(length, int):
        raise TypeError(f'length должен быть int, получен {type(length).__name__}')
    if length <= 0:
        return ''
    token = token_hex((length // 2) + 1)
    return token[:-2] if length % 2 == 0 else token[:-1]
