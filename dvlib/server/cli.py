"""dvlib-server: run the registry and manage publish tokens."""

from __future__ import annotations

import secrets
from pathlib import Path

import typer

from .app import hash_token
from .db import Database

app = typer.Typer(help="DVLib registry server", no_args_is_help=True)
tokens_app = typer.Typer(help="Manage publish tokens", no_args_is_help=True)
app.add_typer(tokens_app, name="tokens")

DATA_DIR_OPTION = typer.Option(Path("./dvlib-data"), "--data-dir", help="Registry data directory")


def _db(data_dir: Path) -> Database:
    return Database(data_dir / "registry.db")


@app.command()
def run(
    data_dir: Path = DATA_DIR_OPTION,
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the registry server."""
    import uvicorn

    from .app import create_app

    typer.echo(f"DVLib registry: data dir {data_dir}, listening on {host}:{port}")
    uvicorn.run(create_app(data_dir), host=host, port=port)


@tokens_app.command("create")
def tokens_create(
    label: str = typer.Option(..., "--label", help="Human label for the token"),
    data_dir: Path = DATA_DIR_OPTION,
) -> None:
    """Create a publish token. The token is shown once — store it safely."""
    token = secrets.token_urlsafe(32)
    db = _db(data_dir)
    db.insert_token(hash_token(token), label)
    typer.echo(f"Token created for {label!r}. Store it now; it cannot be recovered:")
    typer.echo(token)


@tokens_app.command("revoke")
def tokens_revoke(
    token: str = typer.Option(
        ..., "--token", prompt=True, hide_input=True, help="The token to revoke"
    ),
    data_dir: Path = DATA_DIR_OPTION,
) -> None:
    """Revoke a publish token."""
    db = _db(data_dir)
    if db.revoke_token(hash_token(token)):
        typer.echo("Token revoked.")
    else:
        raise typer.BadParameter("unknown token")


@tokens_app.command("list")
def tokens_list(data_dir: Path = DATA_DIR_OPTION) -> None:
    """List token labels and status (hashes are never shown)."""
    db = _db(data_dir)
    for t in db.list_tokens():
        status = "revoked" if t["revoked"] else "active"
        typer.echo(f"{t['label']}\t{status}\tcreated {t['created_at']}")
