"""dns_inventory: the public DNS sending inventory (protocol/GAPS.md v1.2, G6
provisional; protocol/HAND_NOTES.md "The proof slice": the slice was the
teardown). Zero network, zero model calls: it reads the company's stored dns
artifact (capture's fixed lookup list, rendered one line per answer by
engine/capture/extract_text.py dns_lines).

Rows, in order: SPF, DMARC, MX, every DKIM selector on the fixed list that
resolves, then one "not found" row for the selectors that do not, the same for
the fixed sending subdomains. Each row's quote is the rendered DNS line(s)
verbatim; its url is the dns artifact's (dns:<domain>), which the renderer
shows as "public DNS records for <domain>".

- A name answered NXDOMAIN or NODATA is not found, and the row quotes those
  lines. A name whose lookup FAILED was not seen: it is left out (the SPF,
  DMARC or MX row is dropped; a selector or subdomain is left out of the
  not-found row), never written as absent.
- Not-found names are grouped into rows of contiguous lines, each quote under
  25 words.
- Values describe the records only: what SPF authorises, the DMARC policy, the
  mail hosts. Nothing about sending volume (HAND_NOTES "What could not be
  learned from outside").
"""

from __future__ import annotations

from engine.build.slices import common as C
from engine.capture import bundle as B
from engine.extract import dns as D
from engine.extract.base import Art, Bundle

KIND = "dns_inventory"
VERSION = "sl-dns-1.0"
MIN_ROWS = 3
COLUMNS = ["Record", "Name looked up", "What public DNS shows"]


def title(domain: str) -> str:
    return f"Public DNS sending inventory for {domain}"


def _src(art: Art, subject: str, lines: list[D.Line]) -> C.Src:
    text = "\n".join(x.text for x in lines)
    return C.Src("company", subject, "dns", art.url, text, "text", lines[0].start, True)


def _contiguous(all_lines: list[D.Line], picked: list[D.Line]) -> bool:
    idx = [all_lines.index(x) for x in picked]
    return idx == list(range(idx[0], idx[0] + len(idx)))


def _negative(lines: list[D.Line], name: str, rtype: str | None) -> list[D.Line] | None:
    """The NXDOMAIN/NODATA lines that show name has no answer, or None when it
    has one or was not seen."""
    mine = D._for(lines, name, rtype)
    if not mine or any(x.status in ("answer", "failed") for x in mine):
        return None
    return mine


def _row(record: str, name: str, value: str, src: C.Src, *, nf: bool = False) -> C.Draft:
    """One row. A not-found row's value cell rests on the same NXDOMAIN/NODATA
    lines as its quote, marked as an absence so the cell rule accepts it."""
    if nf:
        shown = C.not_found(C.Src(src.scope, src.subject, src.kind, src.url, src.quote,
                                  src.quote_source, src.char_start, False))
    else:
        shown = C.cell(value)
    return C.Draft([C.cell(record), C.cell(name), shown], src)


def _spf_value(rec: str) -> str:
    v = D.parse_spf(rec)
    parts = [*v["senders"], *v["ips"]]
    out = "authorises " + (", ".join(parts) if parts else "no sender")
    return out + (f"; {v['all']}" if v["all"] else "")


def _dmarc_value(rec: str) -> str:
    v = D.parse_dmarc(rec)
    bits = [f"p={v['p']}" if v["p"] else "no policy"]
    if v["sp"]:
        bits.append(f"sp={v['sp']}")
    if v["pct"] is not None:
        bits.append(f"pct={v['pct']}")
    reports = [k for k in ("rua", "ruf") if v[k]]
    bits.append(("reports to " + " and ".join(reports)) if reports else "no report address")
    return ", ".join(bits)


