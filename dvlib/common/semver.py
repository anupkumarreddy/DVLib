"""Semantic version parsing, comparison, and range matching.

Implements node-semver semantics for the subset DVLib supports:
  - exact versions:        "1.2.3", "2.0.0-beta.1"
  - caret ranges:          "^1.2.3", "^0.2.3", "^1"
  - tilde ranges:          "~1.2.3", "~1.2", "~1"
  - comparator sets:       ">=1.2.0,<2.0.0"  (comma-separated AND)

Pre-release rule (node-semver): a version carrying a pre-release tag only
matches a range if some comparator in that range names the same
major.minor.patch AND itself carries a pre-release tag.
"""

from __future__ import annotations

import re
from functools import total_ordering

__all__ = ["Version", "Range", "parse_version", "parse_range", "VersionError"]

_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_COMPARATOR_RE = re.compile(r"^(<=|>=|<|>|=)?\s*(?P<version>\S+)$")


class VersionError(ValueError):
    """Raised when a version or range string cannot be parsed."""


@total_ordering
class Version:
    """An immutable semantic version. Build metadata is parsed but ignored
    for precedence, per the semver spec."""

    __slots__ = ("major", "minor", "patch", "prerelease", "build")

    def __init__(
        self,
        major: int,
        minor: int,
        patch: int,
        prerelease: tuple[str | int, ...] = (),
        build: str = "",
    ) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
        self.build = build

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            s += "+" + self.build
        return s

    def __repr__(self) -> str:
        return f"Version({str(self)!r})"

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.core == other.core and self.prerelease == other.prerelease

    def __hash__(self) -> int:
        return hash((self.core, self.prerelease))

    def __lt__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(a: tuple, b: tuple) -> int:
    # A version without a pre-release has higher precedence.
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        x_num = isinstance(x, int)
        y_num = isinstance(y, int)
        if x_num and y_num:
            return -1 if x < y else 1
        # Numeric identifiers have lower precedence than alphanumeric.
        if x_num:
            return -1
        if y_num:
            return 1
        return -1 if str(x) < str(y) else 1
    # All shared identifiers equal: fewer identifiers = lower precedence.
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


def _parse_identifier(text: str) -> str | int:
    if text.isdigit():
        return int(text)
    return text


def parse_version(text: str, *, allow_partial: bool = False) -> Version:
    """Parse a version string. With allow_partial, "1" and "1.2" are accepted
    and missing components default to 0 (used inside range comparators)."""
    text = text.strip()
    m = _VERSION_RE.match(text)
    if not m:
        raise VersionError(f"invalid version: {text!r}")
    minor = m.group("minor")
    patch = m.group("patch")
    if not allow_partial and (minor is None or patch is None):
        raise VersionError(f"version must be major.minor.patch: {text!r}")
    prerelease = m.group("prerelease")
    pre: tuple[str | int, ...] = ()
    if prerelease:
        identifiers = prerelease.split(".")
        if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in identifiers):
            raise VersionError(
                f"numeric prerelease identifiers must not have leading zeros: {text!r}"
            )
        pre = tuple(_parse_identifier(p) for p in identifiers)
    return Version(
        int(m.group("major")),
        int(minor) if minor is not None else 0,
        int(patch) if patch is not None else 0,
        pre,
        m.group("build") or "",
    )


def _bump(v: Version, level: str) -> Version:
    if level == "major":
        return Version(v.major + 1, 0, 0)
    if level == "minor":
        return Version(v.major, v.minor + 1, 0)
    return Version(v.major, v.minor, v.patch + 1)


class _Comparator:
    __slots__ = ("op", "version")

    def __init__(self, op: str, version: Version) -> None:
        self.op = op
        self.version = version

    def matches(self, v: Version) -> bool:
        c = _compare(v, self.version)
        return {
            "<": c < 0,
            "<=": c <= 0,
            ">": c > 0,
            ">=": c >= 0,
            "=": c == 0,
        }[self.op]

    def __repr__(self) -> str:
        return f"_Comparator({self.op}{self.version})"


def _compare(a: Version, b: Version) -> int:
    if a.core != b.core:
        return -1 if a.core < b.core else 1
    return _compare_prerelease(a.prerelease, b.prerelease)


