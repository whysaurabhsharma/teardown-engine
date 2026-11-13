"""Renderer v1 (docs/PLAN.md Step 52; protocol/TEARDOWN_SPEC.md v1.2).

    te teardown render <company_id> | --company <id> | --pilot [--limit n] [--dry-run]

For each company: run `te teardown check` on data/teardowns/<company_id>.yaml
and refuse on any failure; fill engine/build/templates/teardown.html (jinja2,
autoescaped) with the eight sections in order; write
site/<slug>/index.html with the one stylesheet beside it
(site/<slug>/teardown.css, so deleting a page on request is deleting one
folder); write site/_headers (X-Robots-Tag: noindex, nofollow on every path)
and site/robots.txt (disallow all). $TE_SITE_DIR or --site-dir overrides site/.

Slug: `{company-slug}-{8 random lowercase alphanumerics}` (CONDUCT.md
"Hosting"), generated once with `secrets` and stored in teardowns.page_slug;
a re-render reuses it, so the URL is stable. The future URL is
$TEARDOWN_BASE_URL/<slug>/ when that is set.

Evidence lines (TEARDOWN_SPEC 1.2):
  found      the quoted line, then a link to the page it was quoted from
  not found  "Not found: no {kind} linked from {where}", a link to where it
             was looked for, and the capture date; never quoted
Public DNS records have no page to link: their source is printed as text
("public DNS records for <domain>") with the lookup date.

The page names no recipient: it is shared by link. `{first_name}` is the
email's (Step 54), filled locally then. No script, no tracking pixel, no
external font or stylesheet; the only links are the evidence sources, the
proof-slice sources and the sender's two links.

A render moves the company diagnosed -> built (forward only); a later status is
kept. Files are rewritten only when their content changes. No network, no
model call. --dry-run checks and renders in memory, writes nothing and stores
no slug.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import string
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from engine import states
from engine.build import teardown as T
from engine.models import Teardown
from engine.states import Status

SITE_ENV_VAR = "TE_SITE_DIR"
DEFAULT_SITE_DIR = Path("site")
BASE_URL_ENV_VAR = "TEARDOWN_BASE_URL"
TEMPLATES = Path(__file__).parent / "templates"
STYLESHEET = "teardown.css"
RENDERER_VERSION = "render-1.0"

HEADERS = "/*\n  X-Robots-Tag: noindex, nofollow\n"
ROBOTS = "User-agent: *\nDisallow: /\n"
SLUG_ALPHABET = string.ascii_lowercase + string.digits
SLUG_RANDOM = 8


class RenderError(RuntimeError):
    """The page cannot be rendered."""


def site_dir(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(SITE_ENV_VAR)
    return Path(env) if env else DEFAULT_SITE_DIR


def company_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40].strip("-") or "company"


def new_slug(name: str) -> str:
    tail = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_RANDOM))
    return f"{company_slug(name)}-{tail}"


def stored_slug(conn: sqlite3.Connection, company_id: str) -> str | None:
    r = conn.execute("SELECT page_slug FROM teardowns WHERE company_id = ? AND page_slug IS NOT "
                     "NULL ORDER BY teardown_id LIMIT 1", (company_id,)).fetchone()
    return r["page_slug"] if r is not None else None


def ensure_slug(conn: sqlite3.Connection, company_id: str, name: str) -> str:
    """The company's slug, generated and stored on first render."""
    slug = stored_slug(conn, company_id)
    if slug is not None:
        return slug
    while True:
        slug = new_slug(name)
        if conn.execute("SELECT 1 FROM teardowns WHERE page_slug = ?", (slug,)).fetchone():
            continue
        with conn:
            row = conn.execute("SELECT teardown_id FROM teardowns WHERE company_id = ? "
                               "ORDER BY teardown_id LIMIT 1", (company_id,)).fetchone()
            if row is not None:
                conn.execute("UPDATE teardowns SET page_slug = ? WHERE teardown_id = ?",
                             (slug, row["teardown_id"]))
            else:
                from engine import db

                db.insert(conn, Teardown(company_id=company_id, page_slug=slug))
        return slug


def future_url(slug: str) -> str | None:
    base = os.environ.get(BASE_URL_ENV_VAR, "").strip()
    return f"{base.rstrip('/')}/{slug}/" if base else None


# -- the page's content --------------------------------------------------------------------


