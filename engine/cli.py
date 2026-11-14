"""Command-line entry point: `te`."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

STUB_COMMANDS = (
    "understand",
    "build",
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


@resolve_cmd.command("dedupe")
@click.option("--company", "company_id", default=None,
              help="Only flag pairs that include this company_id (the invariant check "
                   "always covers every company).")
@click.option("--dry-run", is_flag=True, help="Report and flag nothing; write no flags.")
@_db_option
def resolve_dedupe(company_id: str | None, dry_run: bool, db_file: Path | None) -> None:
    """Check one row per domain and flag possible duplicates (Step 27).

    Reports any domain with more than one row, or signals that point away
    from their domain's row. Flags pairs with the same name stem on different
    suffixes (acme.com / acme.in) or names scoring >= 90; flags are stored
    for the review and never merged. No network, no model calls. Exits 1 when
    the invariant is broken.
    """
    from engine import db as dbmod
    from engine.resolve import dedupe

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        rep = dedupe.run(conn, company_id=company_id, dry_run=dry_run)
    finally:
        conn.close()

    if rep.ok:
        click.echo(f"invariant ok: {rep.companies} companies, one row per registrable "
                   "domain, signals on their domain's row")
    else:
        click.echo(f"invariant broken: {len(rep.violations)} violation(s); nothing was "
                   "changed, fix by hand")
        for v in rep.violations:
            click.echo(f"  {v}")
    for f in rep.flags:
        click.echo(f"flag {f.kind:<16} {f.company_id} <-> {f.other_company_id}  "
                   f"score {f.score:.0f}  {f.detail}")
    verb = "would add" if dry_run else "added"
    click.echo(f"possible duplicates {len(rep.flags)}; {verb} {rep.new_flags} new flag(s); "
               "never merged")
    sys.exit(0 if rep.ok else 1)


@resolve_cmd.command("segment")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Tag at most this many companies.")
@click.option("--company", "company_id", default=None,
              help="Only this company_id (also retries one marked segment_unclear).")
@click.option("--dry-run", is_flag=True, help="Print the projected requests and exit.")
@click.option("--seeds", "seeds_file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data") / "seeds.csv", show_default=True,
              help="Seed list CSV; a seed with no head-office evidence is Indian.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on requests (default 500).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def resolve_segment(
    limit: int | None,
    company_id: str | None,
    dry_run: bool,
    seeds_file: Path,
    max_fetches: int | None,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Tag the segment from the head office and apply the location rule (Step 28).

    Reads the homepage and up to three contact or legal pages linked from it
    for entity names (Pvt Ltd, Inc., Delaware, CIN), addresses and phones.
    India -> in_saas; abroad with a fresh India-located GTM role ->
    us_india_team; abroad without one -> dropped out_of_scope; unclear ->
    review_reason "segment_unclear: ...". No model calls.
    """
    from engine import db as dbmod
    from engine.guard.budget import Budget, BudgetExceeded, format_projection, project
    from engine.guard.cache import Cache
    from engine.guard.http import Guard, GuardConfigError
    from engine.resolve import segment
    from engine.resolve.domain import registrable_domain
    from engine.signal.seeds import SeedsError, load_seeds

    try:
        seeds = load_seeds(seeds_file)
    except FileNotFoundError:
        click.echo(f"no seed file at {seeds_file}; no company gets the seed default")
        seeds = []
    except SeedsError as e:
        _fail(str(e))
        return
    seed_sites = {d: s.site for s in seeds if (d := registrable_domain(s.site))}
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    budget = Budget() if max_fetches is None else Budget(max_fetches=max_fetches)
    cache = Cache(cache_dir)
    try:
        targets = segment.select(conn, seed_sites, company_id=company_id, limit=limit)
        if dry_run:
            click.echo(f"companies to tag {len(targets)} "
                       f"(seed {sum(t.seed for t in targets)})")
            click.echo(format_projection(project([t.homepage for t in targets], cache), budget))
            click.echo(f"each homepage adds up to {segment.MAX_EXTRA_PAGES} contact or legal "
                       "pages linked from it")
            return

        async def go() -> segment.Report:
            async with Guard(cache=cache, budget=budget) as g:
                return await segment.run(conn, targets, g)

        try:
            rep = asyncio.run(go())
        except (GuardConfigError, BudgetExceeded) as e:
            _fail(str(e))
            return
    finally:
        conn.close()

    for out in rep.outcomes:
        d = out.decision
        detail = out.error or (f"{d.hq} ({d.basis})" if d and d.hq else "") \
            + (f"; {out.code}: {out.detail}" if out.code else "")
        click.echo(f"{out.target.company_id[:36]:<36}  {out.state:<14} {detail.lstrip('; ')}")
    click.echo(f"companies {rep.companies}: in_saas {rep.segments[segment.IN_SAAS]}, "
               f"us_india_team {rep.segments[segment.US_INDIA_TEAM]}, dropped out_of_scope "
               f"{rep.dropped}, needs_review {sum(rep.needs_review.values())}, errors "
               f"{len(rep.errors)}; changed {rep.changed}")
    for code, n in sorted(rep.needs_review.items()):
        click.echo(f"  needs_review {code:<24} {n}")
    if rep.pages:
        click.echo("pages: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.pages.items())))
    sys.exit(1 if rep.errors else 0)


@main.command("qualify")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Qualify at most this many companies (first by company_id).")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--dry-run", is_flag=True, help="Print the verdicts and write nothing.")
@_db_option
def qualify_cmd(limit: int | None, company_id: str | None, dry_run: bool,
                db_file: Path | None) -> None:
    """Apply the ICP rules and pick each company's signal (Step 29).

    Resolved companies with a segment, in the fixed order of protocol/ICP.md:
    out_of_scope, staffing_or_agency, consumer, then an open GTM role. The
    first rule that fails drops (drop_reason, drop_stage) or, when only a
    person can tell, holds with review_reason "qualify_hold: ...". Passing
    companies get best_signal_id and move to qualified. Size and conflicts
    are left for `te resolve review`. No network, no model calls.
    """
    from engine import db as dbmod
    from engine.resolve import qualify

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        rep = qualify.run(conn, company_id=company_id, limit=limit, dry_run=dry_run)
    finally:
        conn.close()

    for v in rep.verdicts:
        if v.action == "qualify":
            what = f"best {v.best_signal_id} (of {v.gtm_roles} fresh GTM)"
        elif v.action == "drop":
            what = f"{v.reason} ({v.stage}): {v.detail}"
        else:
            what = f"{v.reason}: {v.detail}"
        state = {"qualify": "qualified", "drop": "dropped", "hold": "held"}[v.action]
        click.echo(f"{v.target.company_id[:36]:<36}  {state:<9} {what}")
    for err in rep.errors:
        click.echo(f"error  {err}")
    tail = "dry run, nothing written" if dry_run else f"changed {rep.changed}"
    click.echo(f"companies {rep.companies}: qualified {rep.qualified}, dropped "
               f"{sum(rep.dropped.values())}, held for review {sum(rep.held.values())}, "
               f"errors {len(rep.errors)}; {tail}")
    for reason, n in sorted(rep.dropped.items()):
        click.echo(f"  dropped {reason:<24} {n}")
    for reason, n in sorted(rep.held.items()):
        click.echo(f"  held    {reason:<24} {n}")
    for code, n in sorted(rep.skipped.items()):
        click.echo(f"  skipped, already in review ({code}) {n}")
    if rep.unsegmented:
        click.echo(f"  resolved without a segment {rep.unsegmented}: run `te resolve segment`")
    click.echo("not applied here: too_large and conflict (`te resolve review`, Step 30)")
    sys.exit(1 if rep.errors else 0)


_REVIEW_KEYS = "a accept | d drop | s segment | h headcount | n next | q quit"


@resolve_cmd.command("review")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Review at most this many companies.")
@click.option("--only-review", is_flag=True,
              help="Only companies with a review_reason (default also shows qualified "
                   "companies no one has reviewed).")
@click.option("--pilot", is_flag=True, help="Only pilot companies.")
@click.option("--dry-run", is_flag=True, help="Print each company and ask nothing.")
@click.option("--seeds", "seeds_file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data") / "seeds.csv", show_default=True,
              help="Seed list CSV; a seed's own site is looked up for its homepage title.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Guard's cache, read for the homepage title (default: $TE_CACHE_DIR or "
                   "data/cache). Nothing is fetched.")
@_db_option
def resolve_review(company_id: str | None, limit: int | None, only_review: bool, pilot: bool,
                   dry_run: bool, seeds_file: Path, cache_dir: Path | None,
                   db_file: Path | None) -> None:
    """Review companies one at a time and decide by key (Step 30).

    Shows name, domain, segment, head office with its quotes, review_reason,
    possible duplicates, signals and the cached homepage title. a accepts
    (re-runs the qualify rules for it), d drops with an ICP reason, s sets
    the segment, h records the approximate headcount, n skips, q quits.
    No network, no model calls.
    """
    from engine import db as dbmod
    from engine.guard.cache import Cache
    from engine.resolve import review
    from engine.signal.seeds import SeedsError, load_seeds

    try:
        seed_sites = {s.domain: s.site for s in load_seeds(seeds_file)}
    except (FileNotFoundError, SeedsError):
        seed_sites = {}
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    cache = Cache(cache_dir)
    tally = {"accepted": 0, "held": 0, "dropped": 0, "skipped": 0}
    try:
        try:
            ids = review.select(conn, company_id=company_id, limit=limit,
                                only_review=only_review, pilot=pilot)
        except review.ReviewError as e:
            _fail(str(e))
            return
        if not ids:
            click.echo("nothing to review")
            return
        for i, cid in enumerate(ids, 1):
            while True:
                domain = conn.execute("SELECT domain FROM companies WHERE company_id = ?",
                                      (cid,)).fetchone()["domain"]
                click.echo(f"\n[{i}/{len(ids)}]")
                click.echo(review.render_company(conn, cid, cache=cache,
                                                 seed_site=seed_sites.get(domain or "")))
                if dry_run:
                    break
                key = click.prompt(_REVIEW_KEYS, type=click.Choice(list("adshnq")),
                                   show_choices=False)
                if key == "q":
                    click.echo(", ".join(f"{k} {v}" for k, v in tally.items()))
                    return
                if key == "n":
                    tally["skipped"] += 1
                    break
                if key == "a":
                    hc = conn.execute("SELECT headcount FROM companies WHERE company_id = ?",
                                      (cid,)).fetchone()["headcount"]
                    if hc is not None and hc > review.TOO_LARGE and not click.confirm(
                            f"headcount {hc} > {review.TOO_LARGE} (too_large); accept anyway?",
                            default=False):
                        continue
                    decision = review.Decision("accept")
                elif key == "d":
                    reason = click.prompt("drop reason", type=click.Choice(review.DROP_REASONS))
                    note = click.prompt("note (optional; never a person's name)", default="",
                                        show_default=False)
                    decision = review.Decision("drop", reason=reason, note=note)
                elif key == "s":
                    seg = click.prompt("segment", type=click.Choice(review.SEGMENTS))
                    decision = review.Decision("segment", segment=seg)
                else:
                    n = click.prompt("approximate headcount", type=click.IntRange(min=1))
                    decision = review.Decision("headcount", headcount=n)
                res = review.apply_decision(conn, cid, decision)
                click.echo(("ok: " if res.ok else "refused: ") + res.message)
                for w in res.warnings:
                    click.echo(f"warning: {w}")
                if res.ok and key in "ad":
                    if key == "d":
                        tally["dropped"] += 1
                    elif res.status == "qualified":
                        tally["accepted"] += 1
                    else:
                        tally["held"] += 1
                    break
    finally:
        conn.close()
    if not dry_run:
        click.echo(", ".join(f"{k} {v}" for k, v in tally.items()))


