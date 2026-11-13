"""Extract text and structure from captured artifacts (docs/PLAN.md Step 35).

Pure and local: reads raw bodies from the cache by body_sha256 and writes
derived output back to the cache. Zero network, zero model calls. Changing
anything here that can change the text or the structure bumps
EXTRACTOR_VERSION; `te extract text` then re-derives every ok artifact from
the cache with no request.

What is kept, per artifact (only rows with outcome ok and a cached body):

- text      the page's visible text, as UTF-8, stored under
            cache/text/<text_sha256>. This is the text that quotes must be
            verbatim substrings of (protocol/GAPS.md "Evidence rules").
- structure a JSON document stored under cache/structure/<structure_sha256>:
            every link (text, raw href, absolute url, region), every button,
            every form with its fields and visible-field count, fields outside
            any form, iframes, the page title, and the text of the regions
            dropped from `text` (nav, footer).

HTML text rules
---------------
Dropped entirely: script, style, noscript, template, head, svg, canvas,
iframe, object, embed, audio, video, select (its options are not visible
text; the field is in the structure) and datalist.
Dropped from `text` only: nav, footer, and elements with role="navigation"
or role="contentinfo" (with everything inside them). The links, buttons and
forms in the structure are read from the FULL page, nav and footer included,
each tagged with its region and `in_text` (false when it sits inside a dropped
region), so "the nav has no pricing link" and footer links stay checkable.
The dropped regions' own text is kept in structure["removed_text"]; it is not
part of `text`, so a quote taken from a footer (an entity name, say) will not
pass a faithfulness check against `text`. Nothing is rewritten: entities are
decoded by the HTML parser, characters are otherwise kept as they are (no
Unicode normalisation, no case or quote changes).

Line breaks: each block element (BLOCK_TAGS) and <br> ends a line. Inline
elements join without adding anything, as a browser shows them.

Whitespace normalisation (the exact rule; faithfulness, Step 48, applies the
same rule, see `normalise_ws`)
-------------------------------------------------------------------------------
Within each line, every run of characters for which Python's str.isspace() is
true (space, tab, CR, LF, form feed, vertical tab, NBSP U+00A0, the U+2000
spaces, U+3000, line/paragraph separators, ...) becomes one U+0020 space, and
leading and trailing spaces are removed: " ".join(line.split()). Empty lines
are dropped. Lines are joined with a single LF (U+000A). No trailing newline.
Zero-width characters (U+200B, U+00AD soft hyphen, U+FEFF inside text) are
not whitespace under this rule and are kept.
Consequence: collapsing every whitespace run in the whole text, newlines
included, to one space (`normalise_ws`) yields the same string a reader sees;
a quote spanning two lines matches after both sides go through normalise_ws.

Decoding: UTF-8 (a BOM is dropped); if the bytes are not valid UTF-8, the
charset named in a <meta> within the first 2048 bytes, else cp1252, with
undecodable bytes replaced.

Non-HTML artifacts
------------------
- dns: one line per DNS answer, in the fixed lookup order of bundle.py:
      TXT example.com "v=spf1 include:_spf.google.com ~all"
      MX example.com 10 aspmx.l.google.com
      CNAME em.example.com u123.wl.sendgrid.net
      A em.example.com 192.0.2.1
      NODATA TXT _dmarc.example.com          (name exists, no record of that type)
      NXDOMAIN em.example.com                (name does not exist; once per name)
      FAILED TXT example.com: <detail>       (lookup failed: timeout etc.)
  TXT values are wrapped in double quotes, unescaped, multi-string records
  joined (as guard.dns stores them). The structure holds the DNS summary.
- ats_board, and any artifact whose body is JSON: the board's jobs, each as its
  title line, its location line (when the JSON has one) and its description
  converted from HTML to text by the same rules as a page (Greenhouse's
  entity-escaped `content` is unescaped first; Lever's lists and additional
  sections follow its description). The structure lists each job's title,
  url and location.
- A body that is neither (binary, PDF) yields empty text, format "binary".
"""

from __future__ import annotations

import html as htmllib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode

from engine.guard.cache import Cache, sha256_hex

EXTRACTOR_VERSION = "ex-1.0"

