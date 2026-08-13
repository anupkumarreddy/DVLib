"""Tests for dvlib.common.semver — modeled on node-semver semantics."""

import pytest

from dvlib.common.semver import Range, VersionError, parse_range, parse_version


class TestParse:
    def test_basic(self):
        v = parse_version("1.2.3")
        assert (v.major, v.minor, v.patch) == (1, 2, 3)
        assert v.prerelease == ()
        assert str(v) == "1.2.3"

    def test_prerelease(self):
        v = parse_version("2.0.0-beta.1")
        assert v.prerelease == ("beta", 1)
        assert str(v) == "2.0.0-beta.1"

    def test_build_metadata(self):
        v = parse_version("1.0.0+build.42")
        assert v.build == "build.42"
        assert str(v) == "1.0.0+build.42"

    def test_prerelease_and_build(self):
        v = parse_version("1.0.0-rc.1+exp.sha.5114f85")
        assert v.prerelease == ("rc", 1)
        assert v.build == "exp.sha.5114f85"

    def test_leading_zeros_rejected(self):
        with pytest.raises(VersionError):
            parse_version("01.2.3")

    @pytest.mark.parametrize("version", ["1.0.0-01", "1.0.0-alpha.007"])
    def test_prerelease_leading_zeros_rejected(self, version):
        with pytest.raises(VersionError, match="leading zeros"):
            parse_version(version)

    @pytest.mark.parametrize("bad", ["1.2", "1", "1.2.3.4", "v1.2.3", "", "1.2.x", "1..2", "a.b.c"])
    def test_invalid(self, bad):
        with pytest.raises(VersionError):
            parse_version(bad)

    def test_partial_allowed_in_ranges(self):
        v = parse_version("1.2", allow_partial=True)
        assert v.core == (1, 2, 0)


class TestCompare:
    def test_core_ordering(self):
        assert parse_version("1.0.0") < parse_version("1.0.1")
        assert parse_version("1.0.1") < parse_version("1.1.0")
        assert parse_version("1.9.9") < parse_version("2.0.0")
        assert parse_version("0.9.9") < parse_version("0.10.0")

    def test_prerelease_lower_than_release(self):
        assert parse_version("1.0.0-alpha") < parse_version("1.0.0")

    def test_prerelease_ordering(self):
        # node-semver's canonical example chain
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [parse_version(s) for s in ordered]
        for a, b in zip(versions, versions[1:], strict=False):
            assert a < b, f"{a} !< {b}"
            assert b > a

    def test_numeric_lt_alphanumeric(self):
        assert parse_version("1.0.0-1") < parse_version("1.0.0-alpha")

    def test_build_metadata_ignored_in_precedence(self):
        assert parse_version("1.0.0+a") == parse_version("1.0.0+b")
        assert not (parse_version("1.0.0+a") < parse_version("1.0.0+b"))

    def test_equality_and_hash(self):
        assert parse_version("1.2.3") == parse_version("1.2.3")
        assert hash(parse_version("1.2.3")) == hash(parse_version("1.2.3"))
        assert parse_version("1.2.3") != parse_version("1.2.3-rc")

    def test_sort(self):
        shuffled = ["2.0.0", "0.1.0", "1.0.0-rc.1", "1.0.0", "0.10.0"]
        result = sorted(parse_version(s) for s in shuffled)
        assert [str(v) for v in result] == [
            "0.1.0",
            "0.10.0",
            "1.0.0-rc.1",
            "1.0.0",
            "2.0.0",
        ]


class TestExactRanges:
    def test_exact_match(self):
        r = parse_range("1.2.3")
        assert r.matches(parse_version("1.2.3"))
        assert not r.matches(parse_version("1.2.4"))
        assert not r.matches(parse_version("1.2.2"))

    def test_exact_prerelease(self):
        r = parse_range("2.0.0-beta.1")
        assert r.matches(parse_version("2.0.0-beta.1"))
        assert not r.matches(parse_version("2.0.0-beta.2"))
        assert not r.matches(parse_version("2.0.0"))

    def test_partial_exact_is_wildcard(self):
        r = parse_range("1.2")
        assert r.matches(parse_version("1.2.0"))
        assert r.matches(parse_version("1.2.9"))
        assert not r.matches(parse_version("1.3.0"))
        r = parse_range("1")
        assert r.matches(parse_version("1.99.99"))
        assert not r.matches(parse_version("2.0.0"))