@main.group("company")
def company_group() -> None:
    """Companies entered by hand."""


@company_group.command("add")
@click.option("--hand", is_flag=True,
              help="Required: a Phase 0 company written to by hand (hand = 1, status sent).")
@click.option("--company", required=True, help="Company name.")
@click.option("--domain", required=True, help="Its domain, e.g. example.com.")
@click.option("--segment", required=True, type=click.Choice(["in_saas", "us_india_team"]))
@click.option("--role", required=True, help="The job title the teardown was written against.")
@click.option("--role-url", required=True, help="Where the role was posted (http(s) URL).")
@click.option("--offer", required=True, type=click.Choice(["candidate", "adjacent"]))
@click.option("--family", default=None,
              type=click.Choice(["ae", "demand_gen", "growth", "gtm_engineer", "revops",
                                 "sales_lead", "sdr"]),
              help="Role family, when the title rules cannot place the title.")
@click.option("--sent-on", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Date touch 1 was sent, YYYY-MM-DD.")
@click.option("--channel", type=click.Choice(["email", "linkedin"]), default="email",
              show_default=True)
@click.option("--reply", type=click.Choice(["positive", "question", "referral", "not_now",
                                                "negative", "optout"]), default=None,
              help="Reply to touch 1, if any (outcome replied_<type>); optout also "
                   "suppresses the domain.")
@click.option("--reply-on", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Date of the reply, YYYY-MM-DD.")
@click.option("--note", default=None, help="Free text about the company; never a person's name.")
@click.option("--adopt", is_flag=True,
              help="The domain is already in the pipeline: mark that row hand-made and move "
                   "it forward to sent.")
@click.option("--dry-run", is_flag=True, help="Validate and print; write nothing.")
@_db_option
def company_add(hand: bool, company: str, domain: str, segment: str, role: str, role_url: str,
                offer: str, family: str | None, sent_on: datetime | None, channel: str,
                reply: str | None, reply_on: datetime | None, note: str | None, adopt: bool,
                dry_run: bool, db_file: Path | None) -> None:
    """Enter a Phase 0 company with its role and touch 1 (Step 30).

    Creates the company (hand = 1, status sent), a manual signal for the
    role as its best signal, and touch 1 with any reply. No recipient details
    are taken. Idempotent by domain: a re-run adds only what is missing.
    """
    from engine import db as dbmod
    from engine.resolve import hand as handmod

    if not hand:
        _fail("only Phase 0 companies are added this way: pass --hand")
        return
    entry = handmod.HandEntry(
        company=company, domain=domain, segment=segment, role=role, role_url=role_url,
        offer=offer, family=family, sent_on=sent_on.date() if sent_on else None,
        reply=reply, reply_on=reply_on.date() if reply_on else None, channel=channel,
        note=note, adopt=adopt)
    today = datetime.now(UTC).date()
    if dry_run:
        try:
            dom, cid, fam, sig = handmod.prepare(entry, today)
        except handmod.HandError as e:
            _fail(str(e))
            return
        click.echo(f"would add {cid} ({dom}) hand = 1 at sent; signal {sig.signal_id} "
                   f"{fam}/{offer}; touch 1 via {channel}"
                   + (f", reply {reply}" if reply else ""))
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        res = handmod.add(conn, entry, today=today)
    except handmod.HandError as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    head = "created" if res.created else ("updated" if res.changes else "unchanged")
    click.echo(f"{head} {res.company_id} ({res.domain}); signal {res.signal_id} "
               f"{res.role_family}/{offer}")
    for c in res.changes:
        click.echo(f"  + {c}")
    for w in res.warnings:
        click.echo(f"warning: {w}")


@main.group("pilot")
def pilot_group() -> None:
    """Pick the pilot twelve (Step 31)."""


def _print_pilot(rows: list) -> None:
    for r in rows:
        click.echo(f"{r.company_id[:36]:<36} {r.segment or '-':<14} {r.offer_type or '-':<10} "
                   f"{r.status:<10} {r.title or '(no best signal)'}")


@pilot_group.command("pick")
@click.argument("company_ids", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Check the pick; write nothing.")
@_db_option
def pilot_pick(company_ids: tuple[str, ...], dry_run: bool, db_file: Path | None) -> None:
    """Mark COMPANY_IDS pilot = 1. Refuses hand-made, dropped or unqualified
    companies and more than 12 in all or 6 in a segment; warns on the rest."""
    from engine import db as dbmod
    from engine.resolve import pilot

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        new, warnings = pilot.pick(conn, list(company_ids), dry_run=dry_run)
    except pilot.PilotError as e:
        _fail(f"refused, nothing written:\n{e}")
        return
    finally:
        conn.close()
    verb = "would pick" if dry_run else "picked"
    click.echo(f"{verb} {len(new)}" + (": " + ", ".join(new) if new else ""))
    for w in warnings:
        click.echo(f"warning: {w}")


@pilot_group.command("list")
@_db_option
def pilot_list(db_file: Path | None) -> None:
    """Print the pilot companies and check Step 31; exit 1 if a check fails."""
    from engine import db as dbmod
    from engine.resolve import pilot

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        rows = pilot.pilots(conn)
    finally:
        conn.close()
    _print_pilot(rows)
    chk = pilot.check(rows)
    c = chk.counts
    click.echo(f"total {c['total']}/{pilot.TOTAL}; in_saas {c['in_saas']}, us_india_team "
               f"{c['us_india_team']} (want {pilot.PER_SEGMENT} each); candidate "
               f"{c['candidate']} (want >= {pilot.MIN_CANDIDATE})")
    if chk.ok:
        click.echo("checks ok")
    for p in chk.problems:
        click.echo(f"check: {p}")
    sys.exit(0 if chk.ok else 1)


@pilot_group.command("unpick")
@click.argument("company_id")
@_db_option
def pilot_unpick(company_id: str, db_file: Path | None) -> None:
    """Set pilot = 0 for COMPANY_ID."""
    from engine import db as dbmod
    from engine.resolve import pilot

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        changed = pilot.unpick(conn, company_id)
    except pilot.PilotError as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    click.echo(f"unpicked {company_id}" if changed else f"{company_id} was not in the pilot")


@main.command("capture")
@click.option("--pilot", is_flag=True, help="Only the pilot and hand sets.")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Capture at most this many companies.")
@click.option("--dry-run", is_flag=True, help="Print the projected requests and exit.")
@click.option("--recheck", "recheck_id", default=None, metavar="COMPANY_ID",
              help="Re-fetch, fresh, only the artifacts this company's latest diagnosis cites.")
@click.option("--max-in-flight", type=click.IntRange(min=1, max=4), default=4, show_default=True,
              help="Companies (so hosts) captured at once. Per-host pacing stays 1 request "
                   "per second whatever this is.")
@click.option("--max-fetches", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on HTTP requests (default 500).")
@click.option("--max-dns", type=click.IntRange(min=1), default=None,
              help="Per-run ceiling on DNS queries (default 1000).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def capture_cmd(
    pilot: bool,
    company_id: str | None,
    limit: int | None,
    dry_run: bool,
    recheck_id: str | None,
    max_in_flight: int,
    max_fetches: int | None,
    max_dns: int | None,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Capture each company's evidence bundle (Steps 32-34).

    One artifacts row per bundle kind, whatever the outcome; raw bodies in the
    cache. A qualified company with a complete bundle moves to captured, or is
    dropped no_site_evidence with fewer than 3 ok artifacts. No model calls.
    """
    from engine import db as dbmod
    from engine.capture import bundle
    from engine.capture import capture as cap
    from engine.guard.budget import Budget, BudgetExceeded
    from engine.guard.cache import Cache
    from engine.guard.dns import DnsGuard
    from engine.guard.http import GuardConfigError

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    budget = Budget()
    if max_fetches is not None:
        budget.max_fetches = max_fetches
    if max_dns is not None:
        budget.max_dns_queries = max_dns
    cache = Cache(cache_dir)
    try:
        if recheck_id is not None:
            kinds = cap.cited_kinds(conn, recheck_id)
            if kinds is None:
                _fail(f"{recheck_id}: no diagnosis, so nothing is cited; nothing rechecked")
                return
            if not kinds:
                click.echo(f"{recheck_id}: the latest diagnosis cites no bundle artifact; "
                           "nothing rechecked")
                return
            if dry_run:
                click.echo(f"dry run: would re-fetch fresh for {recheck_id}: {', '.join(kinds)}")
                return

            async def go_recheck() -> cap.Recheck:
                rt = cap.make_runtime(cache, budget, max_in_flight)
                try:
                    return await cap.recheck(conn, recheck_id, kinds, rt)
                finally:
                    await rt.aclose()

            try:
                rc = asyncio.run(go_recheck())
            except (GuardConfigError, BudgetExceeded, cap.CaptureError) as e:
                _fail(str(e))
                return
            for kind in kinds:
                a = rc.rows[kind]
                click.echo(f"{kind:<10} {a.outcome:<14} {a.url}  {a.detail or ''}".rstrip())
            click.echo(f"rechecked {len(kinds)} artifacts fresh; {rc.changed} changed; "
                       f"{rc.requests} requests, {rc.dns_queries} DNS queries")
            return

        try:
            sel = cap.select(conn, pilot=pilot, company_id=company_id, limit=limit)
        except cap.CaptureError as e:
            _fail(str(e))
            return
        for why in sel.refused:
            click.echo(f"refused {why}")
        if sel.complete:
            click.echo(f"already captured at {bundle.BUNDLE_VERSION}: {len(sel.complete)} "
                       "(nothing to do)")
        if company_id is not None and sel.refused:
            sys.exit(1)
        if dry_run:
            d = cap.dry_run(sel.targets, cache, DnsGuard(cache=cache, budget=budget))
            for line in cap.format_dry_run(d, budget):
                click.echo(line)
            return
        if not sel.targets:
            click.echo("companies to capture 0")
            return

        async def go() -> cap.Report:
            rt = cap.make_runtime(cache, budget, max_in_flight)
            try:
                return await cap.run(conn, sel.targets, rt, max_in_flight=max_in_flight)
            finally:
                await rt.aclose()

        rep = asyncio.run(go())
    finally:
        conn.close()

    for cid, state, counts in rep.results:
        line = ", ".join(f"{k} {counts[k]}" for k in ("ok", "not_found", "blocked", "dead",
                                                      "robots_denied") if counts[k])
        click.echo(f"{cid[:36]:<36}  {state:<9} {line}")
    for err in rep.errors:
        click.echo(f"error {err}")
    click.echo(f"companies {rep.companies}: captured {rep.states['captured']}, dropped "
               f"no_site_evidence {rep.states['dropped']}, status kept {rep.states['kept']}; "
               f"rows changed {rep.changed}")
    click.echo("artifacts by outcome: " + ", ".join(
        f"{k} {rep.outcomes[k]}" for k in ("ok", "not_found", "blocked", "dead", "robots_denied")))
    click.echo(f"blocked rate {rep.blocked_rate:.1%} ({rep.outcomes['blocked']} of {rep.rows} "
               f"artifacts)")
    click.echo("fetchers: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.fetchers.items()))
               + f"; playwright pages {rep.playwright_pages}; requests {rep.requests}; "
               f"DNS queries {rep.dns_queries}")
    if rep.stopped:
        click.echo(f"stopped: {rep.stopped}", err=True)
    sys.exit(1 if rep.errors or rep.stopped else 0)


@main.group("extract")
def extract_group() -> None:
    """Derive text and facts from captured artifacts (no network, no model calls)."""


@extract_group.command("text")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Only the pilot and hand sets.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="At most this many companies.")
@click.option("--reextract", is_flag=True,
              help="Re-derive artifacts already extracted at this version and check the "
                   "hashes are reproduced.")
@click.option("--dry-run", is_flag=True, help="Count what would be extracted; write nothing.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def extract_text_cmd(
    company_id: str | None,
    pilot: bool,
    limit: int | None,
    reextract: bool,
    dry_run: bool,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Extract text, links and forms from each ok artifact's cached body (Step 35).

    Reads raw bodies from the cache and writes the text and structure back to it;
    never fetches. Idempotent: an artifact already extracted at this version is
    skipped. Exits 1 if --reextract finds a hash that is not reproduced.
    """
    from engine import db as dbmod
    from engine.capture import extract_text as X
    from engine.guard.cache import Cache

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        try:
            ids = X.select_companies(conn, company_id=company_id, pilot=pilot, limit=limit)
        except X.ExtractError as e:
            _fail(str(e))
            return
        rep = X.run(conn, Cache(cache_dir), ids, reextract=reextract, dry_run=dry_run)
    finally:
        conn.close()
    verb = "would extract" if dry_run else "written"
    click.echo(f"extractor {X.EXTRACTOR_VERSION}: companies {rep.companies}, ok artifacts "
               f"{rep.considered}; {verb} {rep.written}, already current {rep.current}, "
               f"re-derived identical {rep.reproduced}, body missing {rep.missing_body}")
    for label in rep.missing:
        click.echo(f"body not in cache (not fetched): {label}")
    for label in rep.drift:
        click.echo(f"DRIFT: {label} re-extracted at {X.EXTRACTOR_VERSION} with a different "
                   "hash; row left as it was (bump EXTRACTOR_VERSION)")
    sys.exit(1 if rep.drift else 0)


@extract_group.command("facts")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Only the pilot and hand sets.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="At most this many companies.")
@click.option("--dry-run", is_flag=True, help="Compute and count the facts; write nothing.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def extract_facts_cmd(
    company_id: str | None,
    pilot: bool,
    limit: int | None,
    dry_run: bool,
    cache_dir: Path | None,
    db_file: Path | None,
) -> None:
    """Derive facts by rule from each captured bundle (Steps 37-38).

    Reads extracted text and structure from the cache; no network, no model
    calls. Every fact carries a verbatim quote. Idempotent: a company whose
    facts are unchanged is not rewritten. Moves captured -> extracted. Exits 1
    if a quote fails its check or the llm_calls ledger grew.
    """
    from engine import db as dbmod
    from engine.extract import run as R
    from engine.guard.cache import Cache

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        try:
            ids = R.select(conn, company_id=company_id, pilot=pilot, limit=limit)
        except R.ExtractFactsError as e:
            _fail(str(e))
            return
        rep = R.run(conn, Cache(cache_dir), ids, dry_run=dry_run)
    finally:
        conn.close()
    verb = "would rewrite" if dry_run else "rewrote"
    moved = "would move" if dry_run else "moved"
    click.echo(f"facts {rep.version}: companies {rep.companies}; {verb} {rep.changed} "
               f"({rep.rows} rows), unchanged {rep.unchanged}; {moved} captured -> extracted "
               f"{rep.advanced}")
    if rep.by_fact:
        click.echo("rows by fact: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.by_fact.items())))
    if rep.unseen:
        click.echo("skipped, artifact unseen (blocked, dead, robots_denied or missing): "
                   + ", ".join(f"{k} {v}" for k, v in sorted(rep.unseen.items())))
    if rep.unclassified_titles:
        click.echo(f"board titles unclassified (no rule, no cached model answer): "
                   f"{rep.unclassified_titles}")
    for line in rep.not_ready:
        click.echo(f"not ready: {line}")
    for cid in rep.no_facts:
        click.echo(f"no facts (nothing seen; status kept): {cid}")
    for line in rep.rejected:
        click.echo(f"REJECTED: {line}")
    click.echo(f"model calls this stage: {rep.model_calls}")
    sys.exit(1 if rep.rejected or rep.model_calls else 0)


@main.command("show")
@click.argument("company_id")
@click.option("--lines", "n_lines", type=click.IntRange(min=0), default=5, show_default=True,
              help="Lines of extracted text to print per artifact.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def show_cmd(company_id: str, n_lines: int, cache_dir: Path | None,
             db_file: Path | None) -> None:
    """Print COMPANY_ID's evidence bundle: outcomes, text, DNS (Step 36). Read-only."""
    from engine import db as dbmod
    from engine.capture.show import ShowError, show_lines
    from engine.guard.cache import Cache

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        out = show_lines(conn, company_id, Cache(cache_dir), n_lines)
    except ShowError as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # a Windows pipe is cp1252; page text may not be
        reconfigure(errors="backslashreplace")
    for line in out:
        click.echo(line)


# --- people (Steps 40-43) ---------------------------------------------------------


def _stdout_is_terminal() -> bool:
    """True when stdout is an interactive terminal (not a file or a pipe)."""
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


@main.group("people")
def people_group() -> None:
    """The recipient: rule, where to look, record, drop, list and check (Steps 40-43).

    The person finds the recipient by hand; these commands only show where to
    look and check and store what is typed. No names or addresses are printed
    unless asked, and never to a file.
    """


def _people_conn(db_file: Path | None):
    from engine import db as dbmod

    try:
        return dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))


@people_group.command("suggest")
@click.argument("company_id")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def people_suggest(company_id: str, cache_dir: Path | None, db_file: Path | None) -> None:
    """Print COMPANY_ID's recipient rule and where to look, in order. Read-only, no fetching."""
    from engine.guard.cache import Cache
    from engine.people.contacts import PeopleError
    from engine.people.suggest import suggest_lines

    conn = _people_conn(db_file)
    try:
        out = suggest_lines(conn, Cache(cache_dir), company_id)
    except PeopleError as e:
        _fail(str(e))
        return
    finally:
        conn.close()
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # a Windows pipe is cp1252; job text may not be
        reconfigure(errors="backslashreplace")
    for line in out:
        click.echo(line)


@people_group.command("add")
@click.argument("company_id")
@click.option("--name", required=True, help="The recipient's name, as found.")
@click.option("--title", required=True, help="Their title. Never a recruiter, talent or HR.")
@click.option("--source-url", required=True, help="The URL where you found them.")
@click.option("--email", default=None, help="Their address, if one was found.")
@click.option("--confidence", type=click.Choice(["high", "medium", "low"]), default=None,
              help="The address's confidence (required with --email).")
@click.option("--email-source", type=click.Choice(["hunter", "apollo", "pattern", "public",
                                                   "manual"]), default=None,
              help="Where the address came from (required with --email).")
@click.option("--linkedin-url", default=None, help="Their LinkedIn profile URL (typed by you).")
@click.option("--verified", is_flag=True,
              help="A pattern address was checked with Hunter's verifier (allows medium/high).")
@click.option("--force", is_flag=True,
              help="Store an address whose domain is not the company's.")
@click.option("--replace", is_flag=True, help="Overwrite the company's recorded recipient.")
@click.option("--dry-run", is_flag=True, help="Check and print; write nothing.")
@_db_option
def people_add(company_id: str, name: str, title: str, source_url: str, email: str | None,
               confidence: str | None, email_source: str | None, linkedin_url: str | None,
               verified: bool, force: bool, replace: bool, dry_run: bool,
               db_file: Path | None) -> None:
    """Record COMPANY_ID's recipient (one contacts row) and set its channel (Step 43).

    email at high or medium confidence; otherwise linkedin with --linkedin-url;
    with neither, refused (use `te people drop`). The name and address are not
    echoed.
    """
    from engine.people import contacts as P

    conn = _people_conn(db_file)
    try:
        try:
            plan = P.plan_add(conn, company_id, name=name, title=title, source_url=source_url,
                              email=email, confidence=confidence, email_source=email_source,
                              linkedin_url=linkedin_url, verified=verified, force=force)
        except P.PeopleError as e:
            _fail(f"{company_id}: {e}")
            return
        for w in plan.warnings:
            click.echo(f"warning: {w}")
        row = plan.row
        what = (f"title {row.title}; channel {row.channel}; confidence {row.confidence or '-'}; "
                f"email source {row.email_source or '-'}; linkedin "
                f"{'yes' if row.linkedin_url else 'no'}")
        if dry_run:
            verb = ("unchanged" if plan.unchanged else
                    "would replace" if plan.existing_id is not None else "would add")
            click.echo(f"dry run: {company_id}: {verb}: {what}")
            if plan.existing_id is not None and not plan.unchanged and not replace:
                click.echo("  (a recipient is recorded with different values; --replace "
                           "would be needed)")
            return
        try:
            done = P.write(conn, plan, replace=replace)
        except P.PeopleError as e:
            _fail(str(e))
            return
    finally:
        conn.close()
    click.echo(f"{company_id}: {done}: {what}")


@people_group.command("drop")
@click.argument("company_id")
@click.option("--dry-run", is_flag=True, help="Say what would happen; write nothing.")
@_db_option
def people_drop(company_id: str, dry_run: bool, db_file: Path | None) -> None:
    """Drop COMPANY_ID as no_recipient: nobody to name, or no way to reach them (Step 43)."""
    from engine.people import contacts as P

    conn = _people_conn(db_file)
    try:
        try:
            changed = P.drop(conn, company_id, dry_run=dry_run)
        except P.PeopleError as e:
            _fail(str(e))
            return
        has_row = P.recipient(conn, company_id) is not None
    finally:
        conn.close()
    if not changed:
        click.echo(f"{company_id} was already dropped {P.DROP_REASON}")
        return
    click.echo(f"{'dry run: would drop' if dry_run else 'dropped'} {company_id} "
               f"{P.DROP_REASON} (stage {P.DROP_STAGE})")
    if has_row:
        click.echo("note: a recipient row is still recorded for it; it is never used for a "
                   "dropped company")


def _people_rows(conn, pilot: bool, company_id: str | None, limit: int | None):
    from engine.people import contacts as P

    try:
        return P.overview(conn, P.select(conn, pilot=pilot, company_id=company_id, limit=limit))
    except P.PeopleError as e:
        _fail(str(e))


@people_group.command("list")
@click.option("--pilot", is_flag=True, help="Only pilot companies (pilot = 1), dropped included.")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="At most this many companies.")
@click.option("--show-contacts", is_flag=True,
              help="Also print names and addresses: to a terminal only, never to a file.")
@click.option("--dry-run", is_flag=True, help="Read-only anyway; accepted for convention.")
@_db_option
def people_list(pilot: bool, company_id: str | None, limit: int | None, show_contacts: bool,
                dry_run: bool, db_file: Path | None) -> None:
    """List each company's rule, recipient title, channel and confidence.

    Names and addresses only with --show-contacts, and only when stdout is a
    terminal.
    """
    if show_contacts and not _stdout_is_terminal():
        _fail("refused: --show-contacts prints personal data and only to a terminal; "
              "stdout is redirected to a file or a pipe")
        return
    conn = _people_conn(db_file)
    try:
        rows = _people_rows(conn, pilot, company_id, limit)
    finally:
        conn.close()
    click.echo(f"{'company':<30} {'headcount':>9}  {'rule':<10} {'recipient title':<30} "
               f"{'channel':<8} {'conf':<6} source")
    for o in rows:
        c = o.company
        rule = c.rule.band if c.status != "dropped" else f"dropped:{c.drop_reason}"
        hc = str(c.headcount) if c.headcount is not None else "?"
        click.echo(f"{c.company_id[:30]:<30} {hc:>9}  {rule:<10} {(o.title or '-')[:30]:<30} "
                   f"{o.channel or '-':<8} {o.confidence or '-':<6} {o.email_source or '-'}")
        if show_contacts and (o.name or o.email):
            click.echo(f"{'':<30}   name {o.name or '-'}; email {o.email or '-'}")
    click.echo(f"companies {len(rows)}; with a recipient "
               f"{sum(1 for o in rows if o.title is not None or o.has_name)}")


@people_group.command("check")
@click.option("--pilot", is_flag=True, help="The pilot twelve (Gate 3.3).")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="At most this many companies.")
@click.option("--dry-run", is_flag=True, help="Read-only anyway; accepted for convention.")
@_db_option
def people_check(pilot: bool, company_id: str | None, limit: int | None, dry_run: bool,
                 db_file: Path | None) -> None:
    """Gate 3.3: each company has a named recipient with a source URL and either an
    address at medium confidence or better, or a LinkedIn channel. Exit 1 if any fails."""
    from engine.people import contacts as P

    conn = _people_conn(db_file)
    try:
        rows = _people_rows(conn, pilot, company_id, limit)
    finally:
        conn.close()
    failed = 0
    for o in rows:
        problem = P.gate_problem(o)
        failed += problem is not None
        detail = problem or (f"{o.channel}" + (f" ({o.confidence})" if o.channel == "email"
                                                else ""))
        click.echo(f"{'FAIL' if problem else 'ok':<4} {o.company.company_id:<30} {detail}")
    click.echo(f"companies {len(rows)}: ok {len(rows) - failed}, fail {failed}")
    short = pilot and company_id is None and limit is None and len(rows) < P.GATE_SIZE
    if short:
        click.echo(f"FAIL the pilot has {len(rows)} companies, not {P.GATE_SIZE}")
    sys.exit(1 if failed or short or not rows else 0)


@people_group.command("pattern")
@click.argument("company_id")
@click.option("--first", default=None, help="A first name to build the address for.")
@click.option("--last", default=None, help="A last name to build the address for.")
@click.option("--verify", is_flag=True,
              help="Check the built address with Hunter's verifier (needs HUNTER_API_KEY).")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def people_pattern(company_id: str, first: str | None, last: str | None, verify: bool,
                   cache_dir: Path | None, db_file: Path | None) -> None:
    """List public addresses at COMPANY_ID's domain in its captured pages and propose a pattern.

    Stores nothing. An address built from the pattern is `low` unless Hunter's
    verifier says valid; mail servers are never contacted.
    """
    from engine.guard import email_finder as F
    from engine.guard.cache import Cache
    from engine.people import pattern as PT
    from engine.people.contacts import PeopleError, company

    conn = _people_conn(db_file)
    try:
        try:
            c = company(conn, company_id)
            found, missing = PT.public_addresses(conn, Cache(cache_dir), company_id)
        except PeopleError as e:
            _fail(str(e))
            return
    finally:
        conn.close()
    click.echo(f"{company_id}  domain {c.domain or '-'}")
    if missing:
        click.echo(f"ok artifacts not yet extracted (not read): {missing}")
    if not found:
        click.echo("no address at this domain in the captured pages")
    for a in found:
        label = "role" if a.role else (a.shape or "unreadable")
        click.echo(f"  {a.address:<40} {label:<16} {a.kind} {a.url}")
    prop = PT.propose(found)
    click.echo(f"pattern: {prop.pattern or 'none'} ({prop.note})")
    if first is None and last is None:
        if verify:
            _fail("--verify needs --first and --last")
        return
    needs_last = prop.pattern is not None and ("{last}" in prop.pattern or "{l}" in prop.pattern)
    if prop.pattern is None or not first or (needs_last and not last) or not c.domain:
        _fail("cannot build an address: no pattern, or the name parts it needs are missing")
        return
    try:
        addr = PT.render(prop.pattern, first, last or "", c.domain)
    except ValueError as e:
        _fail(str(e))
        return
    level = PT.INFERRED_CONFIDENCE
    flag = ""
    if verify:
        try:
            v = F.hunter_verify(addr)
        except (F.NotConfigured, F.FinderError) as e:
            click.echo(f"verifier: {e}; the address stays {level}")
        else:
            if v.confidence is None:
                _fail(f"verifier: Hunter says {v.status}; do not use this address")
                return
            click.echo(f"verifier: Hunter says {v.status}")
            if v.confidence != level:
                level, flag = v.confidence, " --verified"
    click.echo(f"inferred address: {addr} (confidence {level})")
    click.echo(f"to record it: te people add {company_id} ... --email <the address> "
               f"--confidence {level} --email-source pattern{flag}")


# --- gold set (Steps 44-45, Gate 4.1) ----------------------------------------------


@main.group("gold")
def gold_group() -> None:
    """The gold set: hand-written diagnoses, the teach/held_out split, Gate 4.1.

    The person writes each diagnosis; these commands check quotes against the
    captured bundle and store them. held_out rows are never shown to the judge,
    in a prompt, a skill, a fixture or a chat.
    """


def _gold_conn(db_file: Path | None):
    from engine import db as dbmod

    try:
        return dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))


