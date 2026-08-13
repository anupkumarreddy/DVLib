"""dvlib: command-line client for the DVLib package registry."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from ..common.lockfile import load_lockfile
from ..common.manifest import MANIFEST_FILENAME, ManifestError, load_manifest
from ..common.tarball import TarballError, pack_package
from . import config
from .api import RegistryClient, RegistryError
from .filelist import FILELIST_NAME, PACKAGES_DIR, load_installed
from .installer import InstallError, install

app = typer.Typer(help="DVLib package manager client", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

REGISTRY_OPTION = typer.Option(None, "--registry", help="Registry URL (or DVLIB_REGISTRY env var)")


def _die(msg: str) -> None:
    err_console.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(1)


def _client(registry: str | None, *, require_auth: bool = False) -> RegistryClient:
    url = config.resolve_registry(registry)
    token = config.load_token(url)
    if require_auth and not token:
        _die(f"no token for {url}. Run `dvlib login` or set DVLIB_TOKEN.")
    return RegistryClient(url, token)


@app.command()
def init(
    name: str | None = typer.Option(None, "--name", help="Package name"),
    version: str = typer.Option("0.1.0", "--version"),
    directory: Path = typer.Option(Path("."), "--dir", "-C"),
) -> None:
    """Create a dvlib.toml skeleton in the current project."""
    directory = directory.resolve()
    target = directory / MANIFEST_FILENAME
    if target.exists():
        _die(f"{MANIFEST_FILENAME} already exists in {directory}")
    name = name or directory.name.lower().replace(" ", "_")
    target.write_text(
        f"""\
[package]
name = "{name}"
version = "{version}"
description = ""
authors = []
license = "Proprietary"

[compile]
files = ["src/{name}_pkg.sv"]
include_dirs = ["src"]
defines = []

[dependencies]

