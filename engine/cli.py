"""Command-line entry point: `te`."""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

STUB_COMMANDS = (
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


@main.group("signal")
def signal_group() -> None:
    """E1: collect job posts into the signals table."""


@signal_group.command("collect")
@click.option("--board", "boards", multiple=True, metavar="SOURCE:SLUG",
              help="A board to read, e.g. greenhouse:zentrico. Repeatable. "
                   "SOURCE is greenhouse, lever, ashby or workable.")
@click.option("--boards-file", type=click.Path(dir_okay=False, exists=True, path_type=Path),
              default=None, help="File with one SOURCE:SLUG per line (# comments allowed).")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Read at most this many boards.")
@click.option("--dry-run", is_flag=True, help="Print the projected request count and exit.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on requests (default 500).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def signal_collect(
    boards: tuple[str, ...],
    boards_file: Path | None,
    limit: int | None,
    dry_run: bool,
    max_fetches: int | None,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Read ATS boards through Guard and insert new job posts as signals.

    A 404 is a wrong or private board and an empty list is a real board with
    nothing open; both are reported, neither stops the run.
    """
    from engine import db as dbmod
    from engine.guard.budget import Budget, BudgetExceeded, format_projection, project
    from engine.guard.cache import Cache
    from engine.guard.http import Guard, GuardConfigError
    from engine.signal import ats
    from engine.signal.boards import board_urls, collect_board, read_boards_file, store, unique_boards

    specs = list(boards) + (read_boards_file(boards_file) if boards_file else [])
    try:
        todo = unique_boards(specs)
    except ValueError as e:
        _fail(str(e))
        return
    if not todo:
        _fail("no boards given; use --board SOURCE:SLUG or --boards-file")
        return
    if limit is not None:
        todo = todo[:limit]
    budget = Budget() if max_fetches is None else Budget(max_fetches=max_fetches)
    cache = Cache(cache_dir)
    if dry_run:
        click.echo(format_projection(project(board_urls(todo), cache), budget))
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return

    width = max(len(f"{s}:{b}") for s, b in todo)

    async def run() -> tuple[int, int, int, int]:
        found = new = errors = 0
        async with Guard(cache=cache, budget=budget) as g:
            for source, slug in todo:
                name = f"{source}:{slug}"
                try:
                    r = await collect_board(source, slug, g)
                except ats.BoardFormatError as e:
                    errors += 1
                    click.echo(f"{name:<{width}}  error      {e}")
                    continue
                added = store(conn, r.signals)
                found += len(r.signals)
                new += added
                src = "cache" if r.from_cache else f"{r.requests} req"
                status = r.status if r.status is not None else "-"
                extra = f", {added} new" if r.signals else ""
                skipped = f", {r.skipped} skipped" if r.skipped else ""
                click.echo(f"{name:<{width}}  {r.state:<9} {status!s:>3}  {src:<7} "
                           f"{r.note}{extra}{skipped}")
            return found, new, errors, g.requests_made

    try:
        found, new, errors, made = asyncio.run(run())
    except (GuardConfigError, BudgetExceeded) as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    click.echo(f"boards {len(todo)}, signals {found}, new {new}, errors {errors}, "
               f"requests sent {made}")
    sys.exit(1 if errors else 0)


@signal_group.command("careers")
@click.option("--seeds", "seeds_file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data") / "seeds.csv", show_default=True,
              help="Seed list CSV: company, site, careers_url, source "
                   "(see docs/seeds.example.csv).")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Read at most this many seeds.")
@click.option("--dry-run", is_flag=True, help="Print the projected request count and exit.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on requests (default 500).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def signal_careers(
    seeds_file: Path,
    limit: int | None,
    dry_run: bool,
    max_fetches: int | None,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Read each seed company's careers page through Guard and insert new roles.

    A page that links to a Greenhouse, Lever, Ashby or Workable board is handed
    to that collector; otherwise titles are read from the page. Blocked, missing
    and robots-denied pages are reported, never worked around.
    """
    from engine import db as dbmod
    from engine.guard.budget import Budget, BudgetExceeded, format_projection, project
    from engine.guard.cache import Cache
    from engine.guard.http import Guard, GuardConfigError
    from engine.signal import careers
    from engine.signal.boards import store
    from engine.signal.seeds import SeedsError, load_seeds

    try:
        seeds = load_seeds(seeds_file)
    except FileNotFoundError:
        _fail(f"no seed file at {seeds_file}; copy the shape of docs/seeds.example.csv")
        return
    except SeedsError as e:
        _fail(str(e))
        return
    if not seeds:
        _fail(f"{seeds_file} has no seeds")
        return
    if limit is not None:
        seeds = seeds[:limit]
    budget = Budget() if max_fetches is None else Budget(max_fetches=max_fetches)
    cache = Cache(cache_dir)
    if dry_run:
        click.echo(format_projection(project(careers.page_urls(seeds), cache), budget))
        click.echo("each page that hands off to an ATS board adds up to "
                   f"{2 * careers.MAX_HANDOFFS} more (board JSON and its robots.txt)")
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return

    width = min(30, max(len(s.company) for s in seeds))

    async def run() -> tuple[int, int, int, int]:
        found = new = errors = 0
        async with Guard(cache=cache, budget=budget) as g:
            for seed in seeds:
                r = await careers.collect_seed(seed, g)
                added = store(conn, r.signals)
                found += len(r.signals)
                new += added
                errors += len(r.board_errors)
                src = "cache" if r.from_cache else f"{r.requests} req"
                status = r.status if r.status is not None else "-"
                extra = f", {added} new" if r.signals else ""
                click.echo(f"{seed.company[:width]:<{width}}  {r.outcome:<13} {status!s:>3}  "
                           f"{src:<7} {r.note}{extra}")
            return found, new, errors, g.requests_made

    try:
        found, new, errors, made = asyncio.run(run())
    except (GuardConfigError, BudgetExceeded) as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    click.echo(f"seeds {len(seeds)}, signals {found}, new {new}, errors {errors}, "
               f"requests sent {made}")
    sys.exit(1 if errors else 0)


@signal_group.command("add")
@click.option("--company", required=True, help="Company name as you would write it.")
@click.option("--title", required=True, help="Job title as posted.")
@click.option("--url", required=True, help="Where you saw the role (http(s) URL).")
@click.option("--location", default=None, help="Location as posted.")
@click.option("--posted-at", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Post date, YYYY-MM-DD, if the post shows one.")
@click.option("--dry-run", is_flag=True, help="Print the row and store nothing.")
@_db_option
def signal_add(
    company: str,
    title: str,
    url: str,
    location: str | None,
    posted_at: datetime | None,
    dry_run: bool,
    db_file: Path | None,
) -> None:
    """Record a role you saw yourself (source=manual). Adding it again changes nothing."""
    from engine import db as dbmod
    from engine.signal.boards import store
    from engine.signal.manual import make_signal

    try:
        sig = make_signal(company=company, title=title, url=url, location=location,
                          posted_at=posted_at.date() if posted_at else None,
                          today=datetime.now(UTC).date())
    except ValueError as e:
        _fail(str(e))
        return
    if dry_run:
        click.echo(f"would add {sig.signal_id}  {sig.company_name_raw} | {sig.title} | {sig.url}")
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        added = store(conn, [sig])
    finally:
        conn.close()
    verb = "added" if added else "already recorded"
    click.echo(f"{verb} {sig.signal_id}  {sig.company_name_raw} | {sig.title}")


@signal_group.command("classify")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Classify at most this many unclassified signals.")
@click.option("--company", "company_id", default=None,
              help="Only signals attached to this company_id.")
@click.option("--dry-run", is_flag=True,
              help="Print how many signals the rules and the model would place, and exit.")
@click.option("--no-model", is_flag=True,
              help="Rules only; titles the rules cannot place stay unclassified.")
@click.option("--max-model-calls", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on model calls (default 200).")
@_db_option
def signal_classify(
    limit: int | None,
    company_id: str | None,
    dry_run: bool,
    no_model: bool,
    max_model_calls: int | None,
    db_file: Path | None,
) -> None:
    """Set role_family and offer_type on signals that have no role_family yet.

    Regex rules first; titles they cannot place go to the cached title
    classifier (one call per distinct title, ever). Classified signals are
    never touched, so a re-run changes nothing.
    """
    from engine import db as dbmod
    from engine.guard.budget import Budget
    from engine.signal import classify

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    budget = Budget() if max_model_calls is None else Budget(max_model_calls=max_model_calls)
    try:
        rep = classify.run(conn, limit=limit, company_id=company_id, dry_run=dry_run,
                           use_model=not no_model, budget=budget)
    finally:
        conn.close()

    if dry_run:
        fresh = rep.model_titles - rep.model_cached
        click.echo(f"unclassified signals {rep.rows}: rules {rep.by_rules}, "
                   f"model {rep.by_model}")
        click.echo(f"model titles {rep.model_titles} ({rep.model_cached} cached, "
                   f"{fresh} would be calls{'; --no-model skips them' if no_model else ''})")
        return
    click.echo(f"unclassified signals {rep.rows}: rules {rep.by_rules}, model {rep.by_model}, "
               f"left null {rep.rows - rep.updated}")
    for fam in classify.FAMILIES:
        if rep.families[fam]:
            click.echo(f"  {fam:<12} {rep.families[fam]}")
    if rep.updated:
        click.echo("offer types: " + ", ".join(
            f"{k} {rep.offers[k]}" for k in ("candidate", "adjacent", "none") if rep.offers[k]))
    if rep.unresolved:
        click.echo(f"unresolved titles {len(rep.unresolved)}:")
        for title in rep.unresolved:
            click.echo(f"  {title}")
    for err in rep.errors:
        click.echo(f"model error  {err}")
    if rep.stopped:
        click.echo(f"stopped: {rep.stopped}")
    sys.exit(1 if rep.errors or rep.stopped else 0)


@signal_group.command("filter")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Filter at most this many signals (first by signal_id).")
@click.option("--company", "company_id", default=None,
              help="Only signals attached to this company_id.")
@click.option("--dry-run", is_flag=True, help="Print the counts and write nothing.")
@click.option("--today", "today_", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Run date, YYYY-MM-DD (default: today, UTC).")
@_db_option
def signal_filter(
    limit: int | None,
    company_id: str | None,
    dry_run: bool,
    today_: datetime | None,
    db_file: Path | None,
) -> None:
    """Mark each signal fresh or stale and India-located or not (Step 25).

    Fresh: posted_at, or first_seen when there is no post date, within 60 days
    of the run date. Stale signals are kept with fresh = 0. Nothing is dropped
    on location here; india_located is stored for the segment rule (Step 28).
    A re-run with the same inputs changes nothing.
    """
    from engine import db as dbmod
    from engine.signal import filter as sigfilter

    today = today_.date() if today_ else datetime.now(UTC).date()
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        rep = sigfilter.run(conn, today=today, limit=limit, company_id=company_id,
                            dry_run=dry_run)
    finally:
        conn.close()
    verb = "would change" if dry_run else "changed"
    click.echo(f"run date {today.isoformat()}, window {sigfilter.WINDOW_DAYS} days")
    click.echo(f"signals {rep.total}: fresh {rep.fresh}, stale {rep.stale}, "
               f"india_located {rep.india_located}; {verb} {rep.changed}")
    click.echo(f"fresh GTM signals {rep.fresh_gtm}, from seed-list companies "
               f"{rep.fresh_gtm_seed} (source careers)")
    if rep.by_source:
        click.echo("fresh GTM by source: " + ", ".join(
            f"{k} {v}" for k, v in sorted(rep.by_source.items())))
    if rep.fresh_gtm_missing:
        click.echo(f"fresh GTM signals missing a url, offer type or company name "
                   f"{rep.fresh_gtm_missing}")
    click.echo("note: seed companies whose careers page handed off to an ATS board are "
               "stored under that board's source; the hand-off is not recorded, so they "
               "are not counted as seed-list (the seed-list count is a lower bound)")


@main.group("resolve", invoke_without_command=True)
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Resolve at most this many companies (groups of signals).")
@click.option("--company", "company_id", default=None,
              help="Only this company: a seed's company_id (e.g. zentrico-example), a "
                   "placeholder id (unresolved-...), or a board as source:slug.")
@click.option("--dry-run", is_flag=True, help="Print the projected requests and exit.")
@click.option("--seeds", "seeds_file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data") / "seeds.csv", show_default=True,
              help="Seed list CSV; seed companies resolve to their own site.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on requests (default 500).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
@click.pass_context
def resolve_cmd(
    ctx: click.Context,
    limit: int | None,
    company_id: str | None,
    dry_run: bool,
    seeds_file: Path,
    max_fetches: int | None,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Find and verify each company's official domain (Step 26).

    Reads fresh GTM signals with no company. A domain is verified when the
    homepage title or og:site_name matches the company name (>= 85) and the
    site links back to the same ATS board (or, for seeds, the careers page is
    on the same domain). One check or none leaves the company at `sourced`
    with a review_reason. No model calls.
    """
    if ctx.invoked_subcommand is not None:
        return
    from engine import db as dbmod
    from engine.guard import search_api
    from engine.guard.budget import Budget, BudgetExceeded, format_projection, project
    from engine.guard.cache import Cache
    from engine.guard.http import Guard, GuardConfigError
    from engine.resolve import domain
    from engine.signal.seeds import SeedsError, load_seeds

    try:
        seeds = load_seeds(seeds_file)
    except FileNotFoundError:
        click.echo(f"no seed file at {seeds_file}; resolving without seeds")
        seeds = []
    except SeedsError as e:
        _fail(str(e))
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    budget = Budget() if max_fetches is None else Budget(max_fetches=max_fetches)
    cache = Cache(cache_dir)
    try:
        groups = domain.select_groups(conn, seeds, cache, company_id=company_id, limit=limit)
        if dry_run:
            by_kind: dict[str, int] = {}
            for g in groups:
                by_kind[g.kind] = by_kind.get(g.kind, 0) + 1
            click.echo(f"companies to resolve {len(groups)}"
                       + (": " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
                          if by_kind else ""))
            click.echo(format_projection(project(domain.projected_urls(groups), cache), budget))
            click.echo(f"each candidate domain adds its homepage and up to "
                       f"{domain.MAX_CAREERS_PAGES} careers pages (and robots.txt); at most "
                       f"{domain.MAX_CANDIDATES} candidates per company")
            if not search_api.is_configured():
                click.echo("search provider not configured: companies with no candidate "
                           "domain will be marked for review")
            return

        async def go() -> domain.Report:
            async with Guard(cache=cache, budget=budget) as g:
                return await domain.run(conn, groups, g, cache=cache, budget=budget)

        try:
            rep = asyncio.run(go())
        except (GuardConfigError, BudgetExceeded) as e:
            _fail(str(e))
            return
    finally:
        conn.close()

    for out in rep.outcomes:
        state = "error" if out.error else ("verified" if out.verified else "needs_review")
        detail = out.error or out.reason
        click.echo(f"{out.group.key[:40]:<40}  {state:<12} {out.company_id or '-':<30} {detail}")
    click.echo(f"companies {rep.groups}: verified {rep.verified}, needs_review "
               f"{sum(rep.needs_review.values())}, errors {len(rep.errors)}; "
               f"signals attached {rep.attached}")
    for code, n in sorted(rep.needs_review.items()):
        click.echo(f"  needs_review {code:<22} {n}")
    sys.exit(1 if rep.errors else 0)


@main.group()
def debug() -> None:
    """Redacted exports for debugging a record."""


@debug.command("export")
@click.argument("company_id")
@_db_option
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Output root (default: debug/).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
def debug_export(
    company_id: str, db_file: Path | None, out_dir: Path | None, cache_dir: Path | None
) -> None:
    """Write COMPANY_ID's record to debug/<company_id>/ with every contact value removed."""
    from engine import db as dbmod
    from engine.debug import ExportError, export_company
    from engine.guard.cache import Cache

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        dest = export_company(conn, company_id, out_dir, Cache(cache_dir))
    except ExportError as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    click.echo(f"exported {dest}")


@main.command("verify")
@_db_option
@click.option("--skip-tests", is_flag=True, help="Do not run the test suite.")
@click.option("--root", type=click.Path(file_okay=False, exists=True, path_type=Path),
              default=Path("."), show_default=True, help="Repository root.")
def verify_cmd(db_file: Path | None, skip_tests: bool, root: Path) -> None:
    """Run the pre-commit checks; exit 1 if any fails."""
    from engine.verify import format_result, run_all

    results = run_all(root, db_file, skip_tests=skip_tests)
    width = max(len(r.name) for r in results)
    for r in results:
        click.echo(format_result(r, width))
    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