@gold_group.command("add")
@click.argument("company_id")
@click.option("--gap", type=click.Choice(["G1", "G2", "G3", "G4", "G5", "G6"]), default=None,
              help="The gap the pages show.")
@click.option("--abstain", "abstain_reason", default=None, metavar="REASON",
              help='Abstain instead; starts "no_evidence:", "contradicted:" or "thin:".')
@click.option("--quote", "quotes", type=(str, str), multiple=True, metavar="KIND TEXT",
              help="A verbatim quote from the company's KIND artifact. Repeatable.")
@click.option("--not-found", "not_found", multiple=True, metavar="KIND",
              help="KIND's capture outcome is not_found (an absence). Repeatable.")
@click.option("--split", type=click.Choice(["teach", "held_out"]), default=None,
              help="Default: the company's existing split, else held_out.")
@click.option("--gaps-version", default=None,
              help="GAPS.md version the diagnosis was written under (default: the file's, "
                   "e.g. v1.2).")
@click.option("--note", default=None, help="A ruling or doubt, in your words.")
@click.option("--replace", is_flag=True, help="Overwrite a different row at this gaps version.")
@click.option("--dry-run", is_flag=True, help="Check and print; write nothing.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def gold_add(company_id: str, gap: str | None, abstain_reason: str | None,
             quotes: tuple[tuple[str, str], ...], not_found: tuple[str, ...], split: str | None,
             gaps_version: str | None, note: str | None, replace: bool, dry_run: bool,
             cache_dir: Path | None, db_file: Path | None) -> None:
    """Record COMPANY_ID's gold diagnosis (Step 45). Each quote is checked against
    the captured artifact; a gap needs evidence from two kinds with one found quote.
    Quotes are not echoed."""
    from engine.guard.cache import Cache
    from engine.judge import gold as G

    conn = _gold_conn(db_file)
    try:
        try:
            plan = G.plan_add(conn, Cache(cache_dir), company_id, gap=gap,
                              abstain_reason=abstain_reason, quotes=list(quotes),
                              not_found=list(not_found), split=split,
                              gaps_version=gaps_version, note=note)
        except G.GoldError as e:
            _fail(f"refused: {company_id}: {e}")
            return
        for w in plan.warnings:
            click.echo(f"warning: {w}")
        r = plan.row
        kinds = sorted({e.kind for e in r.evidence})
        what = (f"{r.split}; {r.gaps_version}; evidence {len(r.evidence)} "
                f"({sum(e.found for e in r.evidence)} found) from {', '.join(kinds) or 'none'}")
        if dry_run:
            verb = ("unchanged" if plan.unchanged else
                    "would replace" if plan.existing is not None else "would add")
            click.echo(f"dry run: {company_id}: {verb}: {what}")
            return
        try:
            done = G.write(conn, plan, replace=replace)
        except G.GoldError as e:
            _fail(f"refused: {e}")
            return
    finally:
        conn.close()
    click.echo(f"{company_id}: {done}: {what}")


