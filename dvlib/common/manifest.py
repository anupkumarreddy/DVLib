"""dvlib.toml manifest schema: loading and validation.

Validated both at publish time (server) and install time (client).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .semver import Range, Version, VersionError, parse_range, parse_version

__all__ = ["Manifest", "ManifestError", "NAME_RE", "load_manifest", "MANIFEST_FILENAME"]

MANIFEST_FILENAME = "dvlib.toml"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ManifestError(ValueError):
    """Raised when a manifest is missing, malformed, or fails validation."""


@dataclass
class Manifest:
    name: str
    version: Version
    description: str = ""
    authors: list[str] = field(default_factory=list)
    license: str = ""
    files: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    dependencies: dict[str, Range] = field(default_factory=dict)
    dev_dependencies: dict[str, Range] = field(default_factory=dict)

    @property
    def version_str(self) -> str:
        return str(self.version)


def _err(msg: str) -> ManifestError:
    return ManifestError(f"invalid manifest: {msg}")


def _str_list(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise _err(f"{what} must be a list of strings")
    result = list(value)
    if len(result) != len(set(result)):
        raise _err(f"{what} must not contain duplicate entries")
    return result


def _dep_table(value: object, what: str) -> dict[str, Range]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _err(f'[{what}] must be a table of name = "range"')
    out: dict[str, Range] = {}
    for name, spec in value.items():
        if not NAME_RE.match(name):
            raise _err(f"[{what}] invalid package name {name!r}")
        if not isinstance(spec, str):
            raise _err(f"[{what}] {name}: range must be a string")
        try:
            out[name] = parse_range(spec)
        except VersionError as e:
            raise _err(f"[{what}] {name}: {e}") from e
    return out


def validate_manifest(data: dict) -> Manifest:
    if not isinstance(data, dict):
        raise _err("expected a TOML table")

    pkg = data.get("package")
    if not isinstance(pkg, dict):
        raise _err("missing [package] table")

    name = pkg.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _err(
            f"package.name {name!r} must match {NAME_RE.pattern} "
            "(lowercase, start with alphanumeric)"
        )

    version_raw = pkg.get("version")
    if not isinstance(version_raw, str):
        raise _err("package.version must be a string")
    try:
        version = parse_version(version_raw)
    except VersionError as e:
        raise _err(f"package.version: {e}") from e
    if version.build:
        raise _err(
            "package.version must not contain build metadata because registry "
            "resolution ignores build metadata"
        )

    description = pkg.get("description", "")
    if not isinstance(description, str):
        raise _err("package.description must be a string")
    authors = _str_list(pkg.get("authors"), "package.authors")
    license_ = pkg.get("license", "")
    if not isinstance(license_, str):
        raise _err("package.license must be a string")

    compile_ = data.get("compile") or {}
    if not isinstance(compile_, dict):
        raise _err("[compile] must be a table")
    files = _str_list(compile_.get("files"), "compile.files")
    include_dirs = _str_list(compile_.get("include_dirs"), "compile.include_dirs")
    defines = _str_list(compile_.get("defines"), "compile.defines")
    if not files:
        raise _err("compile.files must list at least one source file")
    for d in defines:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:=\S+)?", d):
            raise _err(
                f"compile.defines entry {d!r} is not NAME or NAME=value "
                "(whitespace is not supported)"
            )
    for p in files + include_dirs:
        _check_relative(p)

    dependencies = _dep_table(data.get("dependencies"), "dependencies")
    dev_dependencies = _dep_table(data.get("dev-dependencies"), "dev-dependencies")
    overlap = set(dependencies) & set(dev_dependencies)
    if overlap:
        raise _err(
            f"packages listed in both [dependencies] and [dev-dependencies]: "
            f"{', '.join(sorted(overlap))}"
        )
    if name in dependencies or name in dev_dependencies:
        raise _err(f"package {name!r} cannot depend on itself")

    return Manifest(
        name=name,
        version=version,
        description=description,
        authors=authors,
        license=license_,
        files=files,
        include_dirs=include_dirs,
        defines=defines,
        dependencies=dependencies,
        dev_dependencies=dev_dependencies,
    )


def _check_relative(path: str) -> None:
    p = Path(path)
    if (
        not path
        or path == "."
        or "\\" in path
        or any(ch.isspace() for ch in path)
        or p.is_absolute()
        or ".." in p.parts
    ):
        raise _err(
            f"path {path!r} must be a non-empty portable relative path without "
            "whitespace, backslashes, '.' or '..'"
        )


def load_manifest(path: Path | str) -> Manifest:
    """Load and validate a dvlib.toml from a file path or a directory
    containing one."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    if not p.is_file():
        raise ManifestError(f"manifest not found: {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{p}: TOML parse error: {e}") from e
    try:
        return validate_manifest(data)
    except ManifestError as e:
        raise ManifestError(f"{p}: {e}") from e


def loads_manifest(text: str) -> Manifest:
    """Load and validate a manifest from TOML text (e.g. extracted from a
    published tarball)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"TOML parse error: {e}") from e
    return validate_manifest(data)
