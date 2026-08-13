"""Tarball packing, safe extraction, and sha256 helpers.

A package tarball contains: dvlib.toml, every file in compile.files, and the
full contents of every directory in compile.include_dirs (so `include files
resolve). Paths inside the archive are relative to the package root.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path, PurePosixPath

from .manifest import MANIFEST_FILENAME, Manifest

__all__ = [
    "MAX_ARCHIVE_BYTES",
    "TarballError",
    "pack_package",
    "safe_extract",
    "sha256_bytes",
    "sha256_file",
]

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024


class TarballError(ValueError):
    """Raised when packing fails or an archive is unsafe to extract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_members(root: Path, manifest: Manifest) -> list[Path]:
    """Relative paths to include in the tarball, deduplicated and sorted."""
    rels: set[str] = {MANIFEST_FILENAME}
    for rel in manifest.files:
        p = root / rel
        if p.is_symlink():
            raise TarballError(f"symbolic links are not supported in packages: {rel}")
        if not p.is_file():
            raise TarballError(f"declared file missing from package: {rel}")
        rels.add(PurePosixPath(rel).as_posix())
    for inc in manifest.include_dirs:
        d = root / inc
        if d.is_symlink():
            raise TarballError(f"symbolic links are not supported in packages: {inc}")
        if not d.exists():
            raise TarballError(f"declared include_dir missing from package: {inc}")
        if d.is_file():
            rels.add(PurePosixPath(inc).as_posix())
            continue
        for child in sorted(d.rglob("*")):
            if child.is_symlink():
                raise TarballError(
                    "symbolic links are not supported in packages: "
                    f"{child.relative_to(root).as_posix()}"
                )
            if child.is_file():
                rels.add(child.relative_to(root).as_posix())
    return sorted(rels)


def pack_package(root: Path | str, manifest: Manifest) -> bytes:
    """Pack a package directory into a deterministic tar.gz payload.

    Deterministic: entries sorted, mtime/uid/gid zeroed, so identical trees
    yield identical sha256."""
    root = Path(root)
    if not (root / MANIFEST_FILENAME).is_file():
        raise TarballError(f"{MANIFEST_FILENAME} not found in {root}")
    members = _collect_members(root, manifest)
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in members:
            src = root / rel
            info = tarfile.TarInfo(name=rel)
            info.size = src.stat().st_size
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with open(src, "rb") as f:
                tar.addfile(info, f)
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as compressed:
        compressed.write(tar_buf.getvalue())
    result = buf.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise TarballError(f"package archive is {len(result)} bytes; limit is {MAX_ARCHIVE_BYTES}")
    return result


def _check_member_safe(member: tarfile.TarInfo, dest: Path) -> PurePosixPath:
    name = member.name
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/"):
        raise TarballError(f"unsafe absolute path in tarball: {name!r}")
    if ".." in p.parts:
        raise TarballError(f"unsafe '..' path in tarball: {name!r}")
    if member.issym() or member.islnk():
        raise TarballError(f"links are not supported in package tarballs: {name!r}")
    if member.isdev():
        raise TarballError(f"device entry in tarball: {name!r}")
    return p


def safe_extract(data: bytes | Path | str, dest: Path | str) -> list[Path]:
    """Extract a tar.gz (bytes or path) into dest, rejecting absolute paths,
    '..' traversal, escaping links, and device entries. Returns extracted
    file paths."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (str, Path)):
        raw = Path(data).read_bytes()
    else:
        raw = data
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise TarballError(f"tarball is {len(raw)} bytes; limit is {MAX_ARCHIVE_BYTES}")
    extracted: list[Path] = []
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as e:
        raise TarballError(f"not a valid tar archive: {e}") from e
    with tar:
        members = tar.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise TarballError(
                f"tarball has too many entries ({len(members)}; limit {MAX_ARCHIVE_MEMBERS})"
            )
        total_size = sum(m.size for m in members if m.isfile())
        if total_size > MAX_EXTRACTED_BYTES:
            raise TarballError(
                f"tarball expands to {total_size} bytes; limit is {MAX_EXTRACTED_BYTES}"
            )
        names: set[str] = set()
        for m in members:
            _check_member_safe(m, dest)
            if m.name in names:
                raise TarballError(f"duplicate path in tarball: {m.name!r}")
            names.add(m.name)
        for m in members:
            if m.isdir():
                (dest / m.name).mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                raise TarballError(f"cannot read member {m.name!r}")
            target.write_bytes(src.read())
            target.chmod(m.mode & 0o777 or 0o644)
            extracted.append(target)
    return extracted