class TestCaretRanges:
    def test_standard(self):
        r = parse_range("^1.2.3")
        assert r.matches(parse_version("1.2.3"))
        assert r.matches(parse_version("1.9.9"))
        assert not r.matches(parse_version("2.0.0"))
        assert not r.matches(parse_version("1.2.2"))

    def test_zero_major(self):
        r = parse_range("^0.2.3")
        assert r.matches(parse_version("0.2.3"))
        assert r.matches(parse_version("0.2.9"))
        assert not r.matches(parse_version("0.3.0"))
        assert not r.matches(parse_version("1.0.0"))

    def test_zero_zero(self):
        r = parse_range("^0.0.3")
        assert r.matches(parse_version("0.0.3"))
        assert not r.matches(parse_version("0.0.4"))

    def test_partial(self):
        r = parse_range("^1.2")
        assert r.matches(parse_version("1.2.0"))
        assert r.matches(parse_version("1.9.0"))
        assert not r.matches(parse_version("2.0.0"))
        r = parse_range("^1")
        assert r.matches(parse_version("1.0.0"))
        assert r.matches(parse_version("1.99.99"))
        assert not r.matches(parse_version("2.0.0"))

    def test_partial_zero_major(self):
        r = parse_range("^0.2")
        assert r.matches(parse_version("0.2.0"))
        assert not r.matches(parse_version("0.3.0"))
        r = parse_range("^0.0")
        assert r.matches(parse_version("0.0.5"))
        assert not r.matches(parse_version("0.1.0"))

    def test_caret_with_prerelease(self):
        r = parse_range("^1.2.3-beta.2")
        assert r.matches(parse_version("1.2.3-beta.2"))
        assert r.matches(parse_version("1.2.3-beta.4"))
        assert r.matches(parse_version("1.2.3"))
        assert r.matches(parse_version("1.9.0"))
        assert not r.matches(parse_version("1.2.3-alpha"))
        assert not r.matches(parse_version("1.2.4-alpha.1"))


class TestTildeRanges:
    def test_full(self):
        r = parse_range("~1.2.3")
        assert r.matches(parse_version("1.2.3"))
        assert r.matches(parse_version("1.2.9"))
        assert not r.matches(parse_version("1.3.0"))
        assert not r.matches(parse_version("1.2.2"))

    def test_partial_minor(self):
        r = parse_range("~1.2")
        assert r.matches(parse_version("1.2.0"))
        assert r.matches(parse_version("1.2.7"))
        assert not r.matches(parse_version("1.3.0"))

    def test_partial_major(self):
        r = parse_range("~1")
        assert r.matches(parse_version("1.0.0"))
        assert r.matches(parse_version("1.9.9"))
        assert not r.matches(parse_version("2.0.0"))


class TestComparatorSets:
    def test_basic_set(self):
        r = parse_range(">=1.2.0,<2.0.0")
        assert r.matches(parse_version("1.2.0"))
        assert r.matches(parse_version("1.99.0"))
        assert not r.matches(parse_version("2.0.0"))
        assert not r.matches(parse_version("1.1.9"))

    def test_single_comparators(self):
        assert parse_range(">1.0.0").matches(parse_version("1.0.1"))
        assert not parse_range(">1.0.0").matches(parse_version("1.0.0"))
        assert parse_range("<=1.0.0").matches(parse_version("1.0.0"))
        assert parse_range("=1.0.0").matches(parse_version("1.0.0"))

    def test_partial_comparator_desugaring(self):
        # <=1.2 means <1.3.0
        r = parse_range("<=1.2")
        assert r.matches(parse_version("1.2.9"))
        assert not r.matches(parse_version("1.3.0"))
        # >1.2 means >=1.3.0
        r = parse_range(">1.2")
        assert r.matches(parse_version("1.3.0"))
        assert not r.matches(parse_version("1.2.9"))
        # >=1.2 means >=1.2.0 ; <1.2 means <1.2.0
        assert parse_range(">=1.2").matches(parse_version("1.2.0"))
        assert not parse_range("<1.2").matches(parse_version("1.2.0"))
        assert parse_range("<1.2").matches(parse_version("1.1.99"))

    def test_whitespace_tolerated(self):
        r = parse_range(" >=1.2.0 , <2.0.0 ")
        assert r.matches(parse_version("1.5.0"))

    def test_invalid(self):
        with pytest.raises(VersionError):
            parse_range("")
        with pytest.raises(VersionError):
            parse_range(">=1.2.0,")
        with pytest.raises(VersionError):
            parse_range("=>1.2.3")


class TestPrereleaseRangeRules:
    def test_prerelease_excluded_from_plain_range(self):
        r = parse_range("^1.0.0")
        assert not r.matches(parse_version("1.5.0-rc.1"))
        assert not r.matches(parse_version("2.0.0-alpha"))

    def test_prerelease_allowed_when_range_names_it(self):
        r = parse_range(">=1.0.0-alpha,<2.0.0")
        assert r.matches(parse_version("1.0.0-beta"))
        assert r.matches(parse_version("1.5.0"))
        # ...but only for the same major.minor.patch
        assert not r.matches(parse_version("1.5.0-rc.1"))

    def test_prerelease_exact(self):
        r = parse_range("1.0.0-rc.1")
        assert r.matches(parse_version("1.0.0-rc.1"))
        assert not r.matches(parse_version("1.0.0-rc.2"))


class TestRangeRepr:
    def test_str_roundtrip(self):
        r = parse_range("^1.2.3")
        assert str(r) == "^1.2.3"
        assert isinstance(r, Range)