@gold_group.command("split")
@click.option("--teach", "teach", nargs=5, required=True, metavar="ID ID ID ID ID",
              help="Exactly five company_ids: both segments, three gap types, one abstention.")
@click.option("--force", is_flag=True, help="Replace a different teach five already recorded.")
@click.option("--dry-run", is_flag=True, help="Check and print; write nothing.")
@_db_option
def gold_split(teach: tuple[str, ...], force: bool, dry_run: bool, db_file: Path | None) -> None:
    """Mark five companies teach and every other gold company held_out (Step 45)."""
    from engine.judge import gold as G

    conn = _gold_conn(db_file)
    try:
        try:
            plan = G.plan_split(conn, list(teach), force=force)
        except G.GoldError as e:
            _fail(f"refused: {e}")
            return
        if not dry_run:
            G.write_split(conn, plan)
    finally:
        conn.close()
    prefix = "dry run: would set" if dry_run else "set"
    click.echo(f"{prefix} teach {len(plan.teach)}: {', '.join(plan.teach)}")
    click.echo(f"{prefix} held_out {len(plan.held_out)}; gold rows changed {plan.changed}")
    click.echo("from here on, held-out diagnoses never go into a prompt, a skill or a chat")


@gold_group.command("list")
@click.option("--reveal-held-out", is_flag=True,
              help="Also print held-out gaps and reasons: to a terminal only.")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many.")
