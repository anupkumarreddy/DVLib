"""End-to-end: real uvicorn server + CLI client, diamond dependency install."""

import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from dvlib.common.lockfile import load_lockfile
from dvlib.common.tarball import sha256_bytes
from dvlib.server.app import hash_token
from dvlib.server.db import Database

TOKEN = "integration-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    """Run a real registry server in a background thread."""
    import uvicorn

    from dvlib.server.app import create_app

    data_dir = tmp_path_factory.mktemp("registry-data")
    port = _free_port()
    app = create_app(data_dir)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/api/v1/health", timeout=0.5)
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("registry server did not start")
    db = Database(data_dir / "registry.db")
    db.insert_token(hash_token(TOKEN), "integration")
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def env(registry, tmp_path, monkeypatch):
    """Environment pointing the CLI at the test registry with isolated
    cache/config dirs."""
    monkeypatch.setenv("DVLIB_REGISTRY", registry)
    monkeypatch.setenv("DVLIB_TOKEN", TOKEN)
    monkeypatch.setenv("DVLIB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DVLIB_CONFIG_HOME", str(tmp_path / "config"))
    return registry


def run_cli(*args, cwd):
    from typer.testing import CliRunner

    from dvlib.client.cli import app

    runner = CliRunner()
    result = runner.invoke(app, list(args))
    return result


def make_pkg(
    root: Path,
    name: str,
    version: str,
    deps: dict[str, str] | None = None,
    dev_deps: dict[str, str] | None = None,
) -> Path:
    d = root / name
    (d / "src").mkdir(parents=True)
    dep_toml = ""
    if deps:
        dep_toml = "[dependencies]\n" + "".join(f'{k} = "{v}"\n' for k, v in deps.items())
    dev_toml = ""
    if dev_deps:
        dev_toml = "[dev-dependencies]\n" + "".join(f'{k} = "{v}"\n' for k, v in dev_deps.items())
    (d / "dvlib.toml").write_text(
        f"""\
[package]
name = "{name}"
version = "{version}"
description = "integration test package {name}"

[compile]
files = ["src/{name}_pkg.sv"]
include_dirs = ["src"]
defines = ["{name.upper()}_ON=1"]

