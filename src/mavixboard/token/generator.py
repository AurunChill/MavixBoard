from secrets import token_hex

def generate(length: int) -> str:
    """Generate a cryptographically secure hex token

    Args:
        length: Desired token length in characters. Must be an int.

    Returns:
        Hex string of exactly `length` chars, or '' if length <= 0.

    Raises:
        TypeError: If `length` is not an int.
    """
    if not isinstance(length, int):
        raise TypeError(f"length must be int, got {type(length).__name__}")
    if length <= 0:
        return ''
    token = token_hex((length // 2) + 1)
    return token[:-2] if length % 2 == 0 else token[:-1]
    