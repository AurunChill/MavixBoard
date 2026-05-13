from mavixboard.core.config import Settings


def test_ws_url_from_http():
    s = Settings(signal_server_ip='http://example.com:8000', signal_ws_url='')
    assert s.ws_url == 'ws://example.com:8000/ws/drone'


def test_ws_url_from_https():
    s = Settings(signal_server_ip='https://example.com', signal_ws_url='')
    assert s.ws_url == 'wss://example.com/ws/drone'


def test_ws_url_trims_trailing_slash():
    s = Settings(signal_server_ip='http://example.com:8000/', signal_ws_url='')
    assert s.ws_url == 'ws://example.com:8000/ws/drone'


def test_ws_url_explicit_override():
    s = Settings(
        signal_server_ip='http://nope.example.com',
        signal_ws_url='ws://override.example.com/custom-path',
    )
    assert s.ws_url == 'ws://override.example.com/custom-path'


def test_ws_url_unscheme_passthrough():
    s = Settings(signal_server_ip='localhost:8000', signal_ws_url='')
    assert s.ws_url == 'localhost:8000/ws/drone'


# ---------- preset.env support ----------

def test_preset_env_loaded_when_present(tmp_path, monkeypatch):
    """If /etc/mavixboard/preset.env exists at import time, its values are
    available via os.environ. Local .env still overrides them so dev work
    isn't blocked by a stale preset on the dev machine."""
    import importlib
    from pathlib import Path
    preset = tmp_path / 'preset.env'
    preset.write_text('USER_ID=preset-user-xyz\n')
    monkeypatch.setattr(
        'mavixboard.core.config._PRESET_PATH', Path(str(preset)),
    )
    # Re-execute the dotenv load behaviour
    from dotenv import load_dotenv
    monkeypatch.delenv('USER_ID', raising=False)
    load_dotenv(preset, override=False)
    import os
    assert os.environ.get('USER_ID') == 'preset-user-xyz'


def test_local_env_overrides_preset(tmp_path, monkeypatch):
    """A USER_ID already in os.environ (e.g. from local .env loaded later
    with override=True) wins over the preset."""
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    preset = tmp_path / 'preset.env'
    preset.write_text('USER_ID=preset-user-xyz\n')
    local = tmp_path / 'local.env'
    local.write_text('USER_ID=local-user-abc\n')

    monkeypatch.delenv('USER_ID', raising=False)
    # Simulate the config.py load order
    load_dotenv(preset, override=False)
    load_dotenv(local, override=True)
    assert os.environ.get('USER_ID') == 'local-user-abc'


# ---------- DRONE_ID / DRONE_TOKEN from preset.env ----------
# The server bakes these into preset.env at .deb build time. Settings
# must read them so the board can use DRONE_TOKEN for WS auth instead
# of a locally-generated random token (which the server doesn't know).

def test_drone_token_and_drone_id_read_from_env(monkeypatch):
    monkeypatch.setenv('DRONE_ID', 'd-from-env')
    monkeypatch.setenv('DRONE_TOKEN', 'tok-from-env')
    s = Settings()
    assert s.drone_id == 'd-from-env'
    assert s.drone_token == 'tok-from-env'


def test_drone_token_defaults_empty_in_dev(monkeypatch):
    """No preset.env, no local .env → both stay empty so __main__ falls
    back to local file generation."""
    monkeypatch.delenv('DRONE_ID', raising=False)
    monkeypatch.delenv('DRONE_TOKEN', raising=False)
    s = Settings()
    assert s.drone_id == ''
    assert s.drone_token == ''


def test_drone_token_loaded_from_preset_env_file(tmp_path, monkeypatch):
    """preset.env content reaches Settings via dotenv."""
    from dotenv import load_dotenv
    preset = tmp_path / 'preset.env'
    preset.write_text(
        'USER_ID=u-1\n'
        'DRONE_ID=d-1\n'
        'DRONE_TOKEN=tok-1\n'
        'SIGNAL_SERVER_IP=http://srv:8000\n'
    )
    for key in ('USER_ID', 'DRONE_ID', 'DRONE_TOKEN', 'SIGNAL_SERVER_IP'):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(preset, override=False)
    s = Settings()
    assert s.user_id == 'u-1'
    assert s.drone_id == 'd-1'
    assert s.drone_token == 'tok-1'
    assert s.signal_server_ip == 'http://srv:8000'
