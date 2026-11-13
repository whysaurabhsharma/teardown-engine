"""Step 35 (extract_text) and Step 36 (te show). Invented companies only.

No network and no model calls: every test that runs the extractor over the
database blocks sockets and the model gateway.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.capture import extract_text as X
from engine.capture.capture import write_rows
from engine.capture.show import show_lines
from engine.cli import main
from engine.debug import export_company
from engine.guard.cache import Cache
from engine.models import Artifact, Contact
from tests.conftest import add_company

FIX = Path(__file__).parent / "fixtures"
HOME = (FIX / "extract" / "velmora_home.html").read_bytes()
JOB = (FIX / "extract" / "velmora_job.html").read_bytes()
CID = "velmora-example"
WHEN = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

DNS_DOC = {
    "bundle_version": "bundle-1.0",
    "domain": "velmora.example",
    "lookups": [
        {"name": "velmora.example", "type": "TXT", "purpose": "spf", "outcome": "ok",
         "records": ["v=spf1 include:_spf.google.com ~all", "google-site-verification=abc123"],
         "detail": ""},
        {"name": "_dmarc.velmora.example", "type": "TXT", "purpose": "dmarc", "outcome": "ok",
         "records": ["v=DMARC1; p=none; rua=mailto:dmarc@velmora.example"], "detail": ""},
        {"name": "velmora.example", "type": "MX", "purpose": "mx", "outcome": "ok",
         "records": ["1 aspmx.l.google.com", "5 alt1.aspmx.l.google.com"], "detail": ""},
        {"name": "google._domainkey.velmora.example", "type": "TXT", "purpose": "dkim",
         "outcome": "ok", "records": ["v=DKIM1; k=rsa; p=MIIBIjAN"], "detail": ""},
        {"name": "selector1._domainkey.velmora.example", "type": "TXT", "purpose": "dkim",
         "outcome": "not_found", "records": [], "detail": "NXDOMAIN"},
        {"name": "k1._domainkey.velmora.example", "type": "TXT", "purpose": "dkim",
         "outcome": "not_found", "records": [], "detail": "no TXT record"},
        {"name": "em.velmora.example", "type": "CNAME", "purpose": "sending_subdomain",
         "outcome": "ok", "records": ["u123.wl.sendgrid.net"], "detail": ""},
        {"name": "mail.velmora.example", "type": "CNAME", "purpose": "sending_subdomain",
         "outcome": "not_found", "records": [], "detail": "NXDOMAIN"},
        {"name": "mail.velmora.example", "type": "A", "purpose": "sending_subdomain",
         "outcome": "not_found", "records": [], "detail": "not sent: name is NXDOMAIN"},
        {"name": "sg.velmora.example", "type": "TXT", "purpose": "sending_subdomain",
         "outcome": "dead", "records": [], "detail": "timeout after 3s"},
    ],
}
DNS_BODY = json.dumps(DNS_DOC, sort_keys=True, separators=(",", ":")).encode("utf-8")


# -- pure extraction -------------------------------------------------------------------


def test_text_drops_scripts_styles_nav_footer() -> None:
    text, _ = X.extract("homepage", "https://velmora.example/", HOME)
    for marker in ("SCRIPT_MARKER", "STYLE-MARKER", "LDJSON-MARKER", "SVG-MARKER",
                   "NOSCRIPT-MARKER", "TEMPLATE-MARKER", "COMMENT-MARKER", "SELECT-MARKER",
                   "Customer stories", "For logistics", "Docs", "Private Limited",
                   "Velmora | Route"):
        assert marker not in text, marker
    assert "Route planning for field teams" in text.split("\n")
    assert "Plan every visit in one place." in text.split("\n")
    assert "No spreadsheets." in text.split("\n")
    assert "Book a demo" in text
    assert "Chat with us" in text
    assert "₹2,000 / month" in text  # entity decoded, kept as the page shows it


def test_whitespace_rule() -> None:
    text, _ = X.extract("homepage", "https://velmora.example/", HOME)
    assert not text.startswith(("\n", " ")) and not text.endswith(("\n", " "))
    for line in text.split("\n"):
        assert line and line == " ".join(line.split())  # no NBSP, tab or double space
    # A quote spanning two lines matches once both sides are whitespace-normalised.
    quote = "place.\n   No spreadsheets."
    assert X.normalise_ws(quote) in X.normalise_ws(text)
    assert X.normalise_lines(["  a \tb  ", "", " 　 ", "c"]) == "a b\nc"
    assert X.normalise_lines(["zero​width"]) == "zero​width"  # kept


def test_structure_keeps_links_buttons_forms_from_the_full_page() -> None:
    _, s = X.extract("homepage", "https://velmora.example/", HOME)
    assert s["format"] == "html"
    assert s["title"] == "Velmora | Route planning for field teams"
    links = {(x["text"], x["url"], x["region"], x["in_text"]) for x in s["links"]}
    assert ("Pricing", "https://velmora.example/pricing", "nav", False) in links
    assert ("Customer stories", "https://velmora.example/customers", "nav", False) in links
    assert ("Docs", "https://velmora.example/docs", "nav", False) in links  # role=navigation
    assert ("About", "https://velmora.example/about", "footer", False) in links
    assert ("Book a demo", "https://velmora.example/demo", "header", True) in links
    home = next(x for x in s["links"] if x["href"] == "/")
    assert home["aria_label"] == "Velmora home"
    assert len(s["links"]) == 8
    buttons = {(b["text"], b["target"]) for b in s["buttons"]}
    assert ("Chat with us", None) in buttons
    assert ("Subscribe", "/subscribe") in buttons
    [form] = s["forms"]
    assert form["action"] == "/subscribe" and form["method"] == "post"
    assert form["url"] == "https://velmora.example/subscribe"
    assert form["field_names"] == ["email", "team_size"]
    assert form["field_count"] == 2 and form["hidden_count"] == 2  # hidden + submit
    email = form["fields"][0]
    assert (email["label"], email["type"], email["required"]) == ("Work email", "email", True)
    assert [f["name"] for f in s["loose_fields"]] == ["search"]
    assert s["iframes"][0]["url"] == "https://calendly.com/velmora-demo/30min"
    removed = [(r["region"], r["text"]) for r in s["removed_text"]]
    assert [r for r, _ in removed] == ["nav", "nav", "footer"]
    assert removed[0][1] == "Pricing Customer stories For logistics"
    assert "© 2026 Velmora Technologies Private Limited" in removed[2][1]


def test_job_page_form_field_count() -> None:
    text, s = X.extract("job_post", "https://jobs.velmora.example/sdr", JOB)
    assert "Build and own target account lists from scratch." in text
    assert s["forms"][0]["field_count"] == 4
    assert s["forms"][0]["field_names"] == ["first_name", "last_name", "email", "resume"]


def test_extraction_is_deterministic() -> None:
    a = X.extract("homepage", "https://velmora.example/", HOME)
    b = X.extract("homepage", "https://velmora.example/", bytes(bytearray(HOME)))
    assert a[0].encode() == b[0].encode()
    assert X.structure_bytes(a[1]) == X.structure_bytes(b[1])


def test_decoding_falls_back_to_meta_charset() -> None:
    body = b'<meta charset="iso-8859-1"><p>Caf\xe9 pricing</p>'
    text, _ = X.extract("pricing", "https://velmora.example/pricing", body)
    assert text == "Café pricing"
    assert X.extract("pricing", "u", b"\xef\xbb\xbf<p>BOM</p>")[0] == "BOM"


def test_dns_rendering_is_verbatim_lines() -> None:
    text, s = X.extract("dns", "dns:velmora.example", DNS_BODY)
    assert text.split("\n") == [
        'TXT velmora.example "v=spf1 include:_spf.google.com ~all"',
        'TXT velmora.example "google-site-verification=abc123"',
        'TXT _dmarc.velmora.example "v=DMARC1; p=none; rua=mailto:dmarc@velmora.example"',
        "MX velmora.example 1 aspmx.l.google.com",
        "MX velmora.example 5 alt1.aspmx.l.google.com",
        'TXT google._domainkey.velmora.example "v=DKIM1; k=rsa; p=MIIBIjAN"',
        "NXDOMAIN selector1._domainkey.velmora.example",
        "NODATA TXT k1._domainkey.velmora.example",
        "CNAME em.velmora.example u123.wl.sendgrid.net",
        "NXDOMAIN mail.velmora.example",
        "FAILED TXT sg.velmora.example: timeout after 3s",
    ]
    d = s["dns"]
    assert d["spf"] == ["v=spf1 include:_spf.google.com ~all"]
    assert d["dmarc_policy"] == "none"
    assert d["mx"] == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
    assert d["dkim_selectors"] == ["google"]
    assert d["sending_subdomains"] == {"em": ["CNAME u123.wl.sendgrid.net"]}
    assert d["verification_txt"] == ["google-site-verification=abc123"]
    assert d["failed"] == ["TXT sg.velmora.example"]


@pytest.mark.parametrize("name, title, phrase", [
    ("greenhouse_zentrico.json", "Founding Sales Development Representative", None),
    ("lever_quillbyte.json", "Founding Account Executive", "What you will do"),
    ("ashby_norvale.json", "GTM Engineer", "Norvale makes fleet maintenance software."),
    ("workable_tessaro.json", "Business Development Representative",
     "Book first meetings with clinic owners across North India"),
])
def test_job_board_json_renders_description_text(name, title, phrase) -> None:
    body = (FIX / "ats" / name).read_bytes()
    text, s = X.extract("ats_board", "https://boards.example/x", body)
    assert s["format"] == "json_jobs" and s["job_count"] >= 1
    lines = text.split("\n")
    assert title in lines
    assert s["jobs"][0]["title"] == title
    for bad in ("<p>", "<li>", "&lt;", "&quot;", "&amp;nbsp;"):
        assert bad not in text
    if phrase:
        assert phrase in text


def test_greenhouse_content_is_unescaped_then_converted() -> None:
    payload = {"jobs": [{"title": "Founding AE", "location": {"name": "Remote, India"},
                         "absolute_url": "https://job-boards.example/acme/1",
                         "content": "&lt;p&gt;Own the &lt;strong&gt;full&lt;/strong&gt; "
                                    "cycle.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Close deals"
                                    "&lt;/li&gt;&lt;/ul&gt;"}]}
    text, s = X.extract("ats_board", "u", json.dumps(payload).encode())
    assert text == "Founding AE\nRemote, India\nOwn the full cycle.\nClose deals"
    assert s["jobs"] == [{"title": "Founding AE", "url": "https://job-boards.example/acme/1",
                          "location": "Remote, India"}]


def test_binary_body_gives_empty_text() -> None:
    assert X.extract("pricing", "u", b"%PDF-1.7 \x00\x01") == ("", {"format": "binary"})


# -- over the database -----------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No socket may connect and no model may be called."""
    from engine.guard.budget import Budget
    from engine.llm import gateway

    def no_net(*a, **kw):
        raise AssertionError("extract touched the network")

    def no_model(*a, **kw):
        raise AssertionError("extract made a model call")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(gateway, "call", no_model)
    monkeypatch.setattr(Budget, "charge_model_call", no_model)


