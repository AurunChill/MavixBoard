import pytest
import mavixboard.token.storage as storage
from mavixboard.config import settings


@pytest.fixture(autouse=True)
def patch_token_path(tmp_path, monkeypatch):
    token_file = tmp_path / settings.token_path.name
    monkeypatch.setattr(storage, "TOKEN_PATH", token_file)
    return token_file


def test_write_creates_file(patch_token_path):
    storage.write("abc123")
    assert patch_token_path.exists()

def test_write_creates_parent_dir(patch_token_path):
    storage.write("abc123")
    assert patch_token_path.parent.is_dir()

def test_write_and_get_roundtrip():
    token = "deadbeef"
    storage.write(token)
    assert storage.get() == token

def test_get_returns_empty_if_no_file():
    assert storage.get() == ""

def test_write_wrong_type_raises_type_error():
    with pytest.raises(TypeError):
        storage.write(12345)

def test_write_none_raises_type_error():
    with pytest.raises(TypeError):
        storage.write(None)

def test_write_overwrites_existing():
    storage.write("first")
    storage.write("second")
    assert storage.get() == "second"
