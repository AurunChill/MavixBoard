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
