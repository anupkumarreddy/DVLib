import pytest

from dvlib.client.resolver import ResolutionError, VersionMetadata, resolve
from dvlib.common.semver import parse_version


class DictIndex:
    def __init__(self, packages):
        # packages: {name: [(version, deps, sha256, yanked), ...]}
        self.packages = packages

    def get_versions(self, name):
        if name not in self.packages:
            return None
        return [
            VersionMetadata(name, parse_version(v), deps, sha, yanked)
            for v, deps, sha, yanked in self.packages[name]
        ]


def idx(**packages):
    return DictIndex(packages)


class TestBasicResolution:
    def test_single_package_newest_wins(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.2.0", {}, "b", False)])
        result = resolve({"foo": "^1.0.0"}, index)
        assert str(result["foo"].version) == "1.2.0"

    def test_exact_version(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.2.0", {}, "b", False)])
        result = resolve({"foo": "1.0.0"}, index)
        assert str(result["foo"].version) == "1.0.0"

    def test_transitive_dependency(self):
        index = idx(
            a=[("1.0.0", {"b": "^1.0.0"}, "a", False)],
            b=[("1.1.0", {}, "b", False)],
        )
        result = resolve({"a": "^1.0.0"}, index)
        assert set(result) == {"a", "b"}
        assert str(result["b"].version) == "1.1.0"

    def test_diamond_dependency(self):
        index = idx(
            top=[("1.0.0", {"left": "^1.0", "right": "^1.0"}, "t", False)],
            left=[("1.0.0", {"shared": "^1.0"}, "l", False)],
            right=[("1.0.0", {"shared": "^1.2"}, "r", False)],
            shared=[
                ("1.0.0", {}, "s1", False),
                ("1.5.0", {}, "s2", False),
            ],
        )
        result = resolve({"top": "^1.0"}, index)
        assert str(result["shared"].version) == "1.5.0"

    def test_backtracking_picks_compatible_version(self):
        index = idx(
            a=[("1.0.0", {"b": ">=1.0,<3.0"}, "a", False)],
            b=[
                ("3.0.0", {"c": "^2.0"}, "b3", False),
                ("2.0.0", {"c": "^1.0"}, "b2", False),
            ],
            c=[("1.0.0", {}, "c1", False)],  # no c 2.x -> must backtrack to b 2.0.0
        )
        result = resolve({"a": "^1.0.0"}, index)
        assert str(result["b"].version) == "2.0.0"
        assert str(result["c"].version) == "1.0.0"


class TestConflicts:
    def test_diamond_conflict_message(self):
        index = idx(
            a=[("1.0.0", {"foo": "^1.0"}, "a", False)],
            b=[("1.0.0", {"foo": "^2.0"}, "b", False)],
            foo=[("1.0.0", {}, "f1", False), ("2.0.0", {}, "f2", False)],
        )
        with pytest.raises(ResolutionError) as ei:
            resolve({"a": "^1.0", "b": "^1.0"}, index)
        msg = str(ei.value)
        assert "foo" in msg
        assert "^1.0" in msg and "^2.0" in msg

    def test_unknown_package(self):
        with pytest.raises(ResolutionError):
            resolve({"ghost": "^1.0"}, idx())

    def test_no_matching_version(self):
        index = idx(foo=[("1.0.0", {}, "a", False)])
        with pytest.raises(ResolutionError):
            resolve({"foo": "^2.0"}, index)


class TestYanked:
    def test_yanked_excluded(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.1.0", {}, "b", True)])
        result = resolve({"foo": "^1.0"}, index)
        assert str(result["foo"].version) == "1.0.0"

    def test_yanked_allowed_when_locked(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.1.0", {}, "b", True)])
        result = resolve({"foo": "^1.0"}, index, locked={"foo": parse_version("1.1.0")})
        assert str(result["foo"].version) == "1.1.0"

    def test_only_yanked_versions_fail(self):
        index = idx(foo=[("1.0.0", {}, "a", True)])
        with pytest.raises(ResolutionError):
            resolve({"foo": "^1.0"}, index)


class TestLockedPins:
    def test_locked_version_preferred_over_newest(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.5.0", {}, "b", False)])
        result = resolve({"foo": "^1.0"}, index, locked={"foo": parse_version("1.0.0")})
        assert str(result["foo"].version) == "1.0.0"

    def test_locked_version_backtracks_when_unsatisfiable(self):
        index = idx(
            foo=[("1.0.0", {}, "a", False), ("1.5.0", {}, "b", False)],
            bar=[("1.0.0", {"foo": ">=1.4"}, "c", False)],
        )
        result = resolve(
            {"foo": "^1.0", "bar": "^1.0"}, index, locked={"foo": parse_version("1.0.0")}
        )
        assert str(result["foo"].version) == "1.5.0"


class TestPrerelease:
    def test_prerelease_excluded_from_plain_range(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.1.0-rc.1", {}, "b", False)])
        result = resolve({"foo": "^1.0"}, index)
        assert str(result["foo"].version) == "1.0.0"

    def test_prerelease_matched_when_range_names_it(self):
        index = idx(foo=[("1.0.0", {}, "a", False), ("1.1.0-rc.1", {}, "b", False)])
        result = resolve({"foo": ">=1.1.0-rc.1,<2.0"}, index)
        assert str(result["foo"].version) == "1.1.0-rc.1"
