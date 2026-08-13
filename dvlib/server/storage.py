"""On-disk tarball storage layout: <data_dir>/packages/<name>/<version>.tar.gz"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..common.manifest import NAME_RE

__all__ = ["Storage", "StorageError"]


class StorageError(ValueError):
    pass


class Storage:
    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "packages"
        self.root.mkdir(parents=True, exist_ok=True)

    def _check(self, name: str, version: str) -> None:
        if not NAME_RE.match(name):
            raise StorageError(f"invalid package name: {name!r}")
        # Version strings are validated semver elsewhere; belt-and-braces here
        # against path traversal via crafted versions.
        if "/" in version or "\\" in version or version in ("", ".", ".."):
            raise StorageError(f"invalid version string: {version!r}")

    def tarball_path(self, name: str, version: str) -> Path:
        self._check(name, version)
        return self.root / name / f"{version}.tar.gz"

    def store(self, name: str, version: str, data: bytes) -> Path:
        path = self.tarball_path(name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def read(self, name: str, version: str) -> bytes | None:
        path = self.tarball_path(name, version)
        if not path.is_file():
            return None
        return path.read_bytes()