@click.option("--pilot", is_flag=True, help="Only pilot companies.")
@click.option("--dry-run", is_flag=True, help="Read-only anyway; accepted for convention.")
@_db_option
def gold_list(reveal_held_out: bool, company_id: str | None, limit: int | None, pilot: bool,
              dry_run: bool, db_file: Path | None) -> None:
    """List each gold company's split, and for teach rows its gap or abstention.
    Held-out rows show only company and split unless --reveal-held-out."""
    from engine.judge import gold as G

    if reveal_held_out:
        if not _stdout_is_terminal():
            _fail("refused: --reveal-held-out prints held-out diagnoses and only to a "
                  "terminal; stdout is redirected to a file or a pipe")
            return
        click.echo("WARNING: held-out diagnoses below. Never paste them into a prompt, a "
                   "skill, a fixture or a chat; they exist only to score the judge.")
    conn = _gold_conn(db_file)
    try:
        rows = G.current_rows(conn)
        pilots = {r[0] for r in conn.execute("SELECT company_id FROM companies WHERE pilot = 1")}
    finally:
        conn.close()
    ids = sorted(c for c in rows if (company_id is None or c == company_id)
                 and (not pilot or c in pilots))
    if limit is not None:
        ids = ids[:limit]
    click.echo(f"{'company':<30} {'split':<9} {'version':<8} {'segment':<14} call")
    for c in ids:
        r = rows[c]
        if r["split"] == "held_out" and not reveal_held_out:
            click.echo(f"{c[:30]:<30} {r['split']:<9}")
            continue
        call = r["gap_type"] or f"abstain ({r['abstain_reason']})"
        n = len(json.loads(r["evidence"]))
        click.echo(f"{c[:30]:<30} {r['split']:<9} {r['gaps_version']:<8} "
                   f"{r['segment'] or '-':<14} {call}; evidence {n}")
    counts = {s: sum(1 for c in ids if rows[c]["split"] == s) for s in ("teach", "held_out")}
    click.echo(f"companies {len(ids)}: teach {counts['teach']}, held_out {counts['held_out']}")


@gold_group.command("check")
@_db_option
def gold_check(db_file: Path | None) -> None:
    """Gate 4.1: eighteen gold rows, the split recorded (5 teach, 13 held_out),
    GAPS.md at v2. Exit 1 if any fails."""
    from engine.judge import gold as G

    conn = _gold_conn(db_file)
    try:
        lines, warnings = G.gate(conn)
    finally:
        conn.close()
    for line in lines:
        click.echo(f"{'ok' if line.ok else 'FAIL':<4} {line.text}")
    for w in warnings:
        click.echo(f"warning: {w}")
    ok = all(line.ok for line in lines)
    click.echo("Gate 4.1 " + ("passes" if ok else "fails"))
    sys.exit(0 if ok else 1)


@gold_group.command("export-teach")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the JSON here instead of stdout.")
@_db_option
def gold_export_teach(out_path: Path | None, db_file: Path | None) -> None:
    """Print the five teach diagnoses as JSON for the judge prompt. Refuses when the
    split is not recorded; never includes a held_out row."""
    from engine.judge import gold as G

    conn = _gold_conn(db_file)
    try:
        try:
            rows = G.teach_export(conn)
        except G.GoldError as e:
            _fail(f"refused: {e}")
            return
    finally:
        conn.close()
    text = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8", newline="\n")
        click.echo(f"wrote {len(rows)} teach diagnoses to {out_path}")
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # a Windows pipe is cp1252; quotes may not be
        reconfigure(errors="backslashreplace")
    click.echo(text, nl=False)


def _judge_options(f):
    for opt in reversed((
        click.option("--company", "company_id", default=None, help="Only this company_id."),
        click.option("--pilot", is_flag=True, help="Only the pilot and hand sets."),
        click.option("--limit", type=click.IntRange(min=1), default=None,
                     help="At most this many companies."),
        click.option("--no-examples", is_flag=True,
                     help="Dry test without the teach examples (no gold split yet): keyed on "
                          "the prompt version + '+noex' and never moves a status."),
        click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path),
                     default=None, help="Cache directory (default: $TE_CACHE_DIR or data/cache)."),
        _db_option,
    )):
        f = opt(f)
    return f


def _faithfulness_lines(f) -> list[str]:
    return [f"  failed claim {x.index} [{x.artifact_kind}]: {x.category}"
            + (f" (score {x.score})" if x.score is not None else "") for x in f.failed]


@main.group("judge", invoke_without_command=True)
@_judge_options
@click.option("--dry-run", is_flag=True,
              help="Print each prompt's size and the projected calls; no call, no write.")
@click.pass_context
def judge_cmd(ctx: click.Context, company_id: str | None, pilot: bool, limit: int | None,
              no_examples: bool, cache_dir: Path | None, db_file: Path | None,
              dry_run: bool) -> None:
    """Name the gap each company's pages show, or abstain (E5, Steps 46-48).

    One model call per company through the gateway, keyed on (judge, company_id,
    prompt_version): a re-run makes no call. The faithfulness check runs on every
    result before it is stored. A named gap moves extracted -> diagnosed; an
    abstention drops the company as no_gap. Refuses to run without the teach
    split unless --no-examples. Exits 1 on any scoring_failed, refused or error.
    """
    if ctx.invoked_subcommand is not None:
        return
    from engine import db as dbmod
    from engine.guard.budget import BudgetExceeded
    from engine.guard.cache import Cache
    from engine.judge import judge as J

    problem = J.table_problem()
    if problem:
        _fail(f"refused: {problem}")
        return
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    cache = Cache(cache_dir)
    bad = 0
    counts: dict[str, int] = {}
    calls = 0
    try:
        try:
            ids = J.select(conn, company_id=company_id, pilot=pilot, limit=limit)
        except J.JudgeError as e:
            _fail(f"refused: {e}")
            return
        examples = None
        if not no_examples:
            try:
                examples = J.teach_examples(conn)
            except J.JudgeError as e:
                if not dry_run:
                    _fail(f"refused: {e}")
                    return
                click.echo(f"warning: a real run would refuse: {e}")
        version = J.version_for(examples is None)
        if dry_run:
            d = J.dry_run(conn, cache, ids, examples=examples)
            for r in d.rows:
                what = "would call" if r.would_call else f"no call: {r.reason}"
                masked = f", {r.masked} contact values masked" if r.masked else ""
                click.echo(f"{r.company_id}: prompt {r.chars} chars (~{r.chars // 4} tokens)"
                           f"{masked}; {what}")
            biggest = max((r.chars for r in d.rows), default=0)
            click.echo(f"dry run {version}: companies {len(d.rows)}; projected calls {d.calls}; "
                       f"largest prompt {biggest} chars (~{biggest // 4} tokens)")
            return
        try:
            for cid in ids:
                r = J.judge_company(conn, cache, cid, examples=examples)
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
                calls += r.calls
                line = f"{cid}: {r.outcome}: {r.detail[:160]}"
                f = r.diagnosis.faithfulness if r.diagnosis is not None else None
                if f is not None:
                    line += f"; claims {f.passed}/{f.checked} passed"
                    if f.near_miss:
                        line += f", near miss {f.near_miss}"
                if r.masked:
                    line += f"; {r.masked} contact values masked"
                if r.status:
                    line += f"; {r.status}"
                click.echo(line)
                if f is not None:
                    for x in _faithfulness_lines(f):
                        click.echo(x)
                if r.outcome in ("scoring_failed", "refused", "error"):
                    bad += 1
        except BudgetExceeded as e:
            click.echo(f"stopped: {e}")
            bad += 1
    finally:
        conn.close()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    click.echo(f"judge {version}: companies {sum(counts.values())}: {summary or 'none'}; "
               f"provider calls {calls}")
    sys.exit(1 if bad else 0)


@judge_cmd.command("faithfulness")
@_judge_options
@click.option("--dry-run", is_flag=True, help="Check and print; write nothing.")
def judge_faithfulness(company_id: str | None, pilot: bool, limit: int | None,
                       no_examples: bool, cache_dir: Path | None, db_file: Path | None,
                       dry_run: bool) -> None:
    """Re-run the faithfulness check on stored diagnoses (Step 48), from the judge's
    original output. Updates claims and faithfulness; an abstention drops as no_gap."""
    from engine import db as dbmod
    from engine.guard.cache import Cache
    from engine.judge import judge as J

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    version = J.version_for(no_examples)
    cache = Cache(cache_dir)
    n = 0
    try:
        try:
            if company_id is not None:
                if J.stored(conn, company_id, version) is None:
                    _fail(f"{company_id}: no diagnosis at {version}")
                    return
                ids = [company_id]
            else:
                ids = [c for c in J.select(conn, pilot=pilot) if J.stored(conn, c, version)]
                ids = ids[:limit] if limit is not None else ids
            for cid in ids:
                r = J.refaithful(conn, cache, cid, prompt_version=version, dry_run=dry_run)
                f = r.diagnosis.faithfulness if r.diagnosis is not None else None
                line = f"{cid}: {r.outcome}: {r.detail[:160]}"
                if f is not None:
                    line += f"; claims {f.passed}/{f.checked} passed, near miss {f.near_miss}"
                if r.status:
                    line += f"; {r.status}"
                click.echo(("dry run: " if dry_run else "") + line)
                if f is not None:
                    for x in _faithfulness_lines(f):
                        click.echo(x)
                n += 1
        except J.JudgeError as e:
            _fail(f"refused: {e}")
            return
    finally:
        conn.close()
    click.echo(f"faithfulness {version}: diagnoses checked {n}")


@judge_cmd.command("verify")
@_judge_options
@click.option("--dry-run", is_flag=True,
              help="Print each diagnosis's claims, writer and prompt sizes; no call, no write.")
def judge_verify(company_id: str | None, pilot: bool, limit: int | None, no_examples: bool,
                 cache_dir: Path | None, db_file: Path | None, dry_run: bool) -> None:
    """Check each surviving claim with a model of another family (Step 49).

    One call per claim on the verifier route, never the family that wrote the
    diagnosis, keyed on (verify, company_id, v-1.0:<diagnosis key>:<claim>): a
    re-run makes no call. Two not_supported turn the diagnosis into an abstention
    (dropped as no_gap); one is recorded so the quality gate fails the teardown.
    Exits 1 on any refused or error.
    """
    from engine import db as dbmod
    from engine.guard.budget import BudgetExceeded
    from engine.guard.cache import Cache
    from engine.judge import judge as J
    from engine.judge import verify as V

    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    version = J.version_for(no_examples)
    cache = Cache(cache_dir)
    counts: dict[str, int] = {}
    calls = bad = 0
    try:
        if company_id is not None and J.stored(conn, company_id, version) is None:
            _fail(f"{company_id}: no diagnosis at {version}")
            return
        ids = V.select(conn, prompt_version=version, company_id=company_id, pilot=pilot,
                       limit=limit)
        try:
            for cid in ids:
                r = V.verify_company(conn, cache, cid, prompt_version=version, dry_run=dry_run)
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
                calls += r.calls
                line = f"{cid}: {r.outcome}: {r.detail[:200]}"
                if r.prompts:
                    line += f"; prompts {', '.join(str(n) for n in r.prompts)} chars"
                if r.masked:
                    line += f"; {r.masked} contact values masked"
                if r.status:
                    line += f"; {r.status}"
                click.echo(("dry run: " if dry_run else "") + line)
                if r.verdict is not None and r.outcome != "existing":
                    for c in r.verdict.claims:
                        click.echo(f"  claim {c.index} [{c.artifact_kind}]: {c.verdict} "
                                   f"({c.provider}, {c.family})")
                if r.outcome in ("refused", "error"):
                    bad += 1
        except BudgetExceeded as e:
            click.echo(f"stopped: {e}")
            bad += 1
        except V.VerifyError as e:
            _fail(f"refused: {e}")
            return
    finally:
        conn.close()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    click.echo(f"verify {V.VERIFY_VERSION} on {version}: diagnoses {sum(counts.values())}: "
               f"{summary or 'none'}; provider calls {calls}")
    sys.exit(1 if bad else 0)


