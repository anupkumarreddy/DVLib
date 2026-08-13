"""Dependency resolution via resolvelib.

Exactly one version per package name (SV package names are global symbols —
npm-style nesting is impossible). Yanked versions are excluded from new
resolutions unless pinned by an existing dvlib.lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from resolvelib import AbstractProvider, BaseReporter, ResolutionImpossible, Resolver

from ..common.semver import Range, Version, parse_range

__all__ = [
    "VersionMetadata",
    "ResolutionError",
    "resolve",
]


@dataclass(frozen=True)
class VersionMetadata:
    """Everything the resolver needs to know about one published version."""

    name: str
    version: Version
    dependencies: dict[str, str]  # dep name -> range spec string
    sha256: str
    yanked: bool = False


class PackageIndex(Protocol):
    """Source of package metadata (registry client in production, dict in tests)."""

    def get_versions(self, name: str) -> list[VersionMetadata] | None:
        """All known versions of a package, or None if the package is unknown."""
        ...


class ResolutionError(Exception):
    """Human-readable resolution failure."""


@dataclass(frozen=True)
class _Requirement:
    name: str
    range: Range
    sources: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.name} {self.range}"


@dataclass(frozen=True)
class _Candidate:
    name: str
    meta: VersionMetadata

    @property
    def version(self) -> Version:
        return self.meta.version


class _Provider(AbstractProvider):
    def __init__(self, index: PackageIndex, locked: dict[str, Version] | None = None) -> None:
        self.index = index
        self.locked = locked or {}

    def identify(self, requirement_or_candidate):
        # All requirements/candidates for the same package share one identifier
        # so resolvelib merges them into a single criterion (one version per
        # package name).
        return requirement_or_candidate.name

    def get_preference(self, identifier, resolutions, candidates, information, backtrack_causes):
        # Prefer identifiers with the most requirements (most constrained first).
        return sum(1 for _ in information[identifier])

    def find_matches(self, identifier, requirements, incompatibilities):
        reqs = list(requirements[identifier])
        if not reqs:
            return []
        name = reqs[0].name
        versions = self.index.get_versions(name)
        if versions is None:
            return []
        bad_versions = {ic.version for ic in incompatibilities[identifier]}
        out = []
        for meta in versions:
            if meta.version in bad_versions:
                continue
            locked_v = self.locked.get(name)
            if meta.yanked and not (locked_v is not None and locked_v == meta.version):
                continue
            if all(r.range.matches(meta.version) for r in reqs):
                out.append(_Candidate(name, meta))
        # Newest first so the resolver tries latest versions before backtracking.
        out.sort(key=lambda c: c.version, reverse=True)
        # Reproducibility: a version pinned by dvlib.lock is tried first when it
        # still satisfies all requirements. `update` drops pins to bypass this.
        locked_v = self.locked.get(name)
        if locked_v is not None:
            for i, cand in enumerate(out):
                if cand.version == locked_v:
                    out.insert(0, out.pop(i))
                    break
        return out

    def is_satisfied_by(self, requirement, candidate):
        return requirement.name == candidate.name and requirement.range.matches(candidate.version)

    def get_dependencies(self, candidate):
        return [
            _Requirement(
                dep_name,
                parse_range(spec),
                sources=(f"{candidate.name} {candidate.version}",),
            )
            for dep_name, spec in candidate.meta.dependencies.items()
        ]


def resolve(
    requirements: dict[str, str | Range],
    index: PackageIndex,
    *,
    locked: dict[str, Version] | None = None,
    root_name: str = "<root project>",
) -> dict[str, VersionMetadata]:
    """Resolve root requirements to one version per package.

    Raises ResolutionError with a human-readable message on conflict or
    unknown package."""
    reqs = [
        _Requirement(
            name, parse_range(spec) if isinstance(spec, str) else spec, sources=(root_name,)
        )
        for name, spec in sorted(requirements.items())
    ]
    provider = _Provider(index, locked)
    resolver = Resolver(provider, BaseReporter())
    try:
        result = resolver.resolve(reqs)
    except ResolutionImpossible as e:
        raise ResolutionError(_format_conflict(e)) from e
    return {name: cand.meta for name, cand in result.mapping.items()}


def _format_conflict(exc: ResolutionImpossible) -> str:
    lines = ["could not resolve dependencies:"]
    seen: dict[str, list[str]] = {}
    for cause in exc.causes:
        req = cause.requirement
        parent = cause.parent
        src = ""
        if req.sources:
            src = req.sources[0]
        elif parent is not None:
            src = f"{parent.name} {parent.version}"
        seen.setdefault(req.name, []).append(f"{src} needs {req.name} {req.range}")
    for name in sorted(seen):
        for desc in sorted(set(seen[name])):
            lines.append(f"  {desc}")
    return "\n".join(lines)
