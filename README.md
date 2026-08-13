# DVLib

A package manager for SystemVerilog/UVM design-verification code, modeled on
pip/npm: a central registry server where engineers publish packages, and a CLI
client that installs a package plus its transitive dependencies and generates a
simulator-ready filelist (`dvlib.f`).

> [!IMPORTANT]
> DVLib is alpha software. Use a private registry for proprietary verification
> IP, put the registry behind TLS and network access controls, and back up its
> SQLite database and package storage together.

## Concepts

- **Manifest** — each package has a `dvlib.toml` declaring name, semver
  version, an explicit ordered `files` list (compile order is semantic in SV),
  `include_dirs` (-> `+incdir+`), `defines` (-> `+define+`), and semver-range
  dependencies.
- **Registry** — a FastAPI + SQLite server storing tarballs on disk. Reads are
  open; publish/yank require a bearer token.
- **Resolution** — a backtracking solver (resolvelib) picks exactly one version
  per package name (SV package names are global symbols, so npm-style nested
  duplicates are impossible). Semver ranges follow node-semver semantics: `^`,
  `~`, exact, and comparator sets (`>=1.2,<2.0`). Pre-releases only match when
  the range itself names a pre-release.
- **Install layout** — packages land in project-local `dvlib_packages/<name>/`,
  pinned in `dvlib.lock` (JSON, with sha256 + dev flags), with a global tarball
  cache at `~/.cache/dvlib`. sha256 is re-verified on every use (NFS-safe).
- **Yank** — a yanked version is hidden from new resolutions but remains
  fetchable for old lockfiles (Cargo-style reproducibility), with a warning.

## Quickstart

```bash
python -m pip install -e .  # into a Python 3.11+ environment

# 1. Run a registry (or use Docker, below)
dvlib-server run --data-dir ./registry-data --port 8000

# 2. Create a publish token
dvlib-server tokens create --label joe --data-dir ./registry-data

# 3. Point the client at the registry and log in
export DVLIB_REGISTRY=http://localhost:8000
dvlib login                    # paste the token at the hidden prompt

# 4. Create and publish a package
cd my_vip
dvlib init --name my_vip
# edit dvlib.toml: list src files in exact compile order
dvlib publish

# 5. Consume it from a testbench project
cd ../my_tb
# dvlib.toml: [dependencies] my_vip = "^1.0"
dvlib install            # resolves, fetches, verifies sha256, writes dvlib.lock
# compile with any simulator:
#   vcs -f dvlib.f ...      xrun -f dvlib.f ...      vsim -f dvlib.f ...
```

## Client commands

| Command | Purpose |
|---|---|
| `dvlib init` | Create a `dvlib.toml` skeleton |
| `dvlib login` | Store a publish token (`DVLIB_TOKEN` env overrides) |
| `dvlib publish` | Pack and publish the package in the current directory |
| `dvlib install [--no-dev]` | Resolve + install deps, write `dvlib.lock` and `dvlib.f` |
| `dvlib update [pkg]` | Re-resolve ignoring lockfile pins (one package or all) |
| `dvlib outdated` | Show installed packages with newer compatible versions |
| `dvlib tree` | Print the installed dependency tree |
| `dvlib search <q>` | Search the registry |
| `dvlib info <name>` | Show all published versions of a package |
| `dvlib yank <name> --version X` | Yank a version |
| `dvlib clean` | Remove `dvlib_packages/` and `dvlib.f` (keeps the lockfile) |

Registry URL resolution: `--registry` flag > `DVLIB_REGISTRY` env >
`http://localhost:8000`.

For any non-local registry, use an `https://` URL. Credentials are stored in
`~/.config/dvlib/credentials.toml` with owner-only permissions; for automation,
prefer the `DVLIB_TOKEN` environment variable supplied by your secret manager.

## Server

```bash
dvlib-server run --data-dir /srv/dvlib --host 0.0.0.0 --port 8000
dvlib-server tokens create --label joe --data-dir /srv/dvlib
dvlib-server tokens revoke --data-dir /srv/dvlib  # hidden token prompt
dvlib-server tokens list --data-dir /srv/dvlib
```

Data dir layout: `registry.db` (SQLite) + `packages/<name>/<version>.tar.gz`.
SQLite is a single-writer store — fine at company scale; migrate to Postgres
if concurrent-publish contention ever shows up.

The built-in server intentionally does not provide TLS, user accounts, quotas,
or malware scanning. Place it behind a production reverse proxy, restrict
network access, and treat all published package contents as untrusted input.
The server binds to `127.0.0.1` by default; binding to `0.0.0.0` must be an
explicit deployment decision.

### Docker

```bash
docker build -t dvlib-registry .
docker run -d -p 8000:8000 -v dvlib-data:/data dvlib-registry
# create a token inside the running container:
docker exec <container> dvlib-server tokens create --label joe --data-dir /data
```

## dvlib.toml reference

```toml
[package]
name = "my_vip"            # ^[a-z0-9][a-z0-9_-]{0,63}$
version = "1.2.0"          # semver; pre-releases allowed (2.0.0-beta.1)
description = "..."
authors = ["joe"]
license = "Proprietary"

[compile]
files = ["src/my_vip_pkg.sv", "src/my_vip_agent.sv"]  # exact compile order
include_dirs = ["src"]       # -> +incdir+; contents are packed into the tarball
defines = ["MY_VIP=1"]       # -> +define+MY_VIP=1

[dependencies]
uvm_kit = "^1.4.0"           # ^, ~, exact, or ">=1.2,<2.0"

[dev-dependencies]           # installed for the root project only; transitive
test_utils = "~0.3.0"        # dev-deps are ignored
```

UVM itself is intentionally not packaged — simulator vendors ship it. The
convention is that packages assume a tool-provided UVM.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check dvlib tests
python -m bandit -q -r dvlib
python -m build
python -m twine check dist/*
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow and
[SECURITY.md](SECURITY.md) for responsible vulnerability reporting. Release
history is recorded in [CHANGELOG.md](CHANGELOG.md).

## License

DVLib is released under the [MIT License](LICENSE).
