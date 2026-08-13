"""httpx wrapper around the registry REST API."""

from __future__ import annotations

import httpx

from ..common.semver import Version, parse_version
from ..common.tarball import MAX_ARCHIVE_BYTES
from .resolver import VersionMetadata

__all__ = ["RegistryClient", "RegistryError"]

API = "/api/v1"


class RegistryError(Exception):
    pass


class RegistryClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            detail = resp.headers.get("content-type", "").startswith("application/json")
            msg = resp.json().get("detail", resp.text) if detail else resp.text
            raise RegistryError(f"{what}: HTTP {resp.status_code}: {msg}")
        return resp

    # ---- read ----

    def get_versions(self, name: str) -> list[VersionMetadata] | None:
        """Implements the resolver's PackageIndex protocol."""
        resp = self._http.get(f"{API}/packages/{name}")
        if resp.status_code == 404:
            return None
        self._check(resp, f"fetch {name}")
        out = []
        for v in resp.json()["versions"]:
            out.append(
                VersionMetadata(
                    name=name,
                    version=parse_version(v["version"]),
                    dependencies=v.get("dependencies", {}),
                    sha256=v["sha256"],
                    yanked=v.get("yanked", False),
                )
            )
        return out

    def version_detail(self, name: str, version: str) -> dict | None:
        resp = self._http.get(f"{API}/packages/{name}/{version}")
        if resp.status_code == 404:
            return None
        self._check(resp, f"fetch {name}@{version}")
        return resp.json()

    def search(self, query: str = "") -> list[dict]:
        resp = self._http.get(f"{API}/packages", params={"q": query})
        self._check(resp, "search")
        return resp.json()

    def download_tarball(self, name: str, version: Version) -> bytes:
        what = f"download {name}@{version}"
        with self._http.stream("GET", f"{API}/packages/{name}/{version}/tarball") as resp:
            if resp.status_code >= 400:
                resp.read()
                self._check(resp, what)
            declared_size = resp.headers.get("content-length")
            if declared_size and declared_size.isdigit() and int(declared_size) > MAX_ARCHIVE_BYTES:
                raise RegistryError(f"{what}: archive exceeds {MAX_ARCHIVE_BYTES} byte limit")
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise RegistryError(f"{what}: archive exceeds {MAX_ARCHIVE_BYTES} byte limit")
                chunks.append(chunk)
            return b"".join(chunks)

    # ---- write ----

    def publish(self, tarball: bytes) -> dict:
        resp = self._http.post(
            f"{API}/packages",
            files={"file": ("package.tar.gz", tarball, "application/gzip")},
        )
        self._check(resp, "publish")
        return resp.json()

    def yank(self, name: str, version: str) -> dict:
        resp = self._http.post(f"{API}/packages/{name}/{version}/yank")
        self._check(resp, f"yank {name}@{version}")
        return resp.json()