# Never text: not rendered, or not readable text.
DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "head", "svg", "canvas", "iframe",
    "object", "embed", "audio", "video", "select", "datalist",
})
# Dropped from `text`, kept in the structure (Step 35: navigation and footers).
REGION_TAGS = {"nav": "nav", "footer": "footer"}
REGION_ROLES = {"navigation": "nav", "contentinfo": "footer"}
# Landmarks reported as a link's region when not inside a dropped region.
LANDMARK_TAGS = ("header", "aside", "main")

BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd",
    "details", "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "legend",
    "li", "main", "menu", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul", "option", "optgroup", "button", "label",
})

# input types that are not fields a visitor fills in
NON_FIELD_INPUTS = frozenset({"hidden", "submit", "button", "reset", "image"})

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


# -- normalisation -----------------------------------------------------------------------


def normalise_lines(lines: Iterable[str]) -> str:
    """The whitespace rule (module docstring): collapse within lines, drop empty ones."""
    kept = (" ".join(line.split()) for line in lines)
    return "\n".join(line for line in kept if line)


def normalise_ws(s: str) -> str:
    """Every whitespace run (newlines included) to one space, trimmed.

    Applied to both the stored text and a quote, a quote is found in the text
    exactly when it is a verbatim substring up to whitespace.
    """
    return " ".join(s.split())


def _collapse(s: str | None) -> str:
    return " ".join((s or "").split())


