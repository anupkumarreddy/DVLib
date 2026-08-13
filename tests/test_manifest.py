import pytest

from dvlib.common.manifest import ManifestError, load_manifest, loads_manifest

VALID = """\
[package]
name = "my_vip"
version = "1.2.0"
description = "A VIP"
authors = ["joe"]
license = "Proprietary"

[compile]
files = ["src/my_vip_pkg.sv", "src/my_vip_agent.sv"]
include_dirs = ["src"]
defines = ["MY_VIP=1", "DEBUG"]

[dependencies]
uvm_kit = "^1.4.0"

[dev-dependencies]
test_utils = "~0.3.0"
"""


class TestValid:
    def test_full_manifest(self):
        m = loads_manifest(VALID)
        assert m.name == "my_vip"
        assert str(m.version) == "1.2.0"
        assert m.description == "A VIP"
        assert m.authors == ["joe"]
        assert m.license == "Proprietary"
        assert m.files == ["src/my_vip_pkg.sv", "src/my_vip_agent.sv"]
        assert m.include_dirs == ["src"]
        assert m.defines == ["MY_VIP=1", "DEBUG"]
        assert str(m.dependencies["uvm_kit"]) == "^1.4.0"
        assert str(m.dev_dependencies["test_utils"]) == "~0.3.0"

    def test_minimal_manifest(self):
        m = loads_manifest('[package]\nname="a"\nversion="0.1.0"\n[compile]\nfiles=["a.sv"]\n')
        assert m.dependencies == {}
        assert m.dev_dependencies == {}
        assert m.include_dirs == []
        assert m.defines == []


class TestInvalid:
    @pytest.mark.parametrize("name", ["MyPkg", "_foo", "-foo", "", "a b", "a.b", "x" * 65])
    def test_bad_names(self, name):
        with pytest.raises(ManifestError):
            loads_manifest(
                f'[package]\nname="{name}"\nversion="1.0.0"\n[compile]\nfiles=["a.sv"]\n'
            )

    @pytest.mark.parametrize("name", ["a", "a-b_c9", "x" * 64])
    def test_good_names(self, name):
        m = loads_manifest(
            f'[package]\nname="{name}"\nversion="1.0.0"\n[compile]\nfiles=["a.sv"]\n'
        )
        assert m.name == name

    def test_missing_package_table(self):
        with pytest.raises(ManifestError, match="package"):
            loads_manifest('[compile]\nfiles=["a.sv"]\n')

    def test_bad_version(self):
        with pytest.raises(ManifestError, match="version"):
            loads_manifest('[package]\nname="a"\nversion="1.2"\n[compile]\nfiles=["a.sv"]\n')

    def test_build_metadata_rejected_for_package_identity(self):
        with pytest.raises(ManifestError, match="build metadata"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.2.3+build.1"\n[compile]\nfiles=["a.sv"]\n'
            )

    def test_empty_files(self):
        with pytest.raises(ManifestError, match="files"):
            loads_manifest('[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=[]\n')

    def test_absolute_file_path(self):
        with pytest.raises(ManifestError, match="relative"):
            loads_manifest('[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["/abs/a.sv"]\n')

    def test_dotdot_file_path(self):
        with pytest.raises(ManifestError, match=r"\.\."):
            loads_manifest('[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["../a.sv"]\n')

    @pytest.mark.parametrize("path", ["", ".", "src/my pkg.sv", "src\\pkg.sv", "src/a.sv\n+x"])
    def test_nonportable_file_path(self, path):
        with pytest.raises(ManifestError, match="portable relative path"):
            loads_manifest(
                f'[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=[{path!r}]\n'.replace(
                    "'", '"'
                )
            )

    def test_duplicate_compile_entry(self):
        with pytest.raises(ManifestError, match="duplicate"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["a.sv", "a.sv"]\n'
            )

    def test_self_dependency(self):
        with pytest.raises(ManifestError, match="itself"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.0.0"\n'
                '[compile]\nfiles=["a.sv"]\n[dependencies]\na="^1"\n'
            )

    def test_bad_dependency_range(self):
        with pytest.raises(ManifestError, match="dependencies"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["a.sv"]\n'
                '[dependencies]\nb="=>1.0"\n'
            )

    def test_dep_dev_overlap(self):
        with pytest.raises(ManifestError, match="both"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["a.sv"]\n'
                '[dependencies]\nb="^1"\n[dev-dependencies]\nb="^1"\n'
            )

    def test_bad_define(self):
        with pytest.raises(ManifestError, match="defines"):
            loads_manifest(
                '[package]\nname="a"\nversion="1.0.0"\n[compile]\nfiles=["a.sv"]\n'
                'defines=["1BAD=1"]\n'
            )

    def test_toml_syntax_error(self):
        with pytest.raises(ManifestError):
            loads_manifest("[package\n")


class TestLoadFromDisk:
    def test_load_from_directory(self, tmp_path):
        (tmp_path / "dvlib.toml").write_text(VALID)
        m = load_manifest(tmp_path)
        assert m.name == "my_vip"

    def test_missing_file(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path)