def _art(kind: str, url: str, outcome: str = "ok", sha: str | None = None,
         fetcher: str = "http", detail: str | None = None) -> Artifact:
    return Artifact(company_id=CID, kind=kind, url=url, outcome=outcome, fetched_at=WHEN,
                    body_sha256=sha, fetcher=fetcher, detail=detail, status=200 if sha else None,
                    bundle_version="bundle-1.0")


@pytest.fixture
def bundle(conn: sqlite3.Connection, cache: Cache) -> str:
    add_company(conn, CID, name="Velmora", domain="velmora.example", status="captured",
                pilot=True)
    board = (FIX / "ats" / "ashby_norvale.json").read_bytes()
    write_rows(conn, [
        _art("homepage", "https://velmora.example/", sha=cache.put_body(HOME)),
        _art("pricing", "https://velmora.example/pricing", "not_found", fetcher="none",
             detail="no matching link in nav, footer or sitemap"),
        _art("customers", "https://velmora.example/customers", "blocked",
             sha=cache.put_body(b"<p>Access denied</p>"), detail="HTTP 403"),
        _art("job_post", "https://jobs.velmora.example/sdr", sha=cache.put_body(JOB)),
        _art("ats_board", "https://api.ashbyhq.example/posting-api/job-board/velmora",
             sha=cache.put_body(board)),
        _art("dns", "dns:velmora.example", sha=cache.put_body(DNS_BODY), fetcher="dns"),
    ])
    return CID


