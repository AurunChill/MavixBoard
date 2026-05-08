from mavixboard.core.config import settings

TOKEN_PATH = settings.token_path


def get() -> str:
    """Read the stored token from disk. 
    
    Returns token if exists
    """
    return TOKEN_PATH.read_text() if TOKEN_PATH.exists() else ""


def write(token: str) -> None:
    """Write the token to disk, creating the directory if needed.

    Raises:
        TypeError: If token is not a str.
    """
    if not isinstance(token, str):
        raise TypeError(f"token must be str, got {type(token).__name__}")
    TOKEN_PATH.write_text(token)
