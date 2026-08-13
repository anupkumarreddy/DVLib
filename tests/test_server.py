import hashlib

import pytest
from fastapi.testclient import TestClient

from dvlib.common.manifest import loads_manifest
from dvlib.common.tarball import pack_package
from dvlib.server.app import create_app, hash_token
from dvlib.server.db import Database

MANIFEST_TMPL = """\
[package]
name = "{name}"
version = "{version}"
description = "desc for {name}"

[compile]
files = ["src/{name}_pkg.sv"]
include_dirs = ["src"]

{deps}
"""


def build_pkg(root, name, version, deps=""):
    d = root / f"{name}-{version}"
    (d / "src").mkdir(parents=True)
    (d / "dvlib.toml").write_text(MANIFEST_TMPL.format(name=name, version=version, deps=deps))
    (d / "src" / f"{name}_pkg.sv").write_text(f"package {name}; endpackage\n")
    manifest = loads_manifest((d / "dvlib.toml").read_text())
    return pack_package(d, manifest)


@pytest.fixture
def server(tmp_path):
    data_dir = tmp_path / "data"
    app = create_app(data_dir)
    client = TestClient(app)
    token = "test-token-123"
    db = Database(data_dir / "registry.db")
    db.insert_token(hash_token(token), "tests")
    client.token = token
    client.data_dir = data_dir
    return client


def auth(client):
    return {"Authorization": f"Bearer {client.token}"}


def publish(client, blob):
    return client.post(
        "/api/v1/packages",
        files={"file": ("pkg.tar.gz", blob, "application/gzip")},
        headers=auth(client),
    )


class TestRead:
    def test_health(self, server):
        r = server.get("/api/v1/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_search_empty(self, server):
        assert server.get("/api/v1/packages").json() == []

    def test_404s(self, server):
        assert server.get("/api/v1/packages/ghost").status_code == 404
        assert server.get("/api/v1/packages/ghost/1.0.0").status_code == 404
        assert server.get("/api/v1/packages/ghost/1.0.0/tarball").status_code == 404


class TestPublish:
    def test_publish_and_fetch(self, server, tmp_path):
        blob = build_pkg(tmp_path, "vip", "1.0.0")
        r = publish(server, blob)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["sha256"] == hashlib.sha256(blob).hexdigest()

        detail = server.get("/api/v1/packages/vip").json()
        assert [v["version"] for v in detail["versions"]] == ["1.0.0"]
        assert detail["versions"][0]["sha256"] == body["sha256"]
        assert detail["versions"][0]["yanked"] is False

        single = server.get("/api/v1/packages/vip/1.0.0").json()
        assert single["version"] == "1.0.0"

        tb = server.get("/api/v1/packages/vip/1.0.0/tarball")
        assert tb.status_code == 200 and tb.content == blob

    def test_publish_requires_token(self, server, tmp_path):
        blob = build_pkg(tmp_path, "vip", "1.0.0")
        r = server.post(
            "/api/v1/packages",
            files={"file": ("pkg.tar.gz", blob, "application/gzip")},
        )
        assert r.status_code == 401

    def test_publish_rejects_bad_token(self, server, tmp_path):
        blob = build_pkg(tmp_path, "vip", "1.0.0")
        r = server.post(
            "/api/v1/packages",
            files={"file": ("pkg.tar.gz", blob, "application/gzip")},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 403

    def test_republish_rejected(self, server, tmp_path):
        blob = build_pkg(tmp_path, "vip", "1.0.0")
        assert publish(server, blob).status_code == 201
        r = publish(server, blob)
        assert r.status_code == 409

    def test_invalid_tarball_rejected(self, server):
        r = publish(server, b"not a tarball")
        assert r.status_code in (400, 422)

    def test_oversized_upload_rejected(self, server, monkeypatch):
        monkeypatch.setattr("dvlib.server.app.MAX_ARCHIVE_BYTES", 8)
        r = publish(server, b"x" * 9)
        assert r.status_code == 413

    def test_missing_declared_file_rejected(self, server, tmp_path):
        # Build a tarball whose manifest declares a file that is absent.
        import io
        import tarfile

        manifest = '[package]\nname="vip"\nversion="1.0.0"\n[compile]\nfiles=["src/missing.sv"]\n'
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("dvlib.toml")
            data = manifest.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        r = publish(server, buf.getvalue())
        assert r.status_code == 422

    def test_search_finds_published(self, server, tmp_path):
        publish(server, build_pkg(tmp_path, "uvm_kit", "1.4.0"))
        results = server.get("/api/v1/packages", params={"q": "uvm"}).json()
        assert len(results) == 1
        assert results[0]["name"] == "uvm_kit"
        assert results[0]["version"] == "1.4.0"
        assert "uvm_kit" in results[0]["description"]

    def test_search_prefers_latest_version(self, server, tmp_path):
        publish(server, build_pkg(tmp_path, "vip", "1.0.0"))
        publish(server, build_pkg(tmp_path, "vip", "1.10.0"))
        publish(server, build_pkg(tmp_path, "vip", "1.2.0"))
        results = server.get("/api/v1/packages", params={"q": "vip"}).json()
        assert results[0]["version"] == "1.10.0"

    def test_dependencies_recorded(self, server, tmp_path):
        deps = '[dependencies]\nuvm_kit = "^1.4.0"\n'
        publish(server, build_pkg(tmp_path, "vip", "1.0.0", deps=deps))
        detail = server.get("/api/v1/packages/vip").json()
        assert detail["versions"][0]["dependencies"] == {"uvm_kit": "^1.4.0"}


class TestYank:
    def test_yank_flow(self, server, tmp_path):
        publish(server, build_pkg(tmp_path, "vip", "1.0.0"))
        r = server.post("/api/v1/packages/vip/1.0.0/yank", headers=auth(server))
        assert r.status_code == 200
        detail = server.get("/api/v1/packages/vip").json()
        assert detail["versions"][0]["yanked"] is True
        # Still fetchable (old lockfiles keep working)
        assert server.get("/api/v1/packages/vip/1.0.0/tarball").status_code == 200

    def test_yank_requires_token(self, server, tmp_path):
        publish(server, build_pkg(tmp_path, "vip", "1.0.0"))
        r = server.post("/api/v1/packages/vip/1.0.0/yank")
        assert r.status_code == 401

    def test_yank_unknown_404(self, server):
        r = server.post("/api/v1/packages/ghost/1.0.0/yank", headers=auth(server))
        assert r.status_code == 404


class TestTokenCli:
    def test_create_list_revoke(self, tmp_path):
        from typer.testing import CliRunner

        from dvlib.server.cli import app

        runner = CliRunner()
        data = tmp_path / "data"
        r = runner.invoke(app, ["tokens", "create", "--label", "joe", "--data-dir", str(data)])
        assert r.exit_code == 0, r.output
        token = r.output.strip().splitlines()[-1]

        r = runner.invoke(app, ["tokens", "list", "--data-dir", str(data)])
        assert "joe" in r.output and "active" in r.output

        # Token authenticates.
        client = TestClient(create_app(data))
        blob = build_pkg(tmp_path, "vip", "1.0.0")
        r = client.post(
            "/api/v1/packages",
            files={"file": ("p.tar.gz", blob, "application/gzip")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201

        r = runner.invoke(app, ["tokens", "revoke", "--token", token, "--data-dir", str(data)])
        assert r.exit_code == 0
        r = client.post(
            "/api/v1/packages",
            files={"file": ("p.tar.gz", blob, "application/gzip")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403