def _derived(conn: sqlite3.Connection) -> dict[str, tuple]:
    return {r["kind"]: (r["text_sha256"], r["structure_sha256"], r["extractor_version"])
            for r in conn.execute("SELECT * FROM artifacts")}


def test_run_extracts_ok_artifacts_only(conn, cache, bundle, offline) -> None:
    rep = X.run(conn, cache, [bundle])
    assert (rep.considered, rep.written, rep.current, rep.drift) == (4, 4, 0, [])
    d = _derived(conn)
    assert d["pricing"] == (None, None, None) and d["customers"] == (None, None, None)
    row = conn.execute("SELECT * FROM artifacts WHERE kind = 'homepage'").fetchone()
    text = X.stored_text(cache, row)
    assert text == X.extract("homepage", row["url"], HOME)[0]
    assert row["text_sha256"] == X.sha256_hex(text.encode("utf-8"))
    st = X.stored_structure(cache, row)
    assert st["extractor_version"] == X.EXTRACTOR_VERSION
    assert st["source_sha256"] == row["body_sha256"] and st["text_sha256"] == row["text_sha256"]
    assert (cache.root / "text" / row["text_sha256"]).is_file()


def test_run_is_idempotent(conn, cache, bundle, offline) -> None:
    X.run(conn, cache, [bundle])
    before = [tuple(r) for r in conn.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    rep = X.run(conn, cache, [bundle])
    assert (rep.written, rep.current, rep.reproduced) == (0, 4, 0)
    rep = X.run(conn, cache, [bundle], reextract=True)
    assert (rep.written, rep.reproduced, rep.drift) == (0, 4, [])
    assert [tuple(r) for r in conn.execute("SELECT * FROM artifacts ORDER BY artifact_id")] \
        == before


def test_reextract_from_cache_reproduces_identical_hashes(conn, cache, bundle, offline) -> None:
    """Gate 3.1: re-extracting from cache reproduces identical hashes."""
    X.run(conn, cache, [bundle])
    first = _derived(conn)
    shutil.rmtree(cache.root / "text")
    shutil.rmtree(cache.root / "structure")
    rep = X.run(conn, cache, [bundle], reextract=True)
    assert (rep.reproduced, rep.written, rep.drift, rep.missing_body) == (4, 0, [], 0)
    assert _derived(conn) == first
    for kind, (t_sha, s_sha, _) in first.items():
        if t_sha:
            assert X.sha256_hex((cache.root / "text" / t_sha).read_bytes()) == t_sha
            assert X.sha256_hex((cache.root / "structure" / s_sha).read_bytes()) == s_sha


def test_version_bump_rederives_from_cache_with_zero_requests(conn, cache, bundle, offline,
                                                              monkeypatch) -> None:
    X.run(conn, cache, [bundle])
    first = _derived(conn)
    monkeypatch.setattr(X, "EXTRACTOR_VERSION", "ex-test-bump")
    rep = X.run(conn, cache, [bundle])
    assert (rep.written, rep.current, rep.missing_body) == (4, 0, 0)
    after = _derived(conn)
    for kind, (t_sha, s_sha, version) in after.items():
        if first[kind][0] is None:
            continue
        assert version == "ex-test-bump"
        assert t_sha == first[kind][0]  # this bump changed no extraction rule
        assert s_sha != first[kind][1]  # the structure records its version
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_drift_under_the_same_version_is_reported_not_written(conn, cache, bundle,
                                                              offline) -> None:
    X.run(conn, cache, [bundle])
    fake = "f" * 64
    cache.put_derived("text", b"")  # make sure a file for some other hash exists
    with conn:
        conn.execute("UPDATE artifacts SET text_sha256 = ? WHERE kind = 'homepage'", (fake,))
    rep = X.run(conn, cache, [bundle], reextract=True)
    assert rep.drift == [f"{CID}/homepage"]
    assert _derived(conn)["homepage"][0] == fake


def test_missing_body_is_reported_and_not_fetched(conn, cache, bundle, offline) -> None:
    row = conn.execute("SELECT body_sha256 FROM artifacts WHERE kind = 'job_post'").fetchone()
    cache.body_path(row[0]).unlink()
    rep = X.run(conn, cache, [bundle])
    assert rep.missing == [f"{CID}/job_post"] and rep.written == 3
    assert _derived(conn)["job_post"] == (None, None, None)


def test_changed_body_clears_derived_columns(conn, cache, bundle, offline) -> None:
    X.run(conn, cache, [bundle])
    write_rows(conn, [_art("homepage", "https://velmora.example/",
                           sha=cache.put_body(HOME + b"<p>new</p>"))])
    assert _derived(conn)["homepage"] == (None, None, None)
    write_rows(conn, [_art("job_post", "https://jobs.velmora.example/sdr",
                           sha=cache.put_body(JOB), detail="same body")])
    assert _derived(conn)["job_post"][2] == X.EXTRACTOR_VERSION  # same body keeps them


def test_select_companies(conn, bundle) -> None:
    add_company(conn, "orvane-example", name="Orvane", domain="orvane.example")
    with conn:
        conn.execute("INSERT INTO artifacts (company_id, kind, url, outcome, fetched_at) "
                     "VALUES ('orvane-example', 'homepage', 'https://orvane.example/', "
                     "'dead', '2026-11-10T08:00:00Z')")
    assert X.select_companies(conn) == ["orvane-example", CID]
    assert X.select_companies(conn, pilot=True) == [CID]
    assert X.select_companies(conn, limit=1) == ["orvane-example"]
    with pytest.raises(X.ExtractError):
        X.select_companies(conn, company_id="nobody-example")


def test_cli_extract_text(db_file, cache, offline) -> None:
    conn = db.connect(db_file)
    _bundle_rows(conn, cache)
    conn.close()
    args = ["extract", "text", "--db", str(db_file), "--cache-dir", str(cache.root)]
    r = CliRunner().invoke(main, args + ["--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would extract 4" in r.output
    assert not (cache.root / "text").exists()
    r = CliRunner().invoke(main, args + ["--pilot"])
    assert r.exit_code == 0, r.output
    assert "written 4" in r.output
    r = CliRunner().invoke(main, args + ["--company", CID, "--reextract"])
    assert r.exit_code == 0, r.output
    assert "written 0" in r.output and "re-derived identical 4" in r.output
    r = CliRunner().invoke(main, args + ["--company", "nobody-example"])
    assert r.exit_code == 1


def _bundle_rows(conn: sqlite3.Connection, cache: Cache) -> None:
    add_company(conn, CID, name="Velmora", domain="velmora.example", status="captured",
                pilot=True)
    write_rows(conn, [
        _art("homepage", "https://velmora.example/", sha=cache.put_body(HOME)),
        _art("pricing", "https://velmora.example/pricing", "not_found", fetcher="none"),
        _art("job_post", "https://jobs.velmora.example/sdr", sha=cache.put_body(JOB)),
        _art("ats_board", "https://boards.example/velmora",
             sha=cache.put_body((FIX / "ats" / "lever_quillbyte.json").read_bytes())),
        _art("dns", "dns:velmora.example", sha=cache.put_body(DNS_BODY), fetcher="dns"),
    ])


# -- te show ---------------------------------------------------------------------------


@pytest.fixture
def contact(conn: sqlite3.Connection, bundle: str) -> None:
    # An invented contact whose name also appears in a page's text.
    with conn:
        db.insert(conn, Contact(company_id=CID, name="Dorrin Vasquel", title="Revenue Steward",
                                email="dorrin@velmora.example", channel="email"))


def test_show_prints_bundle_text_and_dns(conn, cache, bundle, offline) -> None:
    X.run(conn, cache, [bundle])
    out = "\n".join(show_lines(conn, CID, cache, lines=3))
    assert out.startswith(f"{CID}  Velmora  velmora.example  status captured")
    assert "[pilot]" in out
    assert "bundle bundle-1.0: 6 artifacts; ok 4, not_found 1, blocked 1, dead 0" in out
    assert "kinds with no row: product, about, careers, contact, demo_form, compare" in out
    assert f"extracted at {X.EXTRACTOR_VERSION}: 4 of 4 ok" in out
    assert "homepage   ok            http       https://velmora.example/" in out
    assert "pricing    not_found     none       https://velmora.example/pricing" in out
    assert "  no matching link in nav, footer or sitemap" in out
    assert "customers  blocked" in out and "Access denied" not in out
    home_lines = X.stored_text(cache, conn.execute(
        "SELECT * FROM artifacts WHERE kind = 'homepage'").fetchone()).split("\n")
    for line in home_lines[:3]:
        assert f"  | {line}" in out
    assert f"  | {home_lines[3]}" not in out
    assert f"  | ... {len(home_lines) - 3} more lines" in out
    assert "links 8, buttons 2, forms 1 (fields 2)" in out
    assert "jobs " in out
    assert "  SPF:   v=spf1 include:_spf.google.com ~all" in out
    assert "  DMARC: policy none" in out
    assert "  MX:    aspmx.l.google.com, alt1.aspmx.l.google.com" in out
    assert "  DKIM selectors resolving: google" in out
    assert "  sending subdomains resolving: em (CNAME u123.wl.sendgrid.net)" in out
    assert "  lookups failed: TXT sg.velmora.example" in out


def test_show_before_extraction_says_so(conn, cache, bundle, offline) -> None:
    out = "\n".join(show_lines(conn, CID, cache))
    assert "extracted at ex-1.0: 0 of 4 ok" in out
    assert "(not extracted at this version; run `te extract text`)" in out
    assert "  DMARC: policy none" in out  # DNS summary read from the raw JSON


def test_show_never_prints_contacts(conn, cache, bundle, contact, offline) -> None:
    body = b"<h1>Team</h1><p>Dorrin Vasquel founded Velmora. Write to dorrin@velmora.example</p>"
    with conn:
        conn.execute("UPDATE artifacts SET body_sha256 = ? WHERE kind = 'homepage'",
                     (cache.put_body(body),))
    X.run(conn, cache, [bundle])
    out = "\n".join(show_lines(conn, CID, cache, lines=50))
    for value in ("Dorrin", "Vasquel", "dorrin@velmora.example", "Revenue Steward"):
        assert value.lower() not in out.lower()
    assert "[redacted] founded Velmora" in out


def test_cli_show(db_file, cache, offline) -> None:
    conn = db.connect(db_file)
    _bundle_rows(conn, cache)
    conn.close()
    base = ["--db", str(db_file), "--cache-dir", str(cache.root)]
    assert CliRunner().invoke(main, ["extract", "text", *base]).exit_code == 0
    r = CliRunner().invoke(main, ["show", CID, "--lines", "1", *base])
    assert r.exit_code == 0, r.output
    assert "  | Route planning for field teams" in r.output
    assert "  | Plan every visit in one place." not in r.output
    assert "DNS velmora.example" in r.output
    r = CliRunner().invoke(main, ["show", "nobody-example", *base])
    assert r.exit_code == 1


# -- debug export ----------------------------------------------------------------------


def test_debug_export_uses_extracted_text(conn, cache, bundle, offline, tmp_path) -> None:
    X.run(conn, cache, [bundle])
    dest = export_company(conn, CID, tmp_path / "debug", cache)
    record = json.loads((dest / "record.json").read_text(encoding="utf-8"))
    home = next(a for a in record["artifacts"] if a["kind"] == "homepage")
    assert home["text_note"] == f"extracted text ({X.EXTRACTOR_VERSION})"
    text = (dest / home["text_file"]).read_text(encoding="utf-8")
    assert "SCRIPT_MARKER" not in text and "Route planning for field teams" in text
    st = json.loads((dest / home["structure_file"]).read_text(encoding="utf-8"))
    assert st["forms"][0]["field_count"] == 2
    blocked = next(a for a in record["artifacts"] if a["kind"] == "customers")
    assert blocked["text_note"].startswith("raw body")  # not extracted: raw fallback
