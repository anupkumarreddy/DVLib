"""dvlib.lock read/write/verify.

JSON format, lock format version 1:

{
  "version": 1,
  "registry": "http://localhost:8000",
  "packages": {
    "uvm_kit": {"version": "1.4.2", "sha256": "<hex>", "dev": false}
  }
}
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import NAME_RE
from .semver import Version, parse_version

__all__ = ["Lockfile", "LockEntry", "LockfileError", "LOCK_FILENAME", "LOCK_FORMAT_VERSION"]

LOCK_FILENAME = "dvlib.lock"
LOCK_FORMAT_VERSION = 1


class LockfileError(ValueError):
    """Raised when a lockfile is missing, malformed, or an unsupported version."""


@dataclass
class LockEntry:
    version: Version
    sha256: str
    dev: bool = False


@dataclass
class Lockfile:
    registry: str = ""
    packages: dict[str, LockEntry] = field(default_factory=dict)
    format_version: int = LOCK_FORMAT_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.format_version,
            "registry": self.registry,
            "packages": {
                name: {
                    "version": str(e.version),
                    "sha256": e.sha256,
                    "dev": e.dev,
                }
                for name, e in sorted(self.packages.items())
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> Lockfile:
        if not isinstance(data, dict):
            raise LockfileError("lockfile is not a JSON object")
        if data.get("version") != LOCK_FORMAT_VERSION:
            raise LockfileError(
                f"unsupported lock format version {data.get('version')!r} "
                f"(expected {LOCK_FORMAT_VERSION})"
            )
        registry = data.get("registry", "")
        if not isinstance(registry, str):
            raise LockfileError("lockfile 'registry' must be a string")
        raw_pkgs = data.get("packages")
        if not isinstance(raw_pkgs, dict):
            raise LockfileError("lockfile 'packages' must be an object")
        packages: dict[str, LockEntry] = {}
        for name, entry in raw_pkgs.items():
            try:
                if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                    raise ValueError("bad package name")
                if not isinstance(entry, dict):
                    raise TypeError("entry is not an object")
                version = parse_version(entry["version"])
                sha256 = entry["sha256"]
                if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                    raise ValueError("bad sha256")
                dev = entry.get("dev", False)
                if not isinstance(dev, bool):
                    raise TypeError("dev is not a boolean")
            except (KeyError, ValueError, TypeError) as e:
                raise LockfileError(f"lockfile entry for {name!r} is malformed: {e}") from e
            packages[name] = LockEntry(version=version, sha256=sha256, dev=dev)
        return cls(registry=registry, packages=packages)


def load_lockfile(path: Path | str) -> Lockfile | None:
    """Load dvlib.lock from a directory. Returns None if absent; raises
    LockfileError if present but malformed."""
    p = Path(path)
    if p.is_dir():
        p = p / LOCK_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise LockfileError(f"{p}: invalid JSON: {e}") from e
    try:
        return Lockfile.from_dict(data)
    except LockfileError as e:
        raise LockfileError(f"{p}: {e}") from e


def write_lockfile(lock: Lockfile, directory: Path | str) -> Path:
    p = Path(directory) / LOCK_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(lock.to_dict(), indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=p.parent, prefix=".dvlib-lock-", delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, p)
        finally:
            temporary.unlink(missing_ok=True)
    return p