[dev-dependencies]
""",
        encoding="utf-8",
    )
    console.print(f"Created {target}")


@app.command()
def login(
    registry: str | None = REGISTRY_OPTION,
    token: str = typer.Option(..., "--token", prompt=True, hide_input=True),
) -> None:
    """Store a publish token for a registry (DVLIB_TOKEN env var overrides)."""
    url = config.resolve_registry(registry)
    path = config.store_token(url, token)
    console.print(f"Token stored for {url} in {path}")


@app.command()
def publish(
    registry: str | None = REGISTRY_OPTION,
    directory: Path = typer.Option(Path("."), "--dir", "-C"),
) -> None:
    """Pack the current package and publish it to the registry."""
    directory = directory.resolve()
    try:
        manifest = load_manifest(directory)
    except ManifestError as e:
        _die(str(e))
    try:
        blob = pack_package(directory, manifest)
    except TarballError as e:
        _die(str(e))
    with _client(registry, require_auth=True) as client:
        try:
            result = client.publish(blob)
        except RegistryError as e:
            _die(str(e))
    console.print(
        f"[green]Published[/green] {result['name']}@{result['version']} "
        f"(sha256 {result['sha256'][:12]}…)"
    )


@app.command("install")
def install_cmd(
    registry: str | None = REGISTRY_OPTION,
    directory: Path = typer.Option(Path("."), "--dir", "-C"),
    no_dev: bool = typer.Option(False, "--no-dev", help="Skip dev-dependencies"),
) -> None:
    """Install dependencies and generate dvlib.f."""
    directory = directory.resolve()
    with _client(registry) as client:
        try:
            result = install(directory, client, config.cache_dir(), include_dev=not no_dev)
        except (InstallError, ManifestError, RegistryError) as e:
            _die(str(e))
    for item in result.items:
        tags = []
        if item.dev:
            tags.append("dev")
        if item.from_cache:
            tags.append("cached")
        if item.yanked:
            tags.append("[bold red]YANKED[/bold red]")
        suffix = f" ({', '.join(tags)})" if tags else ""
        console.print(f"  installed {item.name}@{item.version}{suffix}")
    for item in result.items:
        if item.yanked:
            err_console.print(
                f"[yellow]warning:[/yellow] {item.name}@{item.version} is yanked; "
                "kept for lockfile reproducibility"
            )
    console.print(f"[green]Wrote[/green] {result.lock_path.name} and {result.filelist_path.name}")


@app.command()
def update(
    package: str | None = typer.Argument(None, help="Package to update (default: all)"),
    registry: str | None = REGISTRY_OPTION,
    directory: Path = typer.Option(Path("."), "--dir", "-C"),
    no_dev: bool = typer.Option(False, "--no-dev"),
) -> None:
    """Re-resolve dependencies, ignoring lockfile pins (for one package or all)."""
    directory = directory.resolve()
    try:
        lock = load_lockfile(directory)
    except Exception as e:
        _die(str(e))
    if package is not None:
        if not lock or package not in lock.packages:
            _die(f"{package!r} is not in dvlib.lock (nothing to update)")
        refresh = {package}
    else:
        refresh = set(lock.packages) if lock else set()
    with _client(registry) as client:
        try:
            result = install(
                directory,
                client,
                config.cache_dir(),
                include_dev=not no_dev,
                refresh=refresh,
            )
        except (InstallError, ManifestError, RegistryError) as e:
            _die(str(e))
    old = {n: str(e.version) for n, e in (lock.packages.items() if lock else [])}
    changed = [
        f"{i.name} {old.get(i.name)} -> {i.version}"
        for i in result.items
        if old.get(i.name) != i.version
    ]
    if changed:
        for c in changed:
            console.print(f"  updated {c}")
    else:
        console.print("All dependencies already up to date.")


@app.command()
def outdated(
    registry: str | None = REGISTRY_OPTION,
    directory: Path = typer.Option(Path("."), "--dir", "-C"),
) -> None:
    """List installed packages that have newer compatible versions."""
    directory = directory.resolve()
    try:
        manifest = load_manifest(directory)
        lock = load_lockfile(directory)
    except (ManifestError, Exception) as e:
        _die(str(e))
    if not lock:
        _die("no dvlib.lock — run `dvlib install` first")
    reqs = dict(manifest.dependencies)
    reqs.update(manifest.dev_dependencies)

    table = Table("package", "locked", "latest matching", "range")
    any_outdated = False
    with _client(registry) as client:
        for name, entry in sorted(lock.packages.items()):
            spec = reqs.get(name)
            if spec is None:
                continue
            versions = client.get_versions(name) or []
            newer = [
                v
                for v in versions
                if not v.yanked and spec.matches(v.version) and v.version > entry.version
            ]
            if newer:
                any_outdated = True
                latest = max(v.version for v in newer)
                table.add_row(name, str(entry.version), str(latest), str(spec))
    if any_outdated:
        console.print(table)
    else:
        console.print("All dependencies are up to date.")


@app.command()
def tree(directory: Path = typer.Option(Path("."), "--dir", "-C")) -> None:
    """Print the installed dependency tree."""
    directory = directory.resolve()
    try:
        project = load_manifest(directory)
        lock = load_lockfile(directory)
    except ManifestError as e:
        _die(str(e))
    installed = load_installed(directory)
    if not installed:
        _die(f"no installed packages under {PACKAGES_DIR}/ — run `dvlib install`")

    root = Tree(f"{project.name} {project.version}")

    def add(node: Tree, name: str, seen: frozenset[str]) -> None:
        pkg = installed.get(name)
        if pkg is None:
            node.add(f"{name} [red](missing)[/red]")
            return
        entry = lock.packages.get(name) if lock else None
        label = f"{name} {pkg.manifest.version}"
        if entry and entry.dev:
            label += " [dim](dev)[/dim]"
        child = node.add(label)
        if name in seen:
            return
        for dep in sorted(pkg.manifest.dependencies):
            add(child, dep, seen | {name})

    for dep in sorted(project.dependencies):
        add(root, dep, frozenset())
    if project.dev_dependencies:
        dev_node = root.add("[dim]dev-dependencies[/dim]")
        for dep in sorted(project.dev_dependencies):
            add(dev_node, dep, frozenset())
    console.print(root)


@app.command()
def search(query: str = typer.Argument(""), registry: str | None = REGISTRY_OPTION) -> None:
    """Search the registry by package name substring."""
    with _client(registry) as client:
        try:
            results = client.search(query)
        except RegistryError as e:
            _die(str(e))
    if not results:
        console.print("No packages found.")
        return
    table = Table("name", "latest", "description")
    for r in results:
        table.add_row(r["name"], r["version"], r.get("description", ""))
    console.print(table)


@app.command()
def info(name: str, registry: str | None = REGISTRY_OPTION) -> None:
    """Show all published versions of a package."""
    with _client(registry) as client:
        versions = client.get_versions(name)
    if versions is None:
        _die(f"package {name!r} not found")
    table = Table("version", "yanked", "sha256", "dependencies")
    for v in sorted(versions, key=lambda v: v.version, reverse=True):
        deps = ", ".join(f"{k} {s}" for k, s in sorted(v.dependencies.items()))
        table.add_row(
            str(v.version),
            "yes" if v.yanked else "",
            v.sha256[:12] + "…",
            deps,
        )
    console.print(f"[bold]{name}[/bold]")
    console.print(table)


@app.command()
def yank(
    name: str,
    version: str = typer.Option(..., "--version"),
    registry: str | None = REGISTRY_OPTION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Yank a published version (hidden from new resolutions, still fetchable)."""
    if not yes:
        typer.confirm(f"Yank {name}@{version}?", abort=True)
    with _client(registry, require_auth=True) as client:
        try:
            client.yank(name, version)
        except RegistryError as e:
            _die(str(e))
    console.print(f"[yellow]Yanked[/yellow] {name}@{version}")


@app.command()
def clean(directory: Path = typer.Option(Path("."), "--dir", "-C")) -> None:
    """Remove installed packages and the generated filelist (keeps dvlib.lock)."""
    directory = directory.resolve()
    removed = []
    for p in (directory / PACKAGES_DIR, directory / FILELIST_NAME):
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(p.name)
        elif p.is_file():
            p.unlink()
            removed.append(p.name)
    console.print("Removed " + ", ".join(removed) if removed else "Nothing to clean.")