{dep_toml}
{dev_toml}
"""
    )
    (d / "src" / f"{name}_pkg.sv").write_text(f"package {name}_pkg; endpackage\n")
    (d / "src" / f"{name}_defs.svh").write_text(f"`define {name.upper()}_DEFS\n")
    return d


@pytest.fixture(scope="module")
def published(registry, tmp_path_factory):
    """Publish a diamond: vip_top -> lib_a, lib_b -> uvm_kit."""
    from dvlib.client.api import RegistryClient
    from dvlib.common.manifest import loads_manifest
    from dvlib.common.tarball import pack_package

    src = tmp_path_factory.mktemp("pkgs")
    pkgs = {
        "uvm_kit": ("1.4.0", {}),
        "lib_a": ("1.0.0", {"uvm_kit": "^1.4.0"}),
        "lib_b": ("1.0.0", {"uvm_kit": "^1.4.0"}),
        "vip_top": ("1.0.0", {"lib_a": "^1.0.0", "lib_b": "^1.0.0"}),
    }
    digests = {}
    with RegistryClient(registry, TOKEN) as client:
        for name, (version, deps) in pkgs.items():
            d = make_pkg(src, name, version, deps)
            blob = pack_package(d, loads_manifest((d / "dvlib.toml").read_text()))
            result = client.publish(blob)
            digests[f"{name}@{version}"] = result["sha256"]
    return digests


class TestEndToEnd:
    def test_install_diamond(self, env, published, tmp_path):
        proj = make_pkg(tmp_path, "mytb", "0.1.0", deps={"vip_top": "^1.0.0"})
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output

        # Lockfile.
        lock = load_lockfile(proj)
        assert lock is not None
        assert set(lock.packages) == {"vip_top", "lib_a", "lib_b", "uvm_kit"}
        assert lock.packages["uvm_kit"].sha256 == published["uvm_kit@1.4.0"]
        assert lock.registry == env

        # Extracted tree.
        pkgs_dir = proj / "dvlib_packages"
        for name in ["vip_top", "lib_a", "lib_b", "uvm_kit"]:
            assert (pkgs_dir / name / "dvlib.toml").is_file()
            assert (pkgs_dir / name / "src" / f"{name}_pkg.sv").is_file()
            # include_dirs contents came along
            assert (pkgs_dir / name / "src" / f"{name}_defs.svh").is_file()
            # per-package fragment
            assert (pkgs_dir / name / f"{name}.f").is_file()

        # dvlib.f: deepest dependency first (uvm_kit before lib_a/lib_b before vip_top).
        lines = [
            line for line in (proj / "dvlib.f").read_text().splitlines() if line.endswith("_pkg.sv")
        ]
        order = [line.split("/")[1] for line in lines]
        assert order[0] == "uvm_kit"
        assert order[-1] == "vip_top"
        assert set(order[1:3]) == {"lib_a", "lib_b"}
        # incdirs and defines present
        content = (proj / "dvlib.f").read_text()
        assert "+incdir+dvlib_packages/uvm_kit/src" in content
        assert "+define+UVM_KIT_ON=1" in content

    def test_reinstall_is_deterministic(self, env, published, tmp_path):
        proj = make_pkg(tmp_path, "mytb", "0.1.0", deps={"vip_top": "^1.0.0"})
        r1 = run_cli("install", "--dir", str(proj), cwd=proj)
        lock1 = (proj / "dvlib.lock").read_text()
        shutil.rmtree(proj / "dvlib_packages")
        r2 = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r1.exit_code == r2.exit_code == 0
        assert (proj / "dvlib.lock").read_text() == lock1

    def test_dev_dependencies_installed_and_marked(self, env, published, tmp_path):
        proj = make_pkg(
            tmp_path,
            "mytb",
            "0.1.0",
            deps={"lib_a": "^1.0.0"},
            dev_deps={"lib_b": "^1.0.0"},
        )
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        lock = load_lockfile(proj)
        assert lock.packages["lib_a"].dev is False
        assert lock.packages["lib_b"].dev is True
        assert (proj / "dvlib_packages" / "lib_b").is_dir()

        # --no-dev excludes dev deps
        r = run_cli("clean", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0
        (proj / "dvlib.lock").unlink()
        r = run_cli("install", "--no-dev", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        assert not (proj / "dvlib_packages" / "lib_b").exists()
        assert (proj / "dvlib_packages" / "lib_a").is_dir()

    def test_conflict_error(self, env, published, tmp_path):
        # uvm_kit only has 1.4.0; demand incompatible ranges via two deps.
        from dvlib.client.api import RegistryClient
        from dvlib.common.manifest import loads_manifest
        from dvlib.common.tarball import pack_package

        src = tmp_path / "conflict_pkgs"
        for name, dep in [("needs_old", {"uvm_kit": "<1.0"}), ("needs_new", {"uvm_kit": "^1.0"})]:
            d = make_pkg(src, name, "1.0.0", dep)
            blob = pack_package(d, loads_manifest((d / "dvlib.toml").read_text()))
            with RegistryClient(env, TOKEN) as client:
                client.publish(blob)

        proj = make_pkg(
            tmp_path,
            "mytb",
            "0.1.0",
            deps={"needs_old": "^1.0", "needs_new": "^1.0"},
        )
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code != 0
        out = r.output + (r.stderr or "")
        assert "uvm_kit" in out and "<1.0" in out and "^1.0" in out

    def test_publish_requires_token(self, registry, tmp_path, monkeypatch):
        monkeypatch.setenv("DVLIB_REGISTRY", registry)
        monkeypatch.delenv("DVLIB_TOKEN", raising=False)
        monkeypatch.setenv("DVLIB_CONFIG_HOME", str(tmp_path / "empty-config"))
        d = make_pkg(tmp_path, "vip_x", "1.0.0")
        r = run_cli("publish", "--dir", str(d), cwd=d)
        assert r.exit_code != 0

    def test_yank_hides_from_new_resolution(self, env, published, tmp_path):
        from dvlib.client.api import RegistryClient
        from dvlib.common.manifest import loads_manifest
        from dvlib.common.tarball import pack_package

        # Publish a second uvm_kit version, install (picks it), then yank it.
        src = tmp_path / "yank_pkgs"
        d = make_pkg(src, "uvm_kit", "1.5.0", {})
        blob = pack_package(d, loads_manifest((d / "dvlib.toml").read_text()))
        digest = sha256_bytes(blob)
        with RegistryClient(env, TOKEN) as client:
            client.publish(blob)

        proj = make_pkg(tmp_path, "mytb", "0.1.0", deps={"uvm_kit": "^1.4.0"})
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        lock = load_lockfile(proj)
        assert str(lock.packages["uvm_kit"].version) == "1.5.0"
        assert lock.packages["uvm_kit"].sha256 == digest

        # Yank 1.5.0; a fresh project must fall back to 1.4.0.
        with RegistryClient(env, TOKEN) as client:
            client.yank("uvm_kit", "1.5.0")
        proj2 = make_pkg(tmp_path / "p2", "mytb2", "0.1.0", deps={"uvm_kit": "^1.4.0"})
        r = run_cli("install", "--dir", str(proj2), cwd=proj2)
        assert r.exit_code == 0, r.output
        lock2 = load_lockfile(proj2)
        assert str(lock2.packages["uvm_kit"].version) == "1.4.0"

        # The old project's lockfile still fetches the yanked version (with warning).
        shutil.rmtree(proj / "dvlib_packages")
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        lock3 = load_lockfile(proj)
        assert str(lock3.packages["uvm_kit"].version) == "1.5.0"
        assert "yanked" in (r.output + (r.stderr or "")).lower()

    def test_search_info_tree_outdated(self, env, published, tmp_path):
        proj = make_pkg(tmp_path, "mytb", "0.1.0", deps={"vip_top": "^1.0.0"})
        r = run_cli("install", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output

        r = run_cli("search", "uvm", cwd=proj)
        assert r.exit_code == 0 and "uvm_kit" in r.output

        r = run_cli("info", "uvm_kit", cwd=proj)
        assert r.exit_code == 0 and "1.4.0" in r.output

        r = run_cli("tree", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        assert "vip_top" in r.output and "uvm_kit" in r.output

        # Nothing newer matching ^1.0.0 yet.
        r = run_cli("outdated", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0
        assert "up to date" in r.output

        # Publish vip_top 1.1.0 -> outdated reports it, update pulls it.
        from dvlib.client.api import RegistryClient
        from dvlib.common.manifest import loads_manifest
        from dvlib.common.tarball import pack_package

        src = tmp_path / "upd_pkgs"
        d = make_pkg(src, "vip_top", "1.1.0", {"lib_a": "^1.0.0", "lib_b": "^1.0.0"})
        blob = pack_package(d, loads_manifest((d / "dvlib.toml").read_text()))
        with RegistryClient(env, TOKEN) as client:
            client.publish(blob)

        r = run_cli("outdated", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0
        assert "1.1.0" in r.output

        r = run_cli("update", "vip_top", "--dir", str(proj), cwd=proj)
        assert r.exit_code == 0, r.output
        lock = load_lockfile(proj)
        assert str(lock.packages["vip_top"].version) == "1.1.0"
        # lib_a/lib_b stay pinned (not refreshed).
        assert str(lock.packages["lib_a"].version) == "1.0.0"
