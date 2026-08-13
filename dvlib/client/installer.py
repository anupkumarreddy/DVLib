"""Install flow: resolve, download to cache, verify sha256, extract, lock."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..common.lockfile import LockEntry, Lockfile, load_lockfile, write_lockfile
from ..common.manifest import ManifestError, load_manifest
from ..common.tarball import TarballError, safe_extract, sha256_bytes
from .api import RegistryClient
from .filelist import PACKAGES_DIR, FilelistError, generate_filelist
from .resolver import ResolutionError, VersionMetadata, resolve

__all__ = ["InstallError", "InstallResult", "install"]


class InstallError(Exception):
    pass


@dataclass
class InstalledItem:
    name: str
    version: str
    dev: bool
    from_cache: bool
    yanked: bool


@dataclass
class InstallResult:
    items: list[InstalledItem]
    lock_path: Path
    filelist_path: Path


def _cache_tarball(cache: Path, name: str, version: str) -> Path:
    return cache / "tarballs" / name / f"{version}.tar.gz"


def _fetch_verified(
    client: RegistryClient,
    cache: Path,
    meta: VersionMetadata,
) -> tuple[bytes, bool]:
    """Return (tarball bytes, from_cache). sha256 is re-verified on every use;
    a corrupt cache entry is re-downloaded once."""
    version = str(meta.version)
    cached = _cache_tarball(cache, name=meta.name, version=version)
    if cached.is_file():
        data = cached.read_bytes()
        if sha256_bytes(data) == meta.sha256:
            return data, True
    data = client.download_tarball(meta.name, meta.version)
    if sha256_bytes(data) != meta.sha256:
        raise InstallError(
            f"sha256 mismatch for {meta.name}@{version}: registry tarball does not "
            "match published digest"
        )
    cached.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cached.parent, prefix=".download-", delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, cached)
        finally:
            temporary.unlink(missing_ok=True)
    return data, False


def install(
    project_root: Path | str,
    client: RegistryClient,
    cache: Path | str,
    *,
    include_dev: bool = True,
    refresh: set[str] | None = None,
) -> InstallResult:
    """Resolve the project's dependencies and install them.

    refresh: package names to re-resolve ignoring the existing lockfile
    (used by `dvlib update pkg`). None = use lock pins only for yanked
    fallback; empty set via update-all is handled by the caller passing
    refresh=set(all locked names).
    """
    project_root = Path(project_root)
    cache = Path(cache)
    manifest = load_manifest(project_root)

    requirements: dict[str, object] = {n: r for n, r in manifest.dependencies.items()}
    dev_names: set[str] = set()
    if include_dev:
        dev_names = set(manifest.dev_dependencies)
        requirements.update(manifest.dev_dependencies)

    lock = None
    try:
        lock = load_lockfile(project_root)
    except Exception as e:
        raise InstallError(str(e)) from e

    refresh = refresh or set()
    locked = {
        name: entry.version
        for name, entry in (lock.packages.items() if lock else [])
        if name not in refresh
    }

    try:
        resolved = resolve(requirements, client, locked=locked)
    except ResolutionError as e:
        raise InstallError(str(e)) from e

    packages_dir = project_root / PACKAGES_DIR
    packages_dir.mkdir(parents=True, exist_ok=True)
    items: list[InstalledItem] = []
    new_lock = Lockfile(registry=client.base_url)

    # Download, extract, and validate every package before replacing any
    # currently installed package. A bad registry response must not destroy a
    # working installation.
    with tempfile.TemporaryDirectory(prefix=".dvlib-stage-", dir=project_root) as stage:
        stage_root = Path(stage)
        for name in sorted(resolved):
            meta = resolved[name]
            version = str(meta.version)
            data, from_cache = _fetch_verified(client, cache, meta)
            staged_dest = stage_root / name
            try:
                safe_extract(data, staged_dest)
                extracted = load_manifest(staged_dest)
            except (TarballError, ManifestError, OSError) as e:
                raise InstallError(f"cannot install {name}@{version}: {e}") from e
            if extracted.name != name or extracted.version != meta.version:
                raise InstallError(
                    f"extracted manifest mismatch for {name}@{version} "
                    f"(got {extracted.name}@{extracted.version})"
                )
            new_lock.packages[name] = LockEntry(meta.version, meta.sha256, dev=name in dev_names)
            items.append(InstalledItem(name, version, name in dev_names, from_cache, meta.yanked))

        for name in sorted(resolved):
            dest = packages_dir / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(stage_root / name), dest)

    # Prune packages that dropped out of the resolution.
    resolved_names = set(resolved)
    if packages_dir.is_dir():
        for child in packages_dir.iterdir():
            if child.is_dir():
                if child.name not in resolved_names:
                    shutil.rmtree(child)

    lock_path = write_lockfile(new_lock, project_root)
    try:
        filelist_path = generate_filelist(project_root)
    except (FilelistError, ManifestError, OSError) as e:
        raise InstallError(f"cannot generate {project_root / 'dvlib.f'}: {e}") from e
    return InstallResult(items, lock_path, filelist_path)