@judge_cmd.command("score")
@click.option("--reveal-held-out", is_flag=True,
              help="Also print each held-out row's human and judge call: to a terminal only.")
@click.option("--dry-run", is_flag=True,
              help="Score the diagnoses already stored; no call, nothing recorded.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def judge_score(reveal_held_out: bool, dry_run: bool, cache_dir: Path | None,
                db_file: Path | None) -> None:
    """Score the judge on the held-out gold rows (Step 50, Gate 4.2).

    Runs the judge (or reuses its stored diagnoses and cached calls) on all
    eighteen gold companies, then reports for the held-out rows only: same call
    as the human (target 10 of 13), gaps named where the human abstained (1 at
    most), and claims failing faithfulness that would reach a draft (zero).
    Records the round. Exits 0 only when every target is met. Always scores all
    the gold companies, so --company, --pilot and --limit do not apply.
    """
    from engine import db as dbmod
    from engine.guard.budget import BudgetExceeded
    from engine.guard.cache import Cache
    from engine.judge import score as S

    if reveal_held_out:
        if not _stdout_is_terminal():
            _fail("refused: --reveal-held-out prints held-out calls and only to a terminal; "
                  "stdout is redirected to a file or a pipe")
            return
        click.echo("WARNING: held-out calls below. Never paste them into a prompt, a skill, "
                   "a fixture or a chat; they exist only to score the judge.")
    try:
        conn = dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))
        return
    try:
        try:
            score = S.run(conn, Cache(cache_dir), dry_run=dry_run)
        except S.ScoreError as e:
            _fail(f"refused: {e}")
            return
        except BudgetExceeded as e:
            _fail(f"stopped: {e}")
            return
        for r in score.rows:
            if not r.judged:
                what = f"not judged ({r.note})"
            else:
                what = "agree" if r.same else "disagree"
            if reveal_held_out and r.judged:
                human = r.human_gap or "abstain"
                judge = r.judge_gap or "abstain"
                what += f": human {human}, judge {judge}"
            if r.faithfulness_failures:
                what += f"; {r.faithfulness_failures} claims fail faithfulness"
            click.echo(f"{r.company_id}: {what}")
        for t in score.teach_problems:
            click.echo(f"warning: teach company not judged: {t}")
        for ok, text in score.checks():
            click.echo(f"{'ok' if ok else 'FAIL':<4} {text}")
        n = S.round_number(conn, score.prompt_version)
        warning = S.round_warning(conn, score.prompt_version)
        if warning:
            click.echo(f"warning: {warning}")
        if dry_run:
            click.echo(f"dry run {score.prompt_version}: round {n}; nothing recorded")
        else:
            wrote = S.record(conn, score)
            click.echo(f"judge {score.prompt_version}: round {n} of at most {S.MAX_ROUNDS}; "
                       f"judged now {score.judged_now}; provider calls {score.calls}; "
                       + ("recorded" if wrote else "same counts as last recorded; not recorded"
                          " again"))
    finally:
        conn.close()
    click.echo(f"score: {score.same_call} of {score.held_out} held-out rows the same call; "
               "Gate 4.2 targets " + ("met" if score.passed else "not met"))
    sys.exit(0 if score.passed else 1)


# --- teardown (Steps 52-53) ----------------------------------------------------------

_teardowns_option = click.option(
    "--dir", "teardowns_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Teardown files folder (default: $TE_TEARDOWNS_DIR or data/teardowns).")
