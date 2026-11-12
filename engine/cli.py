"""Command-line entry point: `te`."""

import asyncio
import os
import sys
from pathlib import Path

import click

STUB_COMMANDS = (
    "find",
    "understand",
    "judge",
    "build",
    "draft",
    "send",
)


NO_DOTENV_ENV_VAR = "TE_NO_DOTENV"  # set by the test suite so .env is never loaded


@click.group()
def main() -> None:
    """Teardown engine."""
    if not os.environ.get(NO_DOTENV_ENV_VAR):
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))


def _make_stub(name: str) -> click.Command:
    @click.command(name=name, help=f"{name} (not implemented).")
    def _stub() -> None:
        click.echo(f"{name}: not implemented")
        sys.exit(1)

    return _stub


for _name in STUB_COMMANDS:
    main.add_command(_make_stub(_name))


@main.command()
def smoke() -> None:
    """Check every external provider; exit 1 if any fails."""
    from engine.smoke import CHECKS, format_result, run_check

    width = max(len(name) for name, _ in CHECKS)
    failed = False
    for name, fn in CHECKS:
        result = run_check(name, fn)
        click.echo(format_result(result, width))
        failed |= not result.ok
    sys.exit(1 if failed else 0)


_db_option = click.option(
    "--db",
    "db_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Database file (default: $TE_DB or data/engine.db).",
)


def _fail(message: str) -> None:
    click.echo(message, err=True)
    sys.exit(1)


@main.group()
def db() -> None:
    """Create and migrate the database."""


@db.command("init")
@_db_option
def db_init(db_file: Path | None) -> None:
    """Create the database and apply every migration."""
    from engine import db as dbmod

    try:
        path, applied = dbmod.init_db(db_file)
        conn = dbmod.connect(path)
        version = dbmod.user_version(conn)
        conn.close()
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    if applied:
        click.echo(f"created {path} at schema version {version}")
    else:
        click.echo(f"{path} already initialised at schema version {version}")


@db.command("migrate")
@_db_option
@click.option("--dry-run", is_flag=True, help="List pending migrations and exit.")
def db_migrate(db_file: Path | None, dry_run: bool) -> None:
    """Back up, then apply any migrations newer than the database."""
    from engine import db as dbmod

    try:
        applied, backup_path = dbmod.migrate(db_file, dry_run=dry_run)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    if not applied:
        click.echo("up to date")
        return
    verb = "would apply" if dry_run else "applied"
    for mig in applied:
        click.echo(f"{verb} {mig.name}")
    if backup_path is not None:
        click.echo(f"backup {backup_path}")


@main.command("backup")
@_db_option
def backup_cmd(db_file: Path | None) -> None:
    """Copy the database to data/backups/ and keep the newest 14."""
    from engine.db import DatabaseError
    from engine.guard.backup import backup

    try:
        dest = backup(db_file)
    except DatabaseError as e:
        _fail(str(e))
        return
    click.echo(f"backup {dest}")


@main.command("status")
@_db_option
def status_cmd(db_file: Path | None) -> None:
    """Print the number of companies in each status."""
    from engine import db as dbmod
    from engine.states import status_counts

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        counts = status_counts(conn)
    finally:
        conn.close()
    width = max(len(s.value) for s in counts)
    for status, n in counts.items():
        click.echo(f"{status.value:<{width}}  {n}")
    click.echo(f"{'total':<{width}}  {sum(counts.values())}")


@main.command("fetch")
@click.argument("urls", nargs=-1, required=True)
@click.option("--kind", default="page", show_default=True, help="Artifact kind label.")
@click.option("--fresh", is_flag=True, help="Bypass today's cache (send-day rechecks only).")
@click.option("--dry-run", is_flag=True, help="Print the projected request count and exit.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on requests (default 500).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
def fetch_cmd(
    urls: tuple[str, ...],
    kind: str,
    fresh: bool,
    dry_run: bool,
    max_fetches: int | None,
    cache_dir: Path | None,
) -> None:
    """Fetch URLs through Guard and print each outcome."""
    from engine.guard.budget import Budget, BudgetExceeded, format_projection, project
    from engine.guard.cache import Cache
    from engine.guard.http import Guard, GuardConfigError

    budget = Budget() if max_fetches is None else Budget(max_fetches=max_fetches)
    cache = Cache(cache_dir)
    if dry_run:
        click.echo(format_projection(project(list(urls), cache, fresh=fresh), budget))
        return

    async def run() -> int:
        async with Guard(cache=cache, budget=budget) as g:
            for url in urls:
                r = await g.fetch(url, kind, fresh=fresh)
                src = "cache" if r.from_cache else f"{r.requests} req"
                status = r.status if r.status is not None else "-"
                note = f"  {r.detail}" if r.detail else ""
                click.echo(f"{r.outcome:<13} {status!s:>3}  {src:<7} {url}{note}")
            return g.requests_made

    try:
        made = asyncio.run(run())
    except (GuardConfigError, BudgetExceeded, ValueError) as e:
        _fail(str(e))
        return
    click.echo(f"requests sent: {made}")


if __name__ == "__main__":
    main()
