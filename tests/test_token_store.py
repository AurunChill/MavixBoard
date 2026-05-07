import pytest
import mavixboard.token.store as store
from mavixboard.config import TOKEN_PATH


@pytest.fixture(autouse=True)
def patch_token_path(tmp_path, monkeypatch):
    token_file = tmp_path / TOKEN_PATH.name
    monkeypatch.setattr(store, "TOKEN_PATH", token_file)
    return token_file


def test_write_creates_file(patch_token_path):
    store.write("abc123")
    assert patch_token_path.exists()

def test_write_creates_parent_dir(patch_token_path):
    store.write("abc123")
    assert patch_token_path.parent.is_dir()

def test_write_and_get_roundtrip():
    token = "deadbeef"
    store.write(token)
    assert store.get() == token

def test_get_returns_empty_if_no_file():
    assert store.get() == ""

def test_write_wrong_type_raises_type_error():
    with pytest.raises(TypeError):
        store.write(12345)

def test_write_none_raises_type_error():
    with pytest.raises(TypeError):
        store.write(None)

def test_write_overwrites_existing():
    store.write("first")
    store.write("second")
    assert store.get() == "second"
