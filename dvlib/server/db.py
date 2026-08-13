"""SQLite persistence layer for the DVLib registry.

Single writer, short transactions — acceptable at company scale. Postgres is
the documented scaling path if write contention ever becomes an issue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    manifest TEXT NOT NULL,
    dependencies TEXT NOT NULL DEFAULT '{}',
    yanked INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class VersionRow:
    name: str
    version: str
    sha256: str
    manifest: dict
    dependencies: dict[str, str]
    yanked: bool
    published_at: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "manifest": self.manifest,
            "dependencies": self.dependencies,
            "yanked": self.yanked,
            "published_at": self.published_at,
        }


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- packages / versions ----

    def package_exists(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM packages WHERE name = ?", (name,)).fetchone()
        return row is not None

    def version_exists(self, name: str, version: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        return row is not None

    def insert_version(
        self,
        name: str,
        version: str,
        sha256: str,
        manifest: dict,
        dependencies: dict[str, str],
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO packages (name, created_at) VALUES (?, ?)",
                (name, now),
            )
            self._conn.execute(
                "INSERT INTO versions (name, version, sha256, manifest, dependencies,"
                " yanked, published_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (name, version, sha256, json.dumps(manifest), json.dumps(dependencies), now),
            )

    def _row_to_version(self, row: sqlite3.Row) -> VersionRow:
        return VersionRow(
            name=row["name"],
            version=row["version"],
            sha256=row["sha256"],
            manifest=json.loads(row["manifest"]),
            dependencies=json.loads(row["dependencies"]),
            yanked=bool(row["yanked"]),
            published_at=row["published_at"],
        )

    def get_versions(self, name: str) -> list[VersionRow] | None:
        """All versions of a package, or None if the package is unknown."""
        with self._lock:
            if not self.package_exists(name):
                return None
            rows = self._conn.execute("SELECT * FROM versions WHERE name = ?", (name,)).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, name: str, version: str) -> VersionRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        return self._row_to_version(row) if row else None

    def yank(self, name: str, version: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE versions SET yanked = 1 WHERE name = ? AND version = ?",
                (name, version),
            )
        return cur.rowcount > 0

    def search(self, query: str) -> list[dict]:
        """Name-substring search: name, latest (highest semver) version, description."""
        from ..common.semver import VersionError, parse_version

        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM packages WHERE name LIKE ?", (f"%{query}%",)
            ).fetchall()
            results = []
            for row in rows:
                versions = self.get_versions(row["name"]) or []
                visible = [v for v in versions if not v.yanked] or versions
                if not visible:
                    continue
                try:
                    latest = max(visible, key=lambda v: parse_version(v.version))
                except VersionError:
                    latest = visible[-1]
                results.append(
                    {
                        "name": row["name"],
                        "version": latest.version,
                        "description": latest.manifest.get("package", {}).get("description", ""),
                    }
                )
        return sorted(results, key=lambda r: r["name"])

    # ---- tokens ----

    def insert_token(self, token_hash: str, label: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tokens (token_hash, label, created_at, revoked) VALUES (?, ?, ?, 0)",
                (token_hash, label, _now()),
            )

    def revoke_token(self, token_hash: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,)
            )
        return cur.rowcount > 0

    def token_valid(self, token_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT revoked FROM tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return row is not None and not row["revoked"]

    def list_tokens(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT label, created_at, revoked FROM tokens ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