def decode(body: bytes) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    m = _META_CHARSET.search(body[:2048])
    if m:
        try:
            return body.decode(m.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    return body.decode("cp1252", errors="replace")


# -- HTML --------------------------------------------------------------------------------


def _children(node: LexborNode) -> list[LexborNode]:
    out = []
    c = node.child
    while c is not None:
        out.append(c)
        c = c.next
    return out


def _region_of(node: LexborNode) -> str | None:
    """'nav' or 'footer' if this element is itself a dropped region."""
    tag = node.tag
    if tag in REGION_TAGS:
        return REGION_TAGS[tag]
    role = (node.attributes.get("role") or "").strip().lower()
    return REGION_ROLES.get(role)


def _lines(root: LexborNode, *, drop_regions: bool,
           removed: list[dict[str, str]] | None = None) -> list[str]:
    """Raw lines of text under root (not yet normalised)."""
    lines: list[str] = []
    cur: list[str] = []

    def brk() -> None:
        if cur:
            lines.append("".join(cur))
            cur.clear()

    stack: list[tuple[LexborNode, bool]] = [(root, False)]
    while stack:
        node, leaving = stack.pop()
        if leaving:
            brk()
            continue
        tag = node.tag or ""
        if tag == "-text":
            cur.append(node.text(deep=False) or "")
            continue
        if tag.startswith("-") or tag in DROP_TAGS:  # comments, doctype
            continue
        if drop_regions and node is not root:
            region = _region_of(node)
            if region is not None:
                if removed is not None:
                    text = normalise_lines(_lines(node, drop_regions=False))
                    if text:
                        removed.append({"region": region, "text": text})
                brk()
                continue
        if tag == "br":
            brk()
            continue
        if tag in BLOCK_TAGS:
            brk()
            stack.append((node, True))
        stack.extend((c, False) for c in reversed(_children(node)))
    brk()
    return lines


def _ancestry(node: LexborNode) -> tuple[str, bool]:
    """(region, in_text): the nearest dropped region or landmark, and whether
    the element's text reaches `text` (not inside a dropped region or a
    DROP_TAGS element)."""
    region: str | None = None
    in_text = True
    p = node.parent
    while p is not None and p.tag not in ("html", "-document"):
        r = _region_of(p)
        if r is not None:
            in_text = False
            region = region or r
        elif p.tag in LANDMARK_TAGS and region is None:
            region = p.tag
        if p.tag in DROP_TAGS:
            in_text = False
        p = p.parent
    return region or "body", in_text


def _attr(node: LexborNode, name: str) -> str | None:
    v = node.attributes.get(name)
    return v if isinstance(v, str) else None


def _url(base: str, href: str | None) -> str | None:
    if href is None:
        return None
    href = href.strip()
    if not href:
        return None
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def _field(node: LexborNode, labels: dict[str, str]) -> dict[str, Any]:
    tag = node.tag or ""
    ftype = (_attr(node, "type") or ("text" if tag == "input" else tag)).strip().lower()
    fid = _attr(node, "id")
    label = labels.get(fid or "") if fid else None
    if label is None:
        p = node.parent
        while p is not None and p.tag not in ("form", "body", "html"):
            if p.tag == "label":
                label = _collapse(p.text(deep=True))
                break
            p = p.parent
    out: dict[str, Any] = {
        "tag": tag,
        "type": ftype,
        "name": _attr(node, "name"),
        "id": fid,
        "label": label or None,
        "placeholder": _attr(node, "placeholder"),
        "aria_label": _attr(node, "aria-label"),
        "required": "required" in node.attributes,
        "visible": not (tag == "input" and ftype in NON_FIELD_INPUTS),
    }
    return out


def _inside(node: LexborNode, tag: str) -> LexborNode | None:
    p = node.parent
    while p is not None:
        if p.tag == tag:
            return p
        p = p.parent
    return None


def html_structure(tree: LexborHTMLParser, base: str) -> dict[str, Any]:
    """Links, buttons, forms, fields and iframes from the full page."""
    base_tag = tree.css_first("base[href]")
    if base_tag is not None:
        base = _url(base, _attr(base_tag, "href")) or base
    title_node = tree.css_first("title")
    labels: dict[str, str] = {}
    for lab in tree.css("label[for]"):
        key = _attr(lab, "for")
        if key and key not in labels:
            labels[key] = _collapse(lab.text(deep=True))

    links = []
    for a in tree.css("a[href]"):
        region, in_text = _ancestry(a)
        href = _attr(a, "href")
        links.append({"text": _collapse(a.text(deep=True)), "href": href,
                      "url": _url(base, href), "aria_label": _attr(a, "aria-label"),
                      "region": region, "in_text": in_text})

    forms_nodes = tree.css("form")
    form_index = {f.mem_id: i for i, f in enumerate(forms_nodes)}

    def which_form(node: LexborNode) -> int | None:
        f = _inside(node, "form")
        return form_index.get(f.mem_id) if f is not None else None

    buttons = []
    for b in tree.css("button, input[type=submit], input[type=button], input[type=image], "
                      "[role=button]"):
        region, in_text = _ancestry(b)
        text = _collapse(b.text(deep=True)) if b.tag != "input" else _collapse(_attr(b, "value"))
        fi = which_form(b)
        target = _attr(b, "formaction") or _attr(b, "href")
        if target is None and fi is not None:
            target = _attr(forms_nodes[fi], "action")
        buttons.append({"tag": b.tag, "type": (_attr(b, "type") or "").lower() or None,
                        "text": text, "aria_label": _attr(b, "aria-label"),
                        "target": target, "url": _url(base, target), "form": fi,
                        "region": region, "in_text": in_text})

    forms = []
    for i, f in enumerate(forms_nodes):
        region, in_text = _ancestry(f)
        fields = [_field(n, labels) for n in f.css("input, select, textarea")]
        action = _attr(f, "action")
        forms.append({"index": i, "id": _attr(f, "id"), "name": _attr(f, "name"),
                      "action": action, "url": _url(base, action),
                      "method": (_attr(f, "method") or "get").strip().lower(),
                      "fields": fields,
                      "field_names": [x["name"] for x in fields if x["visible"] and x["name"]],
                      "field_count": sum(1 for x in fields if x["visible"]),
                      "hidden_count": sum(1 for x in fields if not x["visible"]),
                      "region": region, "in_text": in_text})

    loose = [_field(n, labels) for n in tree.css("input, select, textarea")
             if _inside(n, "form") is None]

    iframes = []
    for fr in tree.css("iframe"):
        region, _ = _ancestry(fr)
        src = _attr(fr, "src")
        iframes.append({"src": src, "url": _url(base, src), "title": _attr(fr, "title"),
                        "region": region})

    return {
        "title": _collapse(title_node.text(deep=True)) if title_node is not None else None,
        "links": links,
        "buttons": buttons,
        "forms": forms,
        "form_count": len(forms),
        "loose_fields": loose,
        "iframes": iframes,
    }


def html_text(markup: str, *, drop_regions: bool = True,
              removed: list[dict[str, str]] | None = None) -> str:
    tree = LexborHTMLParser(markup)
    root = tree.body or tree.root
    if root is None:
        return ""
    return normalise_lines(_lines(root, drop_regions=drop_regions, removed=removed))


def extract_html(markup: str, base: str) -> tuple[str, dict[str, Any]]:
    tree = LexborHTMLParser(markup)
    structure = html_structure(tree, base)
    removed: list[dict[str, str]] = []
    root = tree.body or tree.root
    text = normalise_lines(_lines(root, drop_regions=True, removed=removed)) if root else ""
    structure["removed_text"] = removed
    return text, {"format": "html", **structure}


# -- JSON job boards ---------------------------------------------------------------------


def _fragment_lines(markup: str | None) -> list[str]:
    if not isinstance(markup, str) or not markup.strip():
        return []
    return html_text(markup, drop_regions=False).split("\n")


def _location(job: dict[str, Any]) -> str | None:
    loc = job.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc
    if isinstance(loc, dict) and isinstance(loc.get("name"), str):
        return loc["name"]
    cats = job.get("categories")
    if isinstance(cats, dict) and isinstance(cats.get("location"), str):
        return cats["location"]
    parts = [job.get(k) for k in ("city", "state", "country")]
    joined = ", ".join(p for p in parts if isinstance(p, str) and p.strip())
    return joined or None


def _job_url(job: dict[str, Any]) -> str | None:
    for k in ("absolute_url", "hostedUrl", "jobUrl", "url", "shortlink"):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _job_lines(job: dict[str, Any]) -> list[str]:
    if isinstance(job.get("content"), str):  # Greenhouse: entity-escaped HTML
        return _fragment_lines(htmllib.unescape(job["content"]))
    if isinstance(job.get("descriptionHtml"), str):  # Ashby
        return _fragment_lines(job["descriptionHtml"])
    lines: list[str] = []
    if isinstance(job.get("description"), str):  # Lever, Workable
        lines += _fragment_lines(job["description"])
    elif isinstance(job.get("descriptionPlain"), str):
        lines += job["descriptionPlain"].splitlines()
    for item in job.get("lists") or []:  # Lever
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                lines.append(item["text"])
            lines += _fragment_lines(item.get("content"))
    for k in ("requirements", "benefits"):  # Workable, when present
        lines += _fragment_lines(job.get(k))
    if isinstance(job.get("additional"), str):  # Lever
        lines += _fragment_lines(job["additional"])
    elif isinstance(job.get("additionalPlain"), str):
        lines += job["additionalPlain"].splitlines()
    return lines


def _jobs_of(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        jobs = payload
    elif isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        jobs = payload["jobs"]
    elif isinstance(payload, dict) and isinstance(payload.get("title"), str):
        jobs = [payload]  # a single job's JSON
    else:
        return None
    return [j for j in jobs if isinstance(j, dict)]


def extract_jobs(payload: Any) -> tuple[str, dict[str, Any]]:
    jobs = _jobs_of(payload)
    if jobs is None:
        return "", {"format": "json_unknown"}
    lines: list[str] = []
    listed = []
    for job in jobs:
        title = job.get("title") if isinstance(job.get("title"), str) else job.get("text")
        title = title if isinstance(title, str) else None
        loc = _location(job)
        if title:
            lines.append(title)
        if loc:
            lines.append(loc)
        lines += _job_lines(job)
        listed.append({"title": _collapse(title) or None, "url": _job_url(job),
                       "location": _collapse(loc) or None})
    return normalise_lines(lines), {"format": "json_jobs", "jobs": listed,
                                    "job_count": len(listed)}


# -- DNS ---------------------------------------------------------------------------------


def dns_lines(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    nx: set[str] = set()
    for e in doc.get("lookups") or []:
        name, rtype = str(e.get("name")), str(e.get("type"))
        outcome, detail = e.get("outcome"), str(e.get("detail") or "")
        if outcome == "ok":
            for r in e.get("records") or []:
                out.append(f'{rtype} {name} "{r}"' if rtype == "TXT" else f"{rtype} {name} {r}")
        elif outcome == "not_found":
            if detail == "NXDOMAIN" or detail.startswith("not sent: name is NXDOMAIN"):
                if name not in nx:
                    nx.add(name)
                    out.append(f"NXDOMAIN {name}")
            else:
                out.append(f"NODATA {rtype} {name}")
        else:
            out.append(f"FAILED {rtype} {name}: {detail}" if detail else f"FAILED {rtype} {name}")
    return out


def dns_summary(doc: dict[str, Any]) -> dict[str, Any]:
    """SPF, DMARC policy, MX hosts, other root TXT, resolving DKIM selectors and
    sending subdomains, read from the dns artifact's JSON."""
    domain = str(doc.get("domain") or "")
    spf: list[str] = []
    verification: list[str] = []
    dmarc: list[str] = []
    mx: list[str] = []
    dkim: list[str] = []
    subs: dict[str, list[str]] = {}
    failed: list[str] = []
    for e in doc.get("lookups") or []:
        name, rtype, purpose = str(e.get("name")), str(e.get("type")), e.get("purpose")
        records = [str(r) for r in e.get("records") or []]
        if e.get("outcome") == "dead":
            failed.append(f"{rtype} {name}")
        if e.get("outcome") != "ok":
            continue
        if purpose == "spf":
            for r in records:
                (spf if r.lower().startswith("v=spf1") else verification).append(r)
        elif purpose == "dmarc":
            dmarc += [r for r in records if r.lower().startswith("v=dmarc1")]
        elif purpose == "mx":
            mx += [r.split()[-1] for r in records if r.split()]
        elif purpose == "dkim":
            sel = name.split("._domainkey.", 1)[0]
            if sel not in dkim:
                dkim.append(sel)
        elif purpose == "sending_subdomain":
            sub = name[: -len(domain) - 1] if domain and name.endswith("." + domain) else name
            subs.setdefault(sub, []).extend(f"{rtype} {r}" for r in records)
    policy = None
    if dmarc:
        m = re.search(r"(?:^|;)\s*p\s*=\s*([A-Za-z]+)", dmarc[0])
        policy = m.group(1).lower() if m else None
    return {"domain": domain, "spf": spf, "dmarc": dmarc, "dmarc_policy": policy,
            "mx": mx, "verification_txt": verification, "dkim_selectors": dkim,
            "sending_subdomains": subs, "failed": failed}


def extract_dns(doc: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(doc, dict):
        return "", {"format": "json_unknown"}
    return normalise_lines(dns_lines(doc)), {"format": "dns", "dns": dns_summary(doc)}


# -- dispatch ----------------------------------------------------------------------------


def _json(body: bytes) -> Any:
    head = body.lstrip()[:1]
    if head not in (b"{", b"["):
        return None
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _binary(body: bytes) -> bool:
    return body.startswith(b"%PDF") or b"\x00" in body[:4096]


def extract(kind: str, url: str, body: bytes) -> tuple[str, dict[str, Any]]:
    """(text, structure) for one artifact body. Deterministic: the same input
    gives byte-identical output."""
    if kind == "dns":
        text, structure = extract_dns(_json(body))
    elif (payload := _json(body)) is not None:
        text, structure = extract_jobs(payload)
    elif _binary(body):
        text, structure = "", {"format": "binary"}
    else:
        text, structure = extract_html(decode(body), url)
    return text, structure


def structure_bytes(structure: dict[str, Any]) -> bytes:
    return json.dumps(structure, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# -- running over the artifacts table ----------------------------------------------------


@dataclass
class Derived:
    text_sha256: str
    structure_sha256: str
    chars: int


def derive(cache: Cache, kind: str, url: str, body_sha256: str, body: bytes) -> Derived:
    """Extract, store text and structure in the cache, return their hashes."""
    text, structure = extract(kind, url, body)
    tb = text.encode("utf-8")
    t_sha = sha256_hex(tb)
    structure = {**structure, "extractor_version": EXTRACTOR_VERSION,
                 "source_sha256": body_sha256, "text_sha256": t_sha, "kind": kind}
    cache.put_derived("text", tb)
    s_sha = cache.put_derived("structure", structure_bytes(structure))
    return Derived(t_sha, s_sha, len(text))


@dataclass
class Report:
    companies: int = 0
    considered: int = 0  # ok artifacts with a body hash
    current: int = 0  # already extracted at this version, files present: skipped
    written: int = 0  # rows whose derived columns changed
    reproduced: int = 0  # re-derived under the same version: identical hashes
    missing_body: int = 0  # body not in the cache: left alone, nothing fetched
    drift: list[str] = field(default_factory=list)  # same version, different hash
    missing: list[str] = field(default_factory=list)


class ExtractError(RuntimeError):
    pass


def select_companies(conn: sqlite3.Connection, *, company_id: str | None = None,
                     pilot: bool = False, limit: int | None = None) -> list[str]:
    if company_id is not None:
        if conn.execute("SELECT 1 FROM companies WHERE company_id = ?",
                        (company_id,)).fetchone() is None:
            raise ExtractError(f"no company {company_id!r}")
        return [company_id]
    sql = ("SELECT DISTINCT c.company_id FROM companies c JOIN artifacts a "
           "ON a.company_id = c.company_id")
    if pilot:
        sql += " WHERE c.pilot = 1 OR c.hand = 1"
    ids = [r[0] for r in conn.execute(sql + " ORDER BY c.company_id")]
    return ids[:limit] if limit is not None else ids


def _rows(conn: sqlite3.Connection, company_ids: list[str]) -> Iterator[sqlite3.Row]:
    for cid in company_ids:
        yield from conn.execute(
            "SELECT * FROM artifacts WHERE company_id = ? AND outcome = 'ok' "
            "AND body_sha256 IS NOT NULL ORDER BY artifact_id", (cid,))


def is_current(cache: Cache, row: sqlite3.Row) -> bool:
    return (row["extractor_version"] == EXTRACTOR_VERSION
            and row["text_sha256"] is not None and row["structure_sha256"] is not None
            and cache.derived_path("text", row["text_sha256"]).is_file()
            and cache.derived_path("structure", row["structure_sha256"]).is_file())


def run(conn: sqlite3.Connection, cache: Cache, company_ids: list[str], *,
        reextract: bool = False, dry_run: bool = False) -> Report:
    """Extract every ok artifact of these companies from the cache.

    Idempotent: a row already at EXTRACTOR_VERSION with its files in the cache
    is skipped; with reextract it is re-derived and must reproduce the same
    hashes, else it is reported as drift and left as it was (a changed
    extractor must bump its version). Never fetches: a body missing from the
    cache is reported and the row left alone.
    """
    rep = Report(companies=len(company_ids))
    for row in list(_rows(conn, company_ids)):
        rep.considered += 1
        label = f"{row['company_id']}/{row['kind']}"
        current = is_current(cache, row)
        if current and not reextract:
            rep.current += 1
            continue
        body = cache.get_body(row["body_sha256"])
        if body is None or sha256_hex(body) != row["body_sha256"]:
            rep.missing_body += 1
            rep.missing.append(label)
            continue
        if dry_run:
            rep.written += 0 if current else 1
            continue
        d = derive(cache, row["kind"], row["url"], row["body_sha256"], body)
        same = (row["text_sha256"] == d.text_sha256
                and row["structure_sha256"] == d.structure_sha256
                and row["extractor_version"] == EXTRACTOR_VERSION)
        if same:
            rep.reproduced += 1
            continue
        if row["extractor_version"] == EXTRACTOR_VERSION and row["text_sha256"] is not None:
            rep.drift.append(label)
            continue
        with conn:
            conn.execute("UPDATE artifacts SET text_sha256 = ?, structure_sha256 = ?, "
                         "extractor_version = ? WHERE artifact_id = ?",
                         (d.text_sha256, d.structure_sha256, EXTRACTOR_VERSION,
                          row["artifact_id"]))
        rep.written += 1
    return rep


# -- reading back ------------------------------------------------------------------------


def stored_text(cache: Cache, row: sqlite3.Row | dict[str, Any]) -> str | None:
    """The extracted text of an artifact row, or None if not extracted (or missing)."""
    if not row["extractor_version"] or not row["text_sha256"]:
        return None
    data = cache.get_derived("text", row["text_sha256"])
    return data.decode("utf-8") if data is not None else None


def stored_structure(cache: Cache, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    if not row["extractor_version"] or not row["structure_sha256"]:
        return None
    data = cache.get_derived("structure", row["structure_sha256"])
    return json.loads(data.decode("utf-8")) if data is not None else None
