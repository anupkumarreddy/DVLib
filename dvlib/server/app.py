"""FastAPI registry application.

Open read access; bearer-token auth for publish/yank. Tokens are stored
hashed (sha256) and managed via `dvlib-server tokens ...`.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import __version__
from ..common.manifest import MANIFEST_FILENAME, ManifestError, load_manifest
from ..common.tarball import MAX_ARCHIVE_BYTES, TarballError, safe_extract, sha256_bytes
from .db import Database
from .storage import Storage, StorageError

__all__ = ["create_app", "hash_token"]

API_PREFIX = "/api/v1"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_app(data_dir: Path | str) -> FastAPI:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "registry.db")
    storage = Storage(data_dir)
    bearer = HTTPBearer(auto_error=False)
    publish_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db.close()

    app = FastAPI(title="DVLib Registry", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.storage = storage

    def require_token(
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> str:
        if creds is None or creds.scheme.lower() != "bearer":
            raise HTTPException(401, "bearer token required")
        if not db.token_valid(hash_token(creds.credentials)):
            raise HTTPException(403, "invalid or revoked token")
        return creds.credentials

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/packages")
    def search_packages(q: str = "") -> list[dict]:
        return db.search(q)

    @app.get(f"{API_PREFIX}/packages/{{name}}")
    def package_detail(name: str) -> dict:
        versions = db.get_versions(name)
        if versions is None:
            raise HTTPException(404, f"package {name!r} not found")
        return {"name": name, "versions": [v.to_dict() for v in versions]}

    @app.get(f"{API_PREFIX}/packages/{{name}}/{{version}}")
    def version_detail(name: str, version: str) -> dict:
        row = db.get_version(name, version)
        if row is None:
            raise HTTPException(404, f"package {name}@{version} not found")
        return row.to_dict()

    @app.get(f"{API_PREFIX}/packages/{{name}}/{{version}}/tarball")
    def tarball(name: str, version: str) -> Response:
        if db.get_version(name, version) is None:
            raise HTTPException(404, f"package {name}@{version} not found")
        try:
            data = storage.read(name, version)
        except StorageError as e:
            raise HTTPException(400, str(e)) from e
        if data is None:
            raise HTTPException(404, "tarball missing from storage")
        return Response(content=data, media_type="application/gzip")

    @app.post(f"{API_PREFIX}/packages", status_code=201)
    def publish(file: UploadFile, _token: str = Depends(require_token)) -> dict:
        data = file.file.read(MAX_ARCHIVE_BYTES + 1)
        if not data:
            raise HTTPException(400, "empty upload")
        if len(data) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, f"upload exceeds {MAX_ARCHIVE_BYTES} byte limit")
        # Validate by extracting to a temp dir and parsing the manifest.
        raw_manifest: dict = {}
        with tempfile.TemporaryDirectory(prefix="dvlib-publish-") as tmp:
            try:
                safe_extract(data, tmp)
            except (TarballError, OSError, EOFError) as e:
                raise HTTPException(400, f"invalid tarball: {e}") from e
            if not (Path(tmp) / MANIFEST_FILENAME).is_file():
                raise HTTPException(400, f"tarball lacks {MANIFEST_FILENAME}")
            try:
                manifest = load_manifest(tmp)
            except ManifestError as e:
                raise HTTPException(422, str(e)) from e
            # Every declared file must be present in the tarball.
            missing = [f for f in manifest.files if not (Path(tmp) / f).is_file()]
            if missing:
                raise HTTPException(
                    422, f"declared files missing from tarball: {', '.join(missing)}"
                )
            raw_manifest = tomllib.loads((Path(tmp) / MANIFEST_FILENAME).read_text())
        digest = sha256_bytes(data)
        # Serialize the final check/store/record sequence so concurrent
        # attempts cannot overwrite an already-published immutable version.
        with publish_lock:
            if db.version_exists(manifest.name, manifest.version_str):
                raise HTTPException(
                    409, f"{manifest.name}@{manifest.version_str} already published"
                )
            try:
                storage.store(manifest.name, manifest.version_str, data)
            except StorageError as e:
                raise HTTPException(400, str(e)) from e
            db.insert_version(
                manifest.name,
                manifest.version_str,
                digest,
                raw_manifest,
                {k: str(v) for k, v in manifest.dependencies.items()},
            )
        return {
            "name": manifest.name,
            "version": manifest.version_str,
            "sha256": digest,
        }

    @app.post(f"{API_PREFIX}/packages/{{name}}/{{version}}/yank")
    def yank(name: str, version: str, _token: str = Depends(require_token)) -> dict:
        if not db.yank(name, version):
            raise HTTPException(404, f"package {name}@{version} not found")
        return {"name": name, "version": version, "yanked": True}

    return app
