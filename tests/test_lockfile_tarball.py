import io
import tarfile

import pytest

from dvlib.common.lockfile import (
    LockEntry,
    Lockfile,
    LockfileError,
    load_lockfile,
    write_lockfile,
)
from dvlib.common.manifest import loads_manifest
from dvlib.common.semver import parse_version
from dvlib.common.tarball import (
    TarballError,
    pack_package,
    safe_extract,
    sha256_bytes,
    sha256_file,
)

MANIFEST = """\
[package]
name = "vip"
version = "1.0.0"

[compile]
files = ["src/vip_pkg.sv"]
include_dirs = ["src"]
defines = ["VIP=1"]
"""


def make_pkg(root, manifest=MANIFEST):
    root.mkdir(parents=True, exist_ok=True)
    (root / "dvlib.toml").write_text(manifest)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "vip_pkg.sv").write_text("package vip; endpackage\n")
    (root / "src" / "vip_defs.svh").write_text("`define VIP_X 1\n")
    return root


class TestLockfile:
    def test_roundtrip(self, tmp_path):
        lock = Lockfile(
            registry="http://reg:8000",
            packages={
                "vip": LockEntry(parse_version("1.0.0"), "ab" * 32, dev=False),
                "tutil": LockEntry(parse_version("0.3.1"), "cd" * 32, dev=True),
            },
        )
        write_lockfile(lock, tmp_path)
        loaded = load_lockfile(tmp_path)
        assert loaded is not None
        assert loaded.registry == "http://reg:8000"
        assert loaded.packages["vip"].version == parse_version("1.0.0")
        assert loaded.packages["vip"].sha256 == "ab" * 32
        assert loaded.packages["vip"].dev is False
        assert loaded.packages["tutil"].dev is True

    def test_missing_returns_none(self, tmp_path):
        assert load_lockfile(tmp_path) is None

    def test_bad_json(self, tmp_path):
        (tmp_path / "dvlib.lock").write_text("{nope")
        with pytest.raises(LockfileError, match="JSON"):
            load_lockfile(tmp_path)

    def test_unsupported_version(self, tmp_path):
        (tmp_path / "dvlib.lock").write_text('{"version": 99, "packages": {}}')
        with pytest.raises(LockfileError, match="version"):
            load_lockfile(tmp_path)

    def test_malformed_entry(self, tmp_path):
        (tmp_path / "dvlib.lock").write_text(
            '{"version": 1, "packages": {"a": {"version": "x", "sha256": "y"}}}'
        )
        with pytest.raises(LockfileError, match="malformed"):
            load_lockfile(tmp_path)

    @pytest.mark.parametrize(
        "entry",
        [
            '{"version": "1.0.0", "sha256": "' + "g" * 64 + '"}',
            '{"version": "1.0.0", "sha256": "' + "ab" * 32 + '", "dev": "false"}',
        ],
    )
    def test_rejects_invalid_entry_types(self, tmp_path, entry):
        (tmp_path / "dvlib.lock").write_text('{"version": 1, "packages": {"a": ' + entry + "}}")
        with pytest.raises(LockfileError, match="malformed"):
            load_lockfile(tmp_path)


class TestTarball:
    def test_pack_and_extract_roundtrip(self, tmp_path):
        src = make_pkg(tmp_path / "pkg")
        manifest = loads_manifest((src / "dvlib.toml").read_text())
        blob = pack_package(src, manifest)
        digest = sha256_bytes(blob)
        assert len(digest) == 64
        assert blob[4:8] == b"\0\0\0\0"  # deterministic gzip mtime

        dest = tmp_path / "out"
        files = safe_extract(blob, dest)
        names = {f.relative_to(dest).as_posix() for f in files}
        assert "dvlib.toml" in names
        assert "src/vip_pkg.sv" in names
        # include_dirs contents pulled in even if not listed in files
        assert "src/vip_defs.svh" in names
        assert (dest / "src" / "vip_pkg.sv").read_text() == "package vip; endpackage\n"

    def test_pack_is_deterministic(self, tmp_path):
        src = make_pkg(tmp_path / "pkg")
        manifest = loads_manifest((src / "dvlib.toml").read_text())
        assert pack_package(src, manifest) == pack_package(src, manifest)

    def test_pack_missing_declared_file(self, tmp_path):
        src = make_pkg(tmp_path / "pkg")
        (src / "src" / "vip_pkg.sv").unlink()
        manifest = loads_manifest((src / "dvlib.toml").read_text())
        with pytest.raises(TarballError, match="missing"):
            pack_package(src, manifest)

    def test_pack_missing_manifest(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        manifest = loads_manifest(MANIFEST)
        with pytest.raises(TarballError, match="dvlib.toml"):
            pack_package(empty, manifest)

    def test_sha256_file(self, tmp_path):
        p = tmp_path / "x.bin"
        p.write_bytes(b"hello")
        assert sha256_file(p) == sha256_bytes(b"hello")

    def _evil_tar(self, name, **kwargs):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name=name)
            for k, v in kwargs.items():
                setattr(info, k, v)
            if info.isreg():
                data = b"x"
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
        return buf.getvalue()

    def test_reject_dotdot(self, tmp_path):
        with pytest.raises(TarballError, match=r"\.\."):
            safe_extract(self._evil_tar("../evil.sv"), tmp_path / "out")

    def test_reject_absolute(self, tmp_path):
        with pytest.raises(TarballError, match="absolute"):
            safe_extract(self._evil_tar("/etc/evil.sv"), tmp_path / "out")

    def test_reject_escaping_symlink(self, tmp_path):
        blob = self._evil_tar("link.sv", type=tarfile.SYMTYPE, linkname="../../outside")
        with pytest.raises(TarballError, match="link"):
            safe_extract(blob, tmp_path / "out")

    def test_reject_device(self, tmp_path):
        blob = self._evil_tar("dev0", type=tarfile.CHRTYPE)
        with pytest.raises(TarballError, match="device"):
            safe_extract(blob, tmp_path / "out")

    def test_rejects_inside_symlink(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            f = tarfile.TarInfo("src/real.sv")
            data = b"pkg"
            f.size = len(data)
            tar.addfile(f, io.BytesIO(data))
            link = tarfile.TarInfo("src/alias.sv")
            link.type = tarfile.SYMTYPE
            link.linkname = "real.sv"
            tar.addfile(link)
        with pytest.raises(TarballError, match="links are not supported"):
            safe_extract(buf.getvalue(), tmp_path / "out")

    def test_rejects_duplicate_paths(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for data in (b"first", b"second"):
                info = tarfile.TarInfo("same.sv")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with pytest.raises(TarballError, match="duplicate"):
            safe_extract(buf.getvalue(), tmp_path / "out")

    def test_pack_rejects_symlink(self, tmp_path):
        src = make_pkg(tmp_path / "pkg")
        (src / "src" / "vip_pkg.sv").unlink()
        (src / "src" / "vip_pkg.sv").symlink_to(tmp_path / "outside.sv")
        (tmp_path / "outside.sv").write_text("secret")
        manifest = loads_manifest((src / "dvlib.toml").read_text())
        with pytest.raises(TarballError, match="symbolic links"):
            pack_package(src, manifest)