def _grouped(art: Art, subject: str, lines: list[D.Line], label: str,
             names: list[tuple[str, str]], rtype: str | None) -> list[C.Draft]:
    """'not found' rows for the names with no answer, contiguous and under 25 words."""
    neg: list[tuple[str, list[D.Line]]] = []
    for lab, name in names:
        got = _negative(lines, name, rtype)
        if got is not None:
            neg.append((lab, got))
    out: list[C.Draft] = []
    group: list[tuple[str, list[D.Line]]] = []

    def flush() -> None:
        if not group:
            return
        picked = [x for _, ls in group for x in ls]
        src = _src(art, subject, picked)
        labels = ", ".join(lab for lab, _ in group)
        out.append(_row(label, labels, C.NOT_FOUND, src, nf=True))
        group.clear()

    for lab, ls in neg:
        trial = [x for _, g in group for x in g] + ls
        if group and (not _contiguous(lines, trial)
                      or C.words("\n".join(x.text for x in trial)) >= C.QUOTE_LIMIT):
            flush()
        group.append((lab, ls))
    flush()
    return out


def build(b: Bundle) -> C.Slice:
    """The slice from the company's bundle. Pure."""
    art = b.arts.get("dns")
    domain = (b.domain or "").lower()
    if art is None or b.state("dns") != "ok":
        state = "missing" if art is None else art.outcome
        return C.failed(KIND, VERSION, title(domain), COLUMNS,
                        f"no dns artifact to read ({state})")
    subject = D.domain_of(b, art) or domain
    lines = D.parse(art.text or "")
    drafts: list[C.Draft] = []

    # SPF: the root's TXT
    root_txt = D._for(lines, subject, "TXT")
    spf = [x for x in root_txt if x.status == "answer" and x.value.lower().startswith("v=spf1")]
    if spf:
        drafts.append(_row("SPF", subject, _spf_value(spf[0].value), _src(art, subject, spf[:1])))
    elif root_txt and not any(x.status == "failed" for x in root_txt):
        drafts.append(_row("SPF", subject, C.NOT_FOUND, _src(art, subject, root_txt), nf=True))

    # DMARC
    dname = f"_dmarc.{subject}"
    dm = D._for(lines, dname, "TXT")
    rec = [x for x in dm if x.status == "answer" and x.value.lower().startswith("v=dmarc1")]
    if rec:
        drafts.append(_row("DMARC", dname, _dmarc_value(rec[0].value),
                           _src(art, subject, rec[:1])))
    elif dm and not any(x.status == "failed" for x in dm):
        drafts.append(_row("DMARC", dname, C.NOT_FOUND, _src(art, subject, dm), nf=True))

    # MX
    mx = D._for(lines, subject, "MX")
    answers = [x for x in mx if x.status == "answer"]
    if answers and not any(x.status == "failed" for x in mx):
        hosts = ", ".join(x.value.split()[-1].rstrip(".") for x in answers if x.value.split())
        quote = answers if C.words("\n".join(x.text for x in answers)) < C.QUOTE_LIMIT \
            else answers[:1]
        drafts.append(_row("MX", subject, f"mail handled by {hosts}",
                           _src(art, subject, quote)))
    elif mx and not any(x.status == "failed" for x in mx):
        drafts.append(_row("MX", subject, C.NOT_FOUND, _src(art, subject, mx), nf=True))

    # DKIM: resolving selectors, then the rest as not found
    sel = [(s, f"{s}._domainkey.{subject}") for s in B.DKIM_SELECTORS]
    for s, name in sel:
        ans = [x for x in D._for(lines, name, "TXT") if x.status == "answer"]
        if ans:
            drafts.append(_row("DKIM selector", s, "key published", _src(art, subject, ans[:1])))
    drafts += _grouped(art, subject, lines, "DKIM selectors", sel, "TXT")

    # sending subdomains
    subs = [(s, f"{s}.{subject}") for s in B.SENDING_SUBDOMAINS]
    for s, name in subs:
        ans = [x for x in D._for(lines, name) if x.status == "answer"]
        if ans:
            first = ans[0]
            drafts.append(_row("Sending subdomain", f"{s}.{subject}",
                               f"resolves: {first.rtype} {first.value}",
                               _src(art, subject, ans[:1])))
    drafts += _grouped(art, subject, lines, "Sending subdomains", subs, None)

    def resolve(s: C.Src) -> Art | None:
        return art if s.scope == "company" and s.kind == "dns" else None

    return C.finalize(KIND, VERSION, title(subject), COLUMNS, drafts, resolve,
                      min_rows=MIN_ROWS, meta={"domain": subject,
                                               "dkim_selectors": list(B.DKIM_SELECTORS),
                                               "sending_subdomains": list(B.SENDING_SUBDOMAINS)})
