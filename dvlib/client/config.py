"""Client configuration: registry URL, credentials, cache location."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

__all__ = [
    "DEFAULT_REGISTRY",
    "config_dir",
    "credentials_path",
    "cache_dir",
    "resolve_registry",
    "load_token",
    "store_token",
]

DEFAULT_REGISTRY = "http://localhost:8000"


def config_dir() -> Path:
    if env := os.environ.get("DVLIB_CONFIG_HOME"):
        return Path(env)
    return Path.home() / ".config" / "dvlib"


def credentials_path() -> Path:
    return config_dir() / "credentials.toml"


def cache_dir() -> Path:
    if env := os.environ.get("DVLIB_CACHE"):
        return Path(env)
    return Path.home() / ".cache" / "dvlib"


def resolve_registry(flag: str | None = None) -> str:
    """Registry URL: --registry flag > DVLIB_REGISTRY env > default."""
    url = flag or os.environ.get("DVLIB_REGISTRY") or DEFAULT_REGISTRY
    return url.rstrip("/")


def load_token(registry: str) -> str | None:
    """DVLIB_TOKEN env var overrides the credentials file."""
    if env := os.environ.get("DVLIB_TOKEN"):
        return env
    path = credentials_path()
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    registries = data.get("registry", {})
    if not isinstance(registries, dict):
        return None
    entry = registries.get(registry, {})
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    return token if isinstance(token, str) else None


def store_token(registry: str, token: str) -> Path:
    """Persist a token in credentials.toml (TOML written manually to avoid a
    tomlkit dependency for one trivial write)."""
    path = credentials_path()
    data: dict = {"registry": {}}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {"registry": {}}
    if not isinstance(data.get("registry"), dict):
        data["registry"] = {}
    data["registry"][registry] = {"token": token}
    lines = []
    for url, entry in sorted(data["registry"].items()):
        if not isinstance(url, str) or not isinstance(entry, dict):
            continue
        stored_token = entry.get("token")
        if not isinstance(stored_token, str):
            continue
        # JSON strings are valid TOML basic strings and correctly escape quotes,
        # backslashes, and control characters.
        lines.append(f"[registry.{json.dumps(url)}]")
        lines.append(f"token = {json.dumps(stored_token)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(lines).encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".credentials-", delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path