def _partial_precision(text: str) -> int:
    """How many numeric components the version string names (1..3)."""
    core = re.split(r"[-+]", text, maxsplit=1)[0]
    return len(core.split("."))


class Range:
    """A conjunction of comparators parsed from one range expression."""

    def __init__(self, comparators: list[_Comparator], source: str) -> None:
        self.comparators = comparators
        self.source = source

    def __str__(self) -> str:
        return self.source

    def __repr__(self) -> str:
        return f"Range({self.source!r})"

    def matches(self, v: Version) -> bool:
        if not all(c.matches(v) for c in self.comparators):
            return False
        if v.prerelease:
            # node-semver: pre-releases only match if some comparator names
            # the same major.minor.patch with its own pre-release.
            return any(c.version.prerelease and c.version.core == v.core for c in self.comparators)
        return True


def _expand_caret(v: Version, precision: int) -> list[_Comparator]:
    # Missing components act as wildcards: ^1.2 := >=1.2.0 <2.0.0,
    # ^0.2 := >=0.2.0 <0.3.0, ^0.0 := >=0.0.0 <0.1.0.
    if precision == 1:
        upper = Version(v.major + 1, 0, 0)
    elif precision == 2:
        upper = Version(v.major + 1, 0, 0) if v.major > 0 else Version(0, v.minor + 1, 0)
    elif v.major > 0:
        upper = Version(v.major + 1, 0, 0)
    elif v.minor > 0:
        upper = Version(0, v.minor + 1, 0)
    else:
        upper = Version(0, 0, v.patch + 1)
    return [_Comparator(">=", v), _Comparator("<", upper)]


def _expand_tilde(v: Version, precision: int) -> list[_Comparator]:
    if precision == 1:
        upper = _bump(v, "major")
    else:
        upper = _bump(v, "minor")
    return [_Comparator(">=", v), _Comparator("<", upper)]


def _expand_partial_exact(v: Version, precision: int) -> list[_Comparator]:
    """Bare partial versions act as wildcards: "1.2" == >=1.2.0 <1.3.0."""
    if precision == 3:
        return [_Comparator("=", v)]
    if precision == 2:
        return [_Comparator(">=", v), _Comparator("<", _bump(v, "minor"))]
    return [_Comparator(">=", v), _Comparator("<", _bump(v, "major"))]


def _expand_partial_comparator(op: str, v: Version, precision: int) -> list[_Comparator]:
    """Desugar comparators carrying partial versions per node-semver."""
    if precision == 3:
        return [_Comparator(op, v)]
    bump = "minor" if precision == 2 else "major"
    if op in ("<", ">="):
        # <1.2  -> <1.2.0 ; >=1.2 -> >=1.2.0
        return [_Comparator(op, v)]
    if op == "<=":
        # <=1.2 -> <1.3.0
        return [_Comparator("<", _bump(v, bump))]
    if op == ">":
        # >1.2 -> >=1.3.0
        return [_Comparator(">=", _bump(v, bump))]
    return _expand_partial_exact(v, precision)


def parse_range(text: str) -> Range:
    """Parse a range expression: caret, tilde, exact, or a comma-separated
    comparator set such as ">=1.2.0,<2.0.0"."""
    source = text.strip()
    if not source:
        raise VersionError("empty version range")
    comparators: list[_Comparator] = []
    for raw in source.split(","):
        token = raw.strip()
        if not token:
            raise VersionError(f"empty comparator in range: {source!r}")
        if token.startswith("^"):
            body = token[1:]
            v = parse_version(body, allow_partial=True)
            comparators.extend(_expand_caret(v, _partial_precision(body)))
            continue
        if token.startswith("~"):
            body = token[1:]
            v = parse_version(body, allow_partial=True)
            comparators.extend(_expand_tilde(v, _partial_precision(body)))
            continue
        m = _COMPARATOR_RE.match(token)
        if not m:
            raise VersionError(f"invalid comparator: {token!r}")
        op = m.group(1) or "="
        body = m.group("version")
        v = parse_version(body, allow_partial=True)
        comparators.extend(_expand_partial_comparator(op, v, _partial_precision(body)))
    return Range(comparators, source)
