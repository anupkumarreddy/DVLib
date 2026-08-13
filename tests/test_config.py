import tomllib

from dvlib.client.config import load_token, store_token


def test_credentials_roundtrip_escapes_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("DVLIB_CONFIG_HOME", str(tmp_path))
    registry = 'https://registry.example/path?label="team"'
    token = 'secret"with\\escapes\nand-newline'

    path = store_token(registry, token)

    assert load_token(registry) == token
    assert tomllib.loads(path.read_text())["registry"][registry]["token"] == token
    assert path.stat().st_mode & 0o777 == 0o600
