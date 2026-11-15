"""`te demo` (and `make demo`): the whole pipeline on three invented companies,
offline, with no keys (docs/PLAN.md Step 88, Gate 7.2).

Every path lives in one folder (a fresh temporary one unless --out is given):
the database, the cache, the teardown files, the sender file and the rendered
site. data/, site/ and .env are never read or written; the demo sets its own
SENDER_EMAIL, TE_* paths and providers for the length of the run and puts the
old values back afterwards.

The stages are the engine's own functions, in the order the Makefile runs them:

    signal    the greenhouse collector reads each invented board
    classify  title rules only (no model)
    filter    fresh and India-located
    resolve   seeded: the domain and segment a person would confirm (the real
              resolver needs a search provider)
    qualify   the ICP rules
    capture   Guard and DnsGuard, on a mock transport and resolver serving
              engine/demo/sites.py; robots.txt is obeyed (one site says no)
    extract   text, then facts
    judge     canned answers (engine/demo/canned.py) through llm.gateway, with
              no worked examples (a fresh database holds no gold set)
    verify    a canned verifier of another model family
    slice     icp_accounts needs a search provider, so it fails as "search not
              configured" and role_funnel, the fallback, is built
    draft     canned drafter, then te teardown check and render
    metrics   te metrics on the demo database, after one SIMULATED send, reply
              and review time, labelled as such

Network: Guard is given an httpx.MockTransport, DnsGuard a fake resolver and
capture a fake renderer, so no request leaves the process. As a second wall,
every socket connect to a host other than loopback is refused while the demo
runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import yaml

from engine import db as dbmod
from engine.demo import canned
from engine.demo.sites import (
    COMPANIES,
    GREENHOUSE_API,
    GREENHOUSE_JOBS,
    DemoCompany,
    board_json,
)
from engine.guard.browser import BrowserError, Rendered
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.guard.dns import DnsGuard, LookupFailed, NameNotFound
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter

DEMO_SENDER_EMAIL = "demo@sender.example"
SENDER = {
    "version": "sender-1",
    "name": "Demo Sender",
    "identity": "I build outbound and enrichment systems.",
    "about": ["I build outbound and enrichment systems.",
              "I have run a revenue number at a small company.",
              "I write the Python and SQL myself."],
    "links": [{"label": "CV", "url": "https://sender.example/cv"},
              {"label": "Code", "url": "https://code.example/sender"}],
    "offer": {"candidate": "I have applied for the {role} role at {company}.",
              "adjacent": "I am looking for a GTM engineer role, and I am open to a project."},
    "postal_address": "1 Example Lane, Pune 411001",
    "opt_out": 'Not relevant? Reply "no" to my email and I will not write again.',
}
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class DemoError(RuntimeError):
    pass


# -- the invented internet -----------------------------------------------------------------


@dataclass
class Sites:
    """host -> path -> (status, content type, body), built from sites.py."""

    today: date
    routes: dict[str, dict[str, tuple[int, str, bytes]]] = field(default_factory=dict)
    requests: int = 0

    def __post_init__(self) -> None:
        html = "text/html; charset=utf-8"
        for c in COMPANIES:
            r = self.routes.setdefault(c.domain, {})
            r["/robots.txt"] = (200, "text/plain", c.robots.encode())
            for path, body in c.pages.items():
                r[path] = (200, html, body.encode())
            api = self.routes.setdefault(GREENHOUSE_API, {})
            api[f"/v1/boards/{c.slug}/jobs"] = (200, "application/json", board_json(c, self.today))
            jobs = self.routes.setdefault(GREENHOUSE_JOBS, {})
            for j in c.jobs:
                if "page" in j:
                    jobs[f"/{c.slug}/jobs/{j['id']}"] = (200, html, j["page"].encode())
        for host in (GREENHOUSE_API, GREENHOUSE_JOBS):
            self.routes[host]["/robots.txt"] = (200, "text/plain", b"User-agent: *\nAllow: /\n")

    def lookup(self, url: str) -> tuple[int, str, bytes]:
        u = httpx.URL(url)
        return self.routes.get(u.host, {}).get(u.path, (404, "text/plain", b"no such page"))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        status, ctype, body = self.lookup(str(request.url))
        return httpx.Response(status, headers={"content-type": ctype}, content=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class Resolver:
    """DNS answers from sites.py. A company with no records fails every lookup
    (unseen, never absent); a name not listed is NXDOMAIN."""

    def __init__(self, companies: tuple[DemoCompany, ...]):
        self.records: dict[tuple[str, str], list[str]] = {}
        self.dead: set[str] = set()
        for c in companies:
            if c.dns:
                self.records.update(c.dns)
            else:
                self.dead.add(c.domain)

    async def resolve(self, name: str, rdtype: str, timeout: float) -> list[str]:
        if any(name == d or name.endswith("." + d) for d in self.dead):
            raise LookupFailed("demo: no nameserver answers for this domain")
        if (name, rdtype) in self.records:
            return self.records[(name, rdtype)]
        raise NameNotFound("NXDOMAIN")


class Renderer:
    """Stands in for headless chromium: asks Guard's allow() as the real one does,
    then returns what the invented site serves."""

    def __init__(self, sites: Sites):
        self.sites = sites

    async def render(self, url: str, *, user_agent: str, allow, timeout: float) -> Rendered:
        if not await allow(url, "document"):
            raise BrowserError("document request refused")
        status, ctype, body = self.sites.lookup(url)
        return Rendered(status=status, final_url=url, html=body.decode("utf-8"),
                        headers={"content-type": ctype})

    async def aclose(self) -> None:
        return None


def _fast() -> RateLimiter:
    # The invented hosts live in this process; the 1 request/second pacing protects
    # real sites, and real runs keep it.
    return RateLimiter(limits={}, default_rps=1000)


# -- the walls -----------------------------------------------------------------------------


@contextlib.contextmanager
def no_network() -> Iterator[None]:
    """Refuse every socket connect to a host other than loopback."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_create = socket.create_connection

    def _check(address: object) -> None:
        if isinstance(address, tuple) and address and str(address[0]) not in LOOPBACK:
            raise ConnectionRefusedError("te demo makes no network connection")

    def connect(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return real_connect_ex(self, address)

    def create_connection(address, *a, **kw):  # type: ignore[no-untyped-def]
        _check(address)
        return real_create(address, *a, **kw)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.create_connection = real_create  # type: ignore[assignment]


@contextlib.contextmanager
def demo_env(root: Path) -> Iterator[None]:
    """The demo's own paths, sender address and providers; the old ones come back."""
    from engine.llm import gateway

    env = {
        dbmod.DB_ENV_VAR: str(root / "engine.db"),
        "TE_CACHE_DIR": str(root / "cache"),
        "TE_TEARDOWNS_DIR": str(root / "teardowns"),
        "TE_SENDER": str(root / "sender.yaml"),
        "TE_SITE_DIR": str(root / "site"),
        "TE_VOICE_EXAMPLES": str(root / "voice_examples.yaml"),
        "SENDER_EMAIL": DEMO_SENDER_EMAIL,
        "TE_NO_DOTENV": "1",
    }
    unset = ("TEARDOWN_BASE_URL",)
    old_env = {k: os.environ.get(k) for k in (*env, *unset)}
    old_providers = dict(gateway.PROVIDERS)
    old_budget = gateway._budget
    os.environ.update(env)
    for k in unset:
        os.environ.pop(k, None)
    for name in list(gateway.PROVIDERS):
        gateway.PROVIDERS[name] = _unavailable(name)
    gateway.PROVIDERS["claude"] = canned.CannedProvider("writer", "demo-writer")
    gateway.PROVIDERS["gemini"] = canned.CannedProvider("verifier", "demo-verifier")
    gateway._budget = Budget()
    try:
        yield
    finally:
        gateway.PROVIDERS.clear()
        gateway.PROVIDERS.update(old_providers)
        gateway._budget = old_budget
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _unavailable(name: str):
    from engine.llm.base import NotConfigured

    def provider(prompt, schema, model):  # type: ignore[no-untyped-def]
        raise NotConfigured(f"demo: {name} is not used")

    return provider


# -- the run -------------------------------------------------------------------------------


@dataclass
class DemoResult:
    root: Path
    teardown: Path | None = None
    lines: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    requests: int = 0
    statuses: dict[str, str] = field(default_factory=dict)


def _step(res: DemoResult, echo: Callable[[str], None], text: str) -> None:
    res.lines.append(text)
    echo(text)


def run(root: Path, *, today: date | None = None,
        echo: Callable[[str], None] = lambda s: None) -> DemoResult:
    """Run the demo into root (created; must be empty or absent)."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise DemoError(f"{root} is not empty; give an empty or new folder")
    root.mkdir(parents=True, exist_ok=True)
    with demo_env(root), no_network():
        return asyncio.run(_run(root, today or datetime.now(UTC).date(), echo))


async def _run(root: Path, today: date, echo: Callable[[str], None]) -> DemoResult:
    from engine.build import drafter as DR
    from engine.build import render as RD
    from engine.build.slices import common as SC
    from engine.build.slices import run as SL
    from engine.capture import capture as CAP
    from engine.capture import extract_text as XT
    from engine.extract import run as XF
    from engine.guard import search_api
    from engine.judge import judge as J
    from engine.judge import verify as V
    from engine.models import Company
    from engine.outreach import metrics as M
    from engine.resolve import qualify as Q
    from engine.resolve.domain import company_id_for
    from engine.signal import boards, classify
    from engine.signal import filter as F
    from engine.states import Status, advance

    res = DemoResult(root)
    say = lambda s: _step(res, echo, s)  # noqa: E731
    say(f"te demo: three invented companies, offline, in {root}")
    db_path, _ = dbmod.init_db(root / "engine.db")
    conn = dbmod.connect(db_path)
    cache = Cache(root / "cache")
    sites = Sites(today)
    budget = Budget()

    def guard() -> Guard:
        return Guard(cache=cache, budget=budget, transport=sites.transport(), limiter=_fast())

    try:
        # signal
        g = guard()
        try:
            n = 0
            for c in COMPANIES:
                br = await boards.collect_board("greenhouse", c.slug, g, today=today)
                n += boards.store(conn, br.signals)
        finally:
            await g.aclose()
        classify.run(conn, use_model=False)
        F.run(conn, today=today)
        say(f"signal    {n} job posts from {len(COMPANIES)} invented greenhouse boards; "
            "titles classified by rules")

        # resolve (seeded) and qualify
        ids: dict[str, str] = {}
        with conn:
            for c in COMPANIES:
                cid = company_id_for(c.domain)
                ids[c.name] = cid
                dbmod.insert(conn, Company(company_id=cid, name=c.name, domain=c.domain,
                                           segment=c.segment, status=Status.RESOLVED,
                                           hq="india", hq_basis="demo: seeded"))
                conn.execute("UPDATE signals SET company_id = ? WHERE source = 'greenhouse' "
                             "AND board_slug = ?", (cid, c.slug))
        q = Q.run(conn)
        say(f"resolve   {len(COMPANIES)} companies seeded with domain and segment")
        say(f"qualify   {q.qualified} qualified" + (f"; held {dict(q.held)}" if q.held else ""))

        # capture
        rt = CAP.Runtime(guard=guard(),
                         dns=DnsGuard(cache=cache, budget=budget, limiter=_fast(),
                                      resolver=Resolver(COMPANIES)),
                         renderer=Renderer(sites))
        try:
            targets = [CAP.load_target(conn, cid) for cid in sorted(ids.values())
                       if _status(conn, cid) == Status.QUALIFIED]
            rep = await CAP.run(conn, targets, rt)
        finally:
            await rt.aclose()
        for cid, state, counts in rep.results:
            say(f"capture   {cid}: {state} ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})")
        for e in rep.errors:
            say(f"capture   error: {e}")

        live = [cid for cid in sorted(ids.values()) if _status(conn, cid) == Status.CAPTURED]
        XT.run(conn, cache, live)
        xr = XF.run(conn, cache, live)
        say(f"extract   text and facts for {len(live)} companies; {xr.advanced} extracted")

        # judge and verify
        for cid in live:
            jr = J.judge_company(conn, cache, cid, examples=[], budget=Budget())
            say(f"judge     {cid}: {jr.outcome} {jr.detail}")
            if jr.outcome == "diagnosed":
                vr = V.verify_company(conn, cache, cid, budget=Budget())
                say(f"verify    {cid}: {vr.outcome} ({vr.detail})")

        # slice, draft, check, render
        def no_search(_b: Budget):
            raise search_api.NotConfigured("demo: no search provider")

        srt = SC.Runtime(cache=cache, make_guard=lambda b: Guard(
            cache=cache, budget=b, transport=sites.transport(), limiter=_fast()),
            make_searcher=no_search, today=today)
        diagnosed = [cid for cid in live if _status(conn, cid) == Status.DIAGNOSED]
        sr = await SL.run(conn, srt, diagnosed)
        for r in sr.results:
            say(f"slice     {r.company_id}: {r.outcome} {r.selected or ''}".rstrip())
            for line in r.lines:
                say(f"            {line}")

        sender = root / "sender.yaml"
        sender.write_text(yaml.safe_dump(SENDER, sort_keys=False, allow_unicode=True),
                          encoding="utf-8")
        tds = root / "teardowns"
        site = root / "site"
        for cid in diagnosed:
            dr = DR.draft_company(conn, cid, directory=tds, no_voice=True, budget=Budget(),
                                  today=today)
            say(f"draft     {cid}: {dr.outcome} {dr.detail}")
            rr = RD.render_company(conn, cid, teardowns=tds, sender_file=sender, site=site)
            for f in rr.failures:
                say(f"render    {cid}: {f}")
            say(f"render    {cid}: {rr.outcome}" + (f" {rr.path}" if rr.path else
                                                     f" {rr.detail}"))
            if rr.outcome == "rendered" and res.teardown is None:
                res.teardown = Path(rr.path) / "index.html" if Path(rr.path).is_dir() \
                    else Path(rr.path)

        # simulated outcome, so the metrics show a funnel end to end
        sent = [cid for cid in diagnosed if _status(conn, cid) == Status.BUILT]
        if sent:
            cid = sent[0]
            from engine.models import Touch
            from engine.outreach import replies as RP

            with conn:
                dbmod.insert(conn, Touch(company_id=cid, touch_no=1, channel="email",
                                         status="sent",
                                         sent_at=datetime(today.year, today.month, today.day,
                                                          9, tzinfo=UTC)))
                conn.execute("UPDATE teardowns SET review_minutes = 25 WHERE company_id = ?",
                             (cid,))
            advance(cid, Status.SENT, conn)
            RP.reply(conn, cid, "positive", on=today, today=today)
            say(f"simulate  {cid}: DEMO DATA: one send, one positive reply, 25 review "
                "minutes (no email was drafted or sent)")

        res.requests = sites.requests
        res.statuses = {cid: _status(conn, cid) for cid in sorted(ids.values())}
        res.metrics = M.format_text(M.compute(conn, by="segment"))
    finally:
        conn.close()
    return res


def _status(conn, cid: str) -> str:  # type: ignore[no-untyped-def]
    return conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (cid,)).fetchone()[0]


def default_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="te-demo-"))