def nice_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def is_http(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def short_url(url: str) -> str:
    p = urlsplit(url)
    if not is_http(url):
        return url
    path = p.path if p.path not in ("", "/") else ""
    return f"{p.netloc}{path}" + (f"?{p.query}" if p.query else "")


def label(kind: str) -> str:
    return T.KIND_LABELS.get(kind, kind.replace("_", " "))


@dataclass
class Source:
    """Where a line of evidence came from: a link, or text when there is no page."""

    href: str | None
    text: str


def source(url: str) -> Source:
    if is_http(url):
        return Source(url, short_url(url))
    if url.startswith("dns:"):
        return Source(None, f"public DNS records for {url[4:]}")
    return Source(None, url)


@dataclass
class Evidence:
    found: bool
    quote: str | None
    line: str  # the "Not found: ..." sentence for an absence
    src: Source
    captured: str | None


@dataclass
class Looked:
    label: str
    found: bool
    line: str
    src: Source
    captured: str


@dataclass
class Page:
    company: str
    role: str
    date: str
    sender_name: str
    looked: list[Looked]
    need: list[str]
    evidence: list[Evidence]
    why_now: str
    build: list[str]
    slice_title: str
    slice_columns: list[str]
    slice_rows: list[dict[str, Any]]
    might_have_wrong: str
    about: list[str]
    links: list[dict[str, str]]
    offer: str
    postal_address: str
    opt_out: str
    renderer_version: str = RENDERER_VERSION
    hrefs: set[str] = field(default_factory=set)


def not_found_line(kind: str, url: str, ctx: T.Context) -> str:
    if kind == "dns":
        return f"Not found: no such record in the {label(kind)} looked up"
    where = T.where_label(url, ctx.arts, ctx.homepage_url)
    return f"Not found: no {label(kind)} linked from {where}"


def build_page(chk: T.Checked) -> Page:
    doc, ctx, snd = chk.doc, chk.ctx, chk.sender
    if doc is None or ctx is None or snd is None or not chk.ok:
        raise RenderError(f"{chk.company_id}: the teardown check failed; nothing rendered")
    looked: list[Looked] = []
    for r in doc.looked_at:
        line = "found" if r.found else not_found_line(r.kind, r.url, ctx)
        name = label(r.kind)
        looked.append(Looked(name[:1].upper() + name[1:], r.found, line, source(r.url),
                             nice_date(r.captured)))
    evidence: list[Evidence] = []
    for i in doc.need.evidence:
        c = ctx.claims[i]
        a = ctx.arts.get(c.artifact_kind)
        when = nice_date(T.captured(a)) if a is not None else None
        if c.found:
            evidence.append(Evidence(True, c.quote, "", source(c.url), when))
        else:
            evidence.append(Evidence(False, None, not_found_line(c.artifact_kind, c.url, ctx),
                                     source(c.url), when))
    rows = [{"cells": r.cells, "quote": r.quote, "src": source(r.url or "")}
            for r in doc.slice.rows]
    page = Page(
        company=doc.header.company, role=doc.header.role, date=nice_date(doc.header.date),
        sender_name=snd.name, looked=looked, need=[s.text for s in doc.need.sentences],
        evidence=evidence, why_now=doc.why_now, build=list(doc.build),
        slice_title=doc.slice.title, slice_columns=list(doc.slice.columns), slice_rows=rows,
        might_have_wrong=doc.might_have_wrong, about=list(snd.about),
        links=[{"label": lk.label, "url": lk.url} for lk in snd.links],
        offer=T.offer_line(doc, snd), postal_address=snd.postal_address, opt_out=snd.opt_out)
    hrefs = {x.src.href for x in looked} | {e.src.href for e in evidence} | \
        {r["src"].href for r in rows} | {lk["url"] for lk in page.links}
    page.hrefs = {h for h in hrefs if h}
    return page


def environment() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)),
                       autoescape=select_autoescape(["html"], default=True),
                       undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                       keep_trailing_newline=True)


def html(page: Page) -> str:
    return environment().get_template("teardown.html").render(p=page)


# -- writing -------------------------------------------------------------------------------


def _write(path: Path, text: str) -> bool:
    """Write when the content differs; True when written."""
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def write_site_files(site: Path) -> None:
    _write(site / "_headers", HEADERS)
    _write(site / "robots.txt", ROBOTS)


@dataclass
class Rendered:
    company_id: str
    outcome: str  # rendered | unchanged | dry_run | refused | error
    detail: str = ""
    path: Path | None = None
    slug: str | None = None
    url: str | None = None
    failures: list[T.Failure] = field(default_factory=list)


def render_company(conn: sqlite3.Connection, company_id: str, *,
                   teardowns: Path | None = None, sender_file: Path | None = None,
                   site: Path | None = None, dry_run: bool = False) -> Rendered:
    try:
        chk = T.check(conn, company_id, directory=teardowns, sender_file=sender_file)
    except T.TeardownError as e:
        return Rendered(company_id, "error", str(e))
    if not chk.ok:
        return Rendered(company_id, "refused", f"{len(chk.failures)} check failure(s)",
                        failures=chk.failures)
    assert chk.ctx is not None
    page = build_page(chk)
    text = html(page)
    root = site_dir(site)
    if dry_run:
        slug = stored_slug(conn, company_id)
        path = root / slug / "index.html" if slug else None
        return Rendered(company_id, "dry_run", "checked and rendered in memory; nothing written",
                        path, slug, future_url(slug) if slug else None)
    slug = ensure_slug(conn, company_id, chk.ctx.name)
    out = root / slug
    changed = _write(out / "index.html", text)
    changed |= _write(out / STYLESHEET, (TEMPLATES / STYLESHEET).read_text(encoding="utf-8"))
    write_site_files(root)
    if Status(chk.ctx.status) is Status.DIAGNOSED:
        states.advance(company_id, Status.BUILT, conn)
    return Rendered(company_id, "rendered" if changed else "unchanged", "", out / "index.html",
                    slug, future_url(slug))


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    if company_id is not None:
        return [company_id]
    sql = "SELECT company_id FROM companies WHERE status != 'dropped'"
    if pilot:
        sql += " AND pilot = 1"  # the ten pilot teardowns; hand-made ones were sent by hand
    ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY company_id")]
    return ids[:limit] if limit is not None else ids