_sender_option = click.option(
    "--sender", "sender_file", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Sender file (default: $TE_SENDER or data/sender.yaml).")


def _conn_or_fail(db_file: Path | None):
    from engine import db as dbmod

    try:
        return dbmod.connect(db_file)
    except dbmod.DatabaseError as e:
        _fail(str(e))


@main.group("teardown")
def teardown_group() -> None:
    """Pilot teardowns: write (init), check and render the page."""


@teardown_group.command("init-sender")
@_sender_option
@click.option("--force", is_flag=True, help="Overwrite an existing sender file.")
def teardown_init_sender(sender_file: Path | None, force: bool) -> None:
    """Write a sender.yaml skeleton (name, identity, section 8, postal address)."""
    from engine.build import sender as S

    try:
        p = S.init(sender_file, force=force)
    except S.SenderError as e:
        _fail(f"refused: {e}")
        return
    click.echo(f"wrote {p}; fill every TODO")


@teardown_group.command("init")
@click.argument("company_id")
@_teardowns_option
@click.option("--force", is_flag=True, help="Overwrite an existing teardown file.")
@_db_option
def teardown_init(company_id: str, teardowns_dir: Path | None, force: bool,
                  db_file: Path | None) -> None:
    """Write data/teardowns/COMPANY_ID.yaml from the stored diagnosis."""
    from engine.build import teardown as T

    conn = _conn_or_fail(db_file)
    try:
        p = T.init(conn, company_id, directory=teardowns_dir, force=force)
    except T.TeardownError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    click.echo(f"wrote {p}; write sections 3, 4, 5, 6 and 7, then te teardown check "
               f"{company_id}")


def _echo_check(chk) -> None:
    for f in chk.failures:
        click.echo(f"{chk.company_id}: {f}")
    click.echo(f"{chk.company_id}: " + ("ok" if chk.ok else f"{len(chk.failures)} failure(s)"))


@teardown_group.command("check")
@click.argument("company_id", required=False)
@click.option("--company", "company_opt", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every pilot company with a teardown file.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@_teardowns_option
@_sender_option
@_db_option
def teardown_check(company_id: str | None, company_opt: str | None, pilot: bool,
                   limit: int | None, teardowns_dir: Path | None, sender_file: Path | None,
                   db_file: Path | None) -> None:
    """Check teardown files against TEARDOWN_SPEC; exit 1 on any failure."""
    from engine.build import render as R
    from engine.build import teardown as T

    cid = company_id or company_opt
    if not cid and not pilot:
        _fail("give a company_id, --company or --pilot")
        return
    conn = _conn_or_fail(db_file)
    bad = 0
    try:
        ids = R.select(conn, company_id=cid, pilot=pilot, limit=limit)
        if pilot and not cid:
            ids = [i for i in ids if T.file_for(i, teardowns_dir).exists()]
        for i in ids:
            try:
                chk = T.check(conn, i, directory=teardowns_dir, sender_file=sender_file)
            except T.TeardownError as e:
                click.echo(f"{i}: error: {e}")
                bad += 1
                continue
            _echo_check(chk)
            bad += not chk.ok
    finally:
        conn.close()
    sys.exit(1 if bad else 0)


@teardown_group.command("render")
@click.argument("company_id", required=False)
@click.option("--company", "company_opt", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every pilot company (not dropped).")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--dry-run", is_flag=True, help="Check and render in memory; write nothing.")
@_teardowns_option
@_sender_option
@click.option("--site-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Output folder (default: $TE_SITE_DIR or site/).")
@_db_option
def teardown_render(company_id: str | None, company_opt: str | None, pilot: bool,
                    limit: int | None, dry_run: bool, teardowns_dir: Path | None,
                    sender_file: Path | None, site_dir: Path | None,
                    db_file: Path | None) -> None:
    """Render site/<slug>/index.html for each teardown that passes the check."""
    from engine.build import render as R
    from engine.build import teardown as T

    cid = company_id or company_opt
    if not cid and not pilot:
        _fail("give a company_id, --company or --pilot")
        return
    conn = _conn_or_fail(db_file)
    bad = 0
    try:
        for i in R.select(conn, company_id=cid, pilot=pilot, limit=limit):
            if not cid and not T.file_for(i, teardowns_dir).exists():
                click.echo(f"{i}: skipped: no teardown file (te teardown init {i})")
                continue
            r = R.render_company(conn, i, teardowns=teardowns_dir, sender_file=sender_file,
                                 site=site_dir, dry_run=dry_run)
            for f in r.failures:
                click.echo(f"{i}: {f}")
            if r.outcome in ("refused", "error"):
                bad += 1
                click.echo(f"{i}: {r.outcome}: {r.detail}")
                continue
            where = str(r.path) if r.path else "(no slug yet; a real render generates one)"
            click.echo(f"{i}: {r.outcome} {where}")
            if r.url:
                click.echo(f"{i}: url {r.url}")
            elif r.slug:
                click.echo(f"{i}: url not known: set {R.BASE_URL_ENV_VAR} in .env; the page "
                           f"will live at <base>/{r.slug}/")
    finally:
        conn.close()
    sys.exit(1 if bad else 0)


# --- slice (Steps 60-63) -------------------------------------------------------------

_cache_option = click.option(
    "--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Cache directory (default: $TE_CACHE_DIR or data/cache).")


@main.group("slice")
def slice_group() -> None:
    """Proof slices (Steps 60-63): build, show, and confirm competitors."""


@slice_group.command("build")
@click.argument("company_id", required=False)
@click.option("--company", "company_opt", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every pilot company with a diagnosis.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--dry-run", is_flag=True, help="Print what would be built; no request, no write.")
@click.option("--rebuild", is_flag=True,
              help="Rebuild a network slice even when stored with the same inputs.")
@_cache_option
@_db_option
def slice_build(company_id: str | None, company_opt: str | None, pilot: bool, limit: int | None,
                dry_run: bool, rebuild: bool, cache_dir: Path | None,
                db_file: Path | None) -> None:
    """Build each company's proof slice: the gap's primary slice, else role_funnel.

    A row without a source URL and a quote is dropped before storage. When both
    slices fail, the company is held for review (slice_thin). No model calls.
    """
    from engine.build.slices import run as SR
    from engine.guard.cache import Cache
    from engine.guard.http import GuardConfigError

    conn = _conn_or_fail(db_file)
    try:
        try:
            ids = SR.select_companies(conn, company_id=company_id or company_opt, pilot=pilot,
                                      limit=limit)
        except SR.SliceError as e:
            _fail(str(e))
            return
        rt = SR.default_runtime(Cache(cache_dir))
        try:
            rep = asyncio.run(SR.run(conn, rt, ids, rebuild=rebuild, dry_run=dry_run))
        except GuardConfigError as e:
            _fail(str(e))
            return
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for r in rep.results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
        head = f"{r.company_id}: {r.outcome}"
        if r.gap:
            head += f" (gap {r.gap}, primary {r.primary}"
            head += f", selected {r.selected})" if r.selected else ")"
        click.echo(head)
        for line in r.lines:
            click.echo(f"  {line}")
    click.echo("slices: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
               + f"; model calls {rep.model_calls}" + ("; dry run, nothing written" if dry_run
                                                        else ""))
    if any(r.outcome == "refused" for r in rep.results) and (company_id or company_opt):
        sys.exit(1)


@slice_group.command("show")
@click.argument("company_id")
@_db_option
def slice_show(company_id: str, db_file: Path | None) -> None:
    """Print the stored slices, their rows with sources, and dropped rows."""
    from engine.build.slices import store as S

    conn = _conn_or_fail(db_file)
    try:
        stored = S.slices(conn, company_id)
        comps = S.competitors(conn, company_id)
    finally:
        conn.close()
    if not stored and not comps:
        click.echo(f"{company_id}: no slice stored; run te slice build {company_id}")
        return
    for st in stored:
        mark = " [selected]" if st.selected else ""
        state = f"ok, {len(st.rows)} rows" if st.ok else f"failed: {st.reason}"
        click.echo(f"{st.kind} {st.version}{mark}: {state}; built {st.built_at}; "
                   f"{st.fetches} fetches, {st.searches} searches")
        click.echo(f"  {st.title}")
        click.echo("  columns: " + " | ".join(st.columns))
        for i, row in enumerate(st.rows, start=1):
            click.echo(f"  [{i}] " + " | ".join(row["cells"]))
            click.echo(f"      url: {row['url']}")
            click.echo(f"      quote: {json.dumps(row['quote'], ensure_ascii=False)}")
        for d in st.dropped:
            click.echo(f"  dropped: {' | '.join(d.get('cells') or [])}: {d.get('why')}")
    if comps:
        click.echo("competitors:")
        for c in comps:
            click.echo(f"  {c.rank}. {c.name} {c.domain or '(no domain)'} [{c.source}] "
                       + ("confirmed " + (c.confirmed_at or "") if c.confirmed
                          else "not confirmed"))


@slice_group.command("competitors")
@click.argument("company_id")
@click.option("--set", "sets", multiple=True, metavar="NAME=DOMAIN",
              help="Add a competitor, or give one its domain. Repeatable.")
@click.option("--yes", is_flag=True, help="Confirm without asking (for scripts).")
@click.option("--dry-run", is_flag=True, help="Show the choice; no search, no write.")
@_cache_option
@_db_option
def slice_competitors(company_id: str, sets: tuple[str, ...], yes: bool, dry_run: bool,
                      cache_dir: Path | None, db_file: Path | None) -> None:
    """Show the two competitors funnel_compare would compare against and wait for
    your yes. Nothing about a competitor is fetched before it."""
    from engine.build.slices import run as SR
    from engine.guard.cache import Cache

    conn = _conn_or_fail(db_file)
    try:
        rt = SR.default_runtime(Cache(cache_dir))
        try:
            ch = asyncio.run(SR.competitors(conn, rt, company_id, sets=sets, dry_run=dry_run))
        except SR.SliceError as e:
            _fail(str(e))
            return
        click.echo(f"{company_id}: competitors ({ch.note})")
        for c in ch.competitors:
            click.echo(f"  {c.rank}. {c.name}  {c.domain or '(no domain)'}  [{c.source}]"
                       + ("  confirmed" if c.confirmed else ""))
            if c.evidence_quote:
                click.echo(f"     quote: {json.dumps(c.evidence_quote, ensure_ascii=False)}")
            if c.evidence_url:
                click.echo(f"     from: {c.evidence_url}")
        if not ch.ready:
            _fail(f"not ready: {ch.why}")
            return
        if dry_run:
            click.echo("dry run: nothing confirmed, nothing written")
            return
        if all(c.confirmed for c in ch.competitors[:2]):
            click.echo("already confirmed; te slice build " + company_id)
            return
        if not yes and not click.confirm("Compare against these two? Nothing is fetched "
                                         "before you say yes", default=False):
            click.echo("not confirmed; nothing captured")
            sys.exit(1)
        n = SR.confirm(conn, company_id)
        click.echo(f"confirmed {n}; now te slice build {company_id}")
    finally:
        conn.close()


@main.group("queue")
def queue_group() -> None:
    """The send queue: open, report, approve, applied, draft (Gmail drafts only), sent,
    done, followups, followup-sent, bounce, resume. Nothing is sent."""


_site_option = click.option(
    "--site-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Rendered pages folder (default: $TE_SITE_DIR or site/).")


def _echo_result(r) -> None:
    for w in r.warnings:
        click.echo(f"{r.company_id}: warning: {w}")
    head = f"{r.company_id}: {r.outcome}"
    click.echo(head + (f": {r.detail}" if r.detail else ""))
    for line in r.lines:
        click.echo(f"    {line}" if line else "")


@queue_group.command("approve")
@click.argument("company_id")
@click.option("--dry-run", is_flag=True, help="Run the checks and print; write nothing.")
@_teardowns_option
@_sender_option
@_site_option
@_db_option
def queue_approve(company_id: str, dry_run: bool, teardowns_dir: Path | None,
                  sender_file: Path | None, site_dir: Path | None, db_file: Path | None) -> None:
    """After reading the page: move COMPANY_ID built -> approved (prints the email)."""
    from engine.outreach import draft as Q

    conn = _conn_or_fail(db_file)
    try:
        r = Q.approve(conn, company_id, teardowns=teardowns_dir, sender_file=sender_file,
                      site=site_dir, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


@queue_group.command("draft")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every approved pilot company.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--dry-run", is_flag=True,
              help="Local checks only: no request, no draft, no write.")
@click.option("--role-checked", is_flag=True,
              help="A person saw the post open today (for a signal no collector re-polls).")
@_teardowns_option
@_sender_option
@_site_option
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Cache directory (default: $TE_CACHE_DIR or data/cache).")
@_db_option
def queue_draft(company_id: str | None, pilot: bool, limit: int | None, dry_run: bool,
                role_checked: bool, teardowns_dir: Path | None, sender_file: Path | None,
                site_dir: Path | None, cache_dir: Path | None, db_file: Path | None) -> None:
    """Recheck on send day, then make each approved company's touch-1 Gmail draft."""
    from engine.guard.cache import Cache
    from engine.outreach import draft as Q

    if not company_id and not pilot:
        _fail("give --company or --pilot")
        return
    conn = _conn_or_fail(db_file)
    cache = Cache(cache_dir)
    bad = 0
    try:
        ids = Q.select(conn, company_id=company_id, pilot=pilot, limit=limit)
        if not ids:
            click.echo("nothing approved to draft (te queue approve <company_id>)")
        today = datetime.now(UTC).date()
        w = Q.pace_warning(conn, ids, pilot=pilot, day=today)
        if w:
            click.echo(f"warning: {w}")

        async def go() -> list:
            checks = None if dry_run else Q.LiveChecks(cache)
            drafter = None if dry_run else Q.default_drafter()
            out = []
            try:
                for i in ids:
                    out.append(await Q.draft_company(
                        conn, i, checks=checks, cache=cache, drafter=drafter,
                        teardowns=teardowns_dir, sender_file=sender_file, site=site_dir,
                        dry_run=dry_run, role_checked=role_checked, today=today))
            finally:
                if checks is not None:
                    await checks.aclose()
            return out

        for r in asyncio.run(go()):
            _echo_result(r)
            bad += r.outcome == "refused"
    finally:
        conn.close()
    sys.exit(1 if bad else 0)


@queue_group.command("sent")
@click.argument("company_id")
@click.option("--at", "at", default=None, metavar="ISO",
              help="When it was sent (ISO 8601, UTC unless an offset is given); default now.")
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_sent(company_id: str, at: str | None, dry_run: bool, db_file: Path | None) -> None:
    """A person sent touch 1 by hand: record sent_at and move the company to sent."""
    from engine.outreach import draft as Q

    when = None
    if at is not None:
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            _fail(f"--at {at!r} is not an ISO 8601 time")
            return
    conn = _conn_or_fail(db_file)
    try:
        r = Q.sent(conn, company_id, at=when, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


@queue_group.command("done")
@click.argument("company_id")
@click.option("--minutes", type=click.FloatRange(min=0, min_open=True, max=600), required=True,
              help="Minutes the review of this teardown took (Step 55).")
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_done(company_id: str, minutes: float, dry_run: bool, db_file: Path | None) -> None:
    """Record the review minutes for COMPANY_ID (teardowns.review_minutes)."""
    from engine.outreach import draft as Q

    conn = _conn_or_fail(db_file)
    try:
        r = Q.done(conn, company_id, minutes, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


def _parse_day(value: str | None, flag: str):
    from datetime import date as _date

    if value is None:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        _fail(f"{flag} {value!r} is not a date (YYYY-MM-DD)")


@queue_group.command("applied")
@click.argument("company_id")
@click.option("--on", "on", default=None, metavar="YYYY-MM-DD",
              help="The day the application went in through the company's own form "
                   "(default today, UTC).")
@click.option("--note", default=None,
              help="Free text for your record. Never a person's name or contact.")
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_applied(company_id: str, on: str | None, note: str | None, dry_run: bool,
                  db_file: Path | None) -> None:
    """Step 75: record that you applied for COMPANY_ID's candidate role (unblocks the draft)."""
    from engine.outreach import applied as A
    from engine.outreach import draft as Q

    day = _parse_day(on, "--on")
    conn = _conn_or_fail(db_file)
    try:
        r = A.applied(conn, company_id, on=day, note=note, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


@queue_group.command("open")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Only pilot companies.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--port", type=click.IntRange(min=0, max=65535), default=None,
              help="Port on 127.0.0.1 (default 8765; 0 picks a free one).")
@click.option("--no-browser", is_flag=True, help="Print the URL; do not open a browser.")
@click.option("--dry-run", is_flag=True, help="List what would be served; start no server.")
@_teardowns_option
@_sender_option
@_site_option
@_db_option
def queue_open(company_id: str | None, pilot: bool, limit: int | None, port: int | None,
               no_browser: bool, dry_run: bool, teardowns_dir: Path | None,
               sender_file: Path | None, site_dir: Path | None, db_file: Path | None) -> None:
    """Step 66: serve the review queue on 127.0.0.1 (approve, kill with a reason, edited)."""
    from engine import db as dbmod
    from engine.outreach import review as RV

    conn = _conn_or_fail(db_file)
    path = dbmod.db_path(db_file)
    app = RV.QueueApp(lambda: dbmod.connect(path), pilot=pilot, company_id=company_id,
                      limit=limit, teardowns=teardowns_dir, sender_file=sender_file,
                      site=site_dir)
    try:
        ids = [c["company_id"] for c in app.companies(conn)]
    finally:
        conn.close()
    if dry_run:
        click.echo(f"would serve {len(ids)} teardown(s) on {RV.HOST}: " + (", ".join(ids) or "-"))
        return
    try:
        httpd = RV.make_server(app, RV.DEFAULT_PORT if port is None else port)
    except OSError as e:
        _fail(f"cannot listen on {RV.HOST}:{port}: {e}")
        return
    click.echo(f"review queue: {len(ids)} teardown(s); local only, nothing is sent")
    click.echo(f"open {app.url()}  (Ctrl-C to stop)")
    if not no_browser:
        import webbrowser

        webbrowser.open(app.url())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("stopped")
    finally:
        httpd.server_close()


@queue_group.command("report")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Only pilot companies.")
@_db_option
def queue_report(company_id: str | None, pilot: bool, db_file: Path | None) -> None:
    """Step 68: kill reasons, most-edited sections, review minutes, Gate 5.3."""
    from engine.outreach import review as RV

    conn = _conn_or_fail(db_file)
    try:
        for line in RV.report(conn, pilot=pilot, company_id=company_id):
            click.echo(line)
    finally:
        conn.close()


@queue_group.command("followups")
@click.option("--today", "today", default=None, metavar="YYYY-MM-DD",
              help="The day to list for (default today, UTC).")
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Only pilot companies.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@_teardowns_option
@_sender_option
@_db_option
def queue_followups(today: str | None, company_id: str | None, pilot: bool, limit: int | None,
                    teardowns_dir: Path | None, sender_file: Path | None,
                    db_file: Path | None) -> None:
    """Step 71: threads due for touch 2 (day 4) or 3 (day 9), with the text to paste."""
    from engine.outreach import followups as F

    day = _parse_day(today, "--today") or datetime.now(UTC).date()
    conn = _conn_or_fail(db_file)
    try:
        res = F.followups(conn, day, company_id=company_id, pilot=pilot, limit=limit,
                          teardowns=teardowns_dir, sender_file=sender_file)
        terminal = F.is_terminal()
        if not res.due:
            click.echo(f"no follow-up due on {day}")
        for f in res.due:
            how = ("press Reply in the touch-1 thread in Gmail and paste"
                   if f.thread_channel == "email" else
                   "send by hand on LinkedIn (a connection request with no pitch may stand in "
                   "for touch 2)")
            click.echo(f"{f.company_id}: touch {f.touch_no} due (day {f.day}; due {f.due_on}): "
                       f"{how}")
            if f.gap:
                click.echo(f"    from secondary gap {f.gap}, claims {f.claims}")
            for p in f.problems:
                click.echo(f"    problem: {p}")
            # {first_name} is filled only on a terminal, never into a file or a log
            name = F.recipient_first_name(conn, f.company_id) if terminal else None
            click.echo("    ---")
            for line in F.fill_name(f.text, None if f.fallback else name).split("\n"):
                click.echo(f"    {line}" if line else "")
            click.echo("    ---")
            if not f.fallback:
                click.echo(f"    then: te queue followup-sent {f.company_id} --touch "
                           f"{f.touch_no}")
        for cid, n, due_on in res.upcoming:
            click.echo(f"{cid}: touch {n} not due until {due_on}")
        for cid, why in res.skipped:
            click.echo(f"{cid}: skipped: {why}")
    finally:
        conn.close()


@queue_group.command("followup-sent")
@click.argument("company_id")
@click.option("--touch", "touch_no", type=click.IntRange(min=1), required=True,
              help="2 or 3. There is no fourth message.")
@click.option("--channel", type=click.Choice(["email-reply", "linkedin"]), default=None,
              help="Default: email-reply for an email thread, linkedin for a LinkedIn one.")
@click.option("--at", "at", default=None, metavar="ISO",
              help="When it was sent (ISO 8601, UTC unless an offset is given); default now.")
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_followup_sent(company_id: str, touch_no: int, channel: str | None, at: str | None,
                        dry_run: bool, db_file: Path | None) -> None:
    """Step 71: a person sent follow-up touch 2 or 3 by hand; record it."""
    from engine.outreach import draft as Q
    from engine.outreach import followups as F

    when = None
    if at is not None:
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            _fail(f"--at {at!r} is not an ISO 8601 time")
            return
    conn = _conn_or_fail(db_file)
    try:
        r = F.followup_sent(conn, company_id, touch_no, channel=channel, at=when,
                            dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


@queue_group.command("bounce")
@click.argument("company_id")
@click.option("--on", "on", default=None, metavar="YYYY-MM-DD",
              help="The day it bounced (default today, UTC).")
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_bounce(company_id: str, on: str | None, dry_run: bool, db_file: Path | None) -> None:
    """Step 74: the latest sent email to COMPANY_ID bounced; suppress the address."""
    from engine.outreach import bounces as B
    from engine.outreach import draft as Q

    day = _parse_day(on, "--on")
    conn = _conn_or_fail(db_file)
    try:
        r = B.bounce(conn, company_id, on=day, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)
    sys.exit(1 if r.outcome == "refused" else 0)


@queue_group.command("resume")
@click.option("--reason", required=True,
              help='What was done, e.g. "addresses re-verified".')
@click.option("--dry-run", is_flag=True, help="Print what would be recorded.")
@_db_option
def queue_resume(reason: str, dry_run: bool, db_file: Path | None) -> None:
    """Step 74: lift the stop that two bounces in one day put on drafting."""
    from engine.outreach import bounces as B
    from engine.outreach import draft as Q

    conn = _conn_or_fail(db_file)
    try:
        r = B.resume(conn, reason, dry_run=dry_run)
    except Q.QueueError as e:
        _fail(f"refused: {e}")
        return
    finally:
        conn.close()
    _echo_result(r)


# --- draft and quality (Steps 64-65) -------------------------------------------------


class _DraftGroup(click.Group):
    """`te draft <company_id>` beside `te draft init-voice`: a first argument that is
    not a subcommand is the company id."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["--company", args[0], *args[1:]]
        return super().parse_args(ctx, args)


_voice_option = click.option(
    "--voice", "voice_file", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Voice examples file (default: $TE_VOICE_EXAMPLES or data/voice_examples.yaml).")


@main.group("draft", cls=_DraftGroup, invoke_without_command=True)
@click.option("--company", "company_id", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every pilot company with a named gap.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--dry-run", is_flag=True, help="Build each prompt; no call, no write.")
@click.option("--force", is_flag=True, help="Overwrite sections a person has edited.")
@click.option("--no-voice", is_flag=True, help="Run without voice examples.")
@_voice_option
@_teardowns_option
@_db_option
@click.pass_context
def draft_cmd(ctx: click.Context, company_id: str | None, pilot: bool, limit: int | None,
              dry_run: bool, force: bool, no_voice: bool, voice_file: Path | None,
              teardowns_dir: Path | None, db_file: Path | None) -> None:
    """Draft sections 3, 4, 5, 7 and the email into the teardown file (Step 64).

    One call per company on the drafter route, keyed on (draft, company_id,
    d-1.0:<hash of the inputs>): a re-run with the same inputs makes no call.
    Never overwrites a section a person edited without --force. Exits 1 on any
    refused or error.
    """
    if ctx.invoked_subcommand is not None:
        return
    from engine.build import drafter as DR
    from engine.guard.budget import BudgetExceeded

    if not company_id and not pilot:
        _fail("give a company_id, --company or --pilot")
        return
    conn = _conn_or_fail(db_file)
    bad = calls = 0
    counts: dict[str, int] = {}
    try:
        for cid in DR.select(conn, company_id=company_id, pilot=pilot, limit=limit):
            try:
                r = DR.draft_company(conn, cid, directory=teardowns_dir, voice_file=voice_file,
                                     no_voice=no_voice, force=force, dry_run=dry_run)
            except DR.DrafterError as e:
                _fail(f"refused: {e}")
                return
            except BudgetExceeded as e:
                click.echo(f"stopped: {e}")
                bad += 1
                break
            for w in r.warnings:
                click.echo(f"{cid}: warning: {w}")
            extra = f"; prompt {r.prompt_chars} chars, voice examples {r.voice}"
            if r.masked:
                extra += f", {r.masked} contact values masked"
            click.echo(f"{cid}: {r.outcome}: {r.detail}{extra}")
            if r.outcome == "drafted" and r.path:
                click.echo(f"{cid}: wrote {r.path}; next: te quality {cid}")
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
            calls += r.calls
            bad += r.outcome in ("refused", "error")
    finally:
        conn.close()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    click.echo(f"draft: companies {sum(counts.values())}: {summary or 'none'}; "
               f"provider calls {calls}")
    sys.exit(1 if bad else 0)


@draft_cmd.command("init-voice")
@_voice_option
@click.option("--force", "force_voice", is_flag=True, help="Overwrite an existing file.")
def draft_init_voice(voice_file: Path | None, force_voice: bool) -> None:
    """Write a voice_examples.yaml skeleton: three sent emails, names removed."""
    from engine.build import drafter as DR

    try:
        p = DR.init_voice(voice_file, force=force_voice)
    except DR.DrafterError as e:
        _fail(f"refused: {e}")
        return
    click.echo(f"wrote {p}; paste the emails you sent, with every name replaced")


@main.command("quality")
@click.argument("company_id", required=False)
@click.option("--company", "company_opt", default=None, help="Only this company_id.")
@click.option("--pilot", is_flag=True, help="Every pilot company with a teardown file.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most N companies.")
@click.option("--dry-run", is_flag=True, help="Run the gates; record nothing.")
@_teardowns_option
@_sender_option
@_db_option
def quality_cmd(company_id: str | None, company_opt: str | None, pilot: bool,
                limit: int | None, dry_run: bool, teardowns_dir: Path | None,
                sender_file: Path | None, db_file: Path | None) -> None:
    """Run the quality gates before human review (Step 65); exit 1 on any failure.

    No model call. Records pass/fail and the reasons in quality_checks.
    """
    from engine.build import quality as QG
    from engine.build import teardown as T

    cid = company_id or company_opt
    if not cid and not pilot:
        _fail("give a company_id, --company or --pilot")
        return
    conn = _conn_or_fail(db_file)
    failed = passed = 0
    try:
        for i in QG.select(conn, company_id=cid, pilot=pilot, limit=limit,
                           directory=teardowns_dir):
            try:
                r = QG.run_company(conn, i, directory=teardowns_dir, sender_file=sender_file,
                                   dry_run=dry_run)
            except T.TeardownError as e:
                click.echo(f"{i}: error: {e}")
                failed += 1
                continue
            for x in r.reasons:
                click.echo(f"{i}: FAIL {x.gate}: {x.message}")
            click.echo(f"{i}: " + ("pass" if r.passed else f"fail ({len(r.reasons)} reason(s))")
                       + f"; {r.recorded}")
            passed += r.passed
            failed += not r.passed
    finally:
        conn.close()
    click.echo(f"quality {'dry run ' if dry_run else ''}: passed {passed}, failed {failed}")
    sys.exit(1 if failed else 0)


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
