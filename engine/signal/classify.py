"""Title classifier: role family and offer type for each signal (docs/PLAN.md Step 24).

    role_family(title)  regex rules, then the cached small model for the rest
    offer_type(family, title)

Families: sdr, ae, sales_lead, revops, gtm_engineer, growth, demand_gen, not_gtm.

1. Rules. The title is lower-cased, apostrophes dropped ("founder's" ->
   "founders"), "&" read as "and", every other non-alphanumeric run turned into
   one space. The ordered rules in RULES then run; the first match wins. Order
   matters: specific beats generic, and a non-GTM discipline beats a GTM word
   ("Sales Engineer", "Product Designer, Growth", "Sales Recruiter" are not_gtm).

   | Order | Rule (on the normalised title)                                           | Family       |
   | ----- | ------------------------------------------------------------------------ | ------------ |
   | 1     | gtm / go to market (+ up to 2 words) + engineer / engineering / developer / automation; revenue / sales automation / marketing automation + engineer / developer | gtm_engineer |
   | 2     | growth (+ up to 2 words) + engineer / engineering / developer, or engineer / developer ... growth | growth |
   | 3     | salesforce / hubspot / crm / zoho / marketo / pardot + admin / developer / consultant / ... | revops |
   | 4     | non-GTM disciplines: pre-sales, solutions consultant / architect / engineer, customer success / support / experience / service, technical account manager, implementation, onboarding; accounts (finance, not "key accounts") executive / manager, accountant, finance, payroll, tax, audit; HR, recruiting, talent, people ops; legal, compliance; design, UX, UI; any engineer / engineering / developer / SDE / DevOps / SRE / QA / architect / data scientist / ML; product manager / owner; data, business, financial analyst; executive or personal assistant, office admin and other non-office trades | not_gtm |
   | 5     | revops, revenue operations, sales / marketing / gtm ops(erations), business / gtm / sales systems, deal desk, sales or revenue enablement, marketing automation, sales / revenue analyst or analytics | revops |
   | 6     | founders office, chief of staff, office of the CEO / founder, founders associate, associate to the CEO / founder | growth |
   | 7     | growth (but not "business growth")                                       | growth       |
   | 8     | demand gen(eration), performance marketing, paid media / marketing / acquisition / ads / search / social, PPC, SEM, field marketing, ABM, account based marketing, campaign manager, digital marketing, email marketing, pipeline marketing, event marketing, head / VP / director / chief of marketing, marketing head / director / lead, CMO | demand_gen |
   | 9     | product marketing, content, copywriter, writer, editor, SEO, social media, community, brand, PR, communications, video | not_gtm |
   | 10    | senior leader word (head, VP, vice president, SVP, EVP, AVP, director, chief, GM, general manager) with a sales word (sales, business development, BD, revenue, commercial, gtm, SDR, BDR); sales / BD / revenue + head / director / VP / leader; CRO, chief revenue / business / commercial officer, CBO | sales_lead |
   | 11    | business development manager, manager business development, BDM; (key / strategic / enterprise / ...) account(s) manager; area / territory / zonal sales manager | ae |
   | 12    | manager / lead (not "lead gen") / leader / team lead / supervisor with a sales word | sales_lead |
   | 13    | founding / first + gtm / go to market / sales / revenue / commercial      | sales_lead   |
   | 14    | SDR, BDR, sales development, business development representative / executive / associate / trainee / intern / specialist / officer, BDE, BDA, inside sales, lead gen(eration), lead qualification, appointment setter, telesales, telecaller, telemarketing, outbound rep, market development rep, MDR | sdr |
   | 15    | account executive, AE, account manager / director, sales executive / representative / rep / associate / consultant / specialist / officer / professional, enterprise / mid market / SMB / channel / field / B2B / SaaS sales, closer | ae |
   | -     | nothing matched                                                           | the model    |

2. The model. A title no rule places goes to the gateway route
   "title_classifier" (Groq, Cloudflare fallback) with the family enum as the
   output schema. The prompt holds the title and nothing else: no company, no
   person, no URL. The gateway caches on (stage, company_id, prompt_version)
   and ignores the prompt, so the title is put into the key:
       stage = "classify_title", company_id = None,
       prompt_version = f"{PROMPT_VERSION}:{sha256(cache_title(title))[:16]}"
   cache_title collapses whitespace and case-folds, so "Founding  AE" and
   "founding ae" share one call; any other difference is a different title and
   a different call. Changing the prompt or the family definitions bumps
   PROMPT_VERSION, which re-asks every title once.

3. Offer type (docs/PLAN.md "The two segments and the two offers";
   protocol/OFFER.md, role definitions):

   | Family        | Offer type                                                    |
   | ------------- | ------------------------------------------------------------- |
   | gtm_engineer  | candidate                                                     |
   | revops        | candidate                                                     |
   | growth        | candidate                                                     |
   | sdr           | adjacent; candidate if the title says "founding" or "first"   |
   | ae            | adjacent; candidate if the title says "founding" or "first"   |
   | sales_lead    | adjacent; candidate if the title says "founding" or "first"   |
   | demand_gen    | adjacent; candidate if the title says "founding" or "first"   |
   | not_gtm       | None ("Founding Engineer" stays None)                         |

Decisions on titles the plan leaves open (conservative; change here, not ad hoc):
- Indian conventions. "Business Development Executive / Associate / BDE / BDA"
  and "Inside Sales Executive" are sdr (prospecting and qualifying seats).
  "Business Development Manager" is ae: in Indian SaaS it is usually an
  individual quota carrier, not a manager of people. "Inside Sales Manager",
  "Sales Manager" and "SDR Team Lead" are sales_lead (the title says manager or
  lead). "Key Account Manager" and "Account Manager" are ae (quota on existing
  accounts). "Accounts Executive / Manager" (finance) is not_gtm.
- Pre-sales, sales engineer, solutions engineer / consultant / architect and
  customer success are not_gtm: none is a family in the plan, and none is a
  role in OFFER.md.
- "Growth Engineer" is growth (OFFER.md names it a candidate role).
- Founder's office and chief of staff are growth, so they come out candidate as
  OFFER.md requires; the plan's enum has no family of their own. A chief of
  staff for engineering hits rule 4 first and is not_gtm.
- Performance marketing is demand_gen (paid acquisition execution, adjacent);
  growth marketing is growth (candidate).
- Sales enablement and deal desk are revops.
- Product marketing, content, SEO, brand and PR are not_gtm. A bare
  "Marketing Manager" is left to the model.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from engine.guard.budget import Budget
from engine.llm import gateway
from engine.llm.base import LlmError

Family = Literal[
    "sdr", "ae", "sales_lead", "revops", "gtm_engineer", "growth", "demand_gen", "not_gtm"
]
FAMILIES: tuple[str, ...] = (
    "sdr", "ae", "sales_lead", "revops", "gtm_engineer", "growth", "demand_gen", "not_gtm"
)
CANDIDATE_FAMILIES = frozenset({"gtm_engineer", "revops", "growth"})
ADJACENT_FAMILIES = frozenset({"sdr", "ae", "sales_lead", "demand_gen"})

STAGE = "classify_title"
ROUTE = "title_classifier"
PROMPT_VERSION = "c-1.0"
MAX_TITLE_CHARS = 200

SCHEMA: dict = {
    "type": "object",
    "properties": {"role_family": {"type": "string", "enum": list(FAMILIES)}},
    "required": ["role_family"],
    "additionalProperties": False,
}

# The definitions are descriptions, not job titles: the gateway refuses any
# prompt containing a contacts value, and contacts.title holds titles such as
# a head of sales. Only the job title itself is variable.
PROMPT = """Classify one job title into exactly one go-to-market role family.

Families:
- sdr: an entry-level seller who prospects, cold-calls or qualifies leads before handing them on.
- ae: an individual seller who runs deals to close, or grows existing customer accounts.
- sales_lead: someone who leads or manages a team of sellers, up to the top revenue leader.
- revops: someone who runs revenue, sales or marketing operations, the CRM, the go-to-market systems or seller enablement.
- gtm_engineer: an engineer who builds data, enrichment and automation systems for selling and marketing.
- growth: someone who owns growth experiments or growth marketing, or a generalist who works directly for the founders on revenue and operations.
- demand_gen: a marketer who generates pipeline through paid, performance, campaign, event or account-based marketing, or who leads marketing.
- not_gtm: everything else, including software engineering, design, people and hiring, finance, legal, customer support and success, technical pre-sales, product, content and brand.

Job title: {title}"""


# --- normalisation ------------------------------------------------------------------

def normalise(title: str) -> str:
    """Lower-case words separated by single spaces, for the rules."""
    t = unicodedata.normalize("NFKC", title).lower()
    t = re.sub(r"['’`]", "", t)
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def cache_title(title: str) -> str:
    """The title as the model cache sees it: whitespace collapsed, case-folded."""
    return " ".join(unicodedata.normalize("NFKC", title).split()).casefold()


def prompt_version_for(title: str) -> str:
    digest = hashlib.sha256(cache_title(title).encode("utf-8")).hexdigest()[:16]
    return f"{PROMPT_VERSION}:{digest}"


def build_prompt(title: str) -> str:
    clean = " ".join(unicodedata.normalize("NFKC", title).split())[:MAX_TITLE_CHARS]
    return PROMPT.format(title=clean)


# --- rules ----------------------------------------------------------------------------

def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_SALES_WORD = r"(sales|business development|bd|revenue|commercial|gtm|go to market|sdrs?|bdrs?)"
_LEADER_STRONG = r"\b(head|vp|vice president|svp|evp|avp|director|chief|gm|general manager)\b"
_LEADER_WEAK = r"\b(manager|managers|lead(?! gen| qual)|leader|team lead|supervisor)\b"
_NOT_KEY = r"(?<!key )(?<!strategic )(?<!enterprise )(?<!global )(?<!major )(?<!national )(?<!named )"

Matcher = Callable[[str], bool]


def _any(*patterns: str) -> Matcher:
    compiled = [_re(p) for p in patterns]
    return lambda t: any(p.search(t) for p in compiled)


def _all(*patterns: str) -> Matcher:
    compiled = [_re(p) for p in patterns]
    return lambda t: all(p.search(t) for p in compiled)


def _or(*matchers: Matcher) -> Matcher:
    return lambda t: any(m(t) for m in matchers)


RULES: list[tuple[str, str, Matcher]] = [
    ("gtm_engineer", "gtm engineer", _any(
        r"\b(gtm|go to market)\b(\s+\w+){0,2}\s+(engineer|engineers|engineering|developer|automation)\b",
        r"\b(engineer|engineering|developer)\b.*\b(gtm|go to market)\b",
        r"\b(revenue|sales automation|marketing automation)\s+(engineer|developer)\b",
    )),
    ("growth", "growth engineer", _any(
        r"\bgrowth\b(\s+\w+){0,2}\s+(engineer|engineering|developer)\b",
        r"\b(engineer|developer)\b(\s+\w+){0,3}\s+growth\b",
    )),
    ("revops", "crm admin", _any(
        r"\b(salesforce|sfdc|hubspot|crm|zoho|marketo|pardot)\s+(admin|administrator|developer|engineer"
        r"|consultant|specialist|analyst|architect|operations|ops)\b",
    )),
    ("not_gtm", "non-gtm discipline", _any(
        r"\bpre ?sales\b",
        r"\bsolutions?\s+(consultant|architect|specialist|engineer|engineering|lead|manager|director)\b",
        r"\bcustomer\s+(success|support|experience|service|care|onboarding)\b",
        r"\b(technical|tech|product)\s+support\b",
        r"\bsupport\s+(engineer|specialist|executive|agent|associate|lead|manager)\b",
        r"\btechnical account manager\b|\bhelp ?desk\b|\bimplementation\b",
        r"\bonboarding\s+(specialist|manager|executive|lead)\b",
        _NOT_KEY + r"\baccounts\s+(executive|manager|assistant|officer|associate|payable|receivable|head)\b",
        r"\b(accountant|finance|financial|payroll|tax|audit|auditor|bookkeeper|bookkeeping|treasury)\b",
        r"\b(hr|hrbp|human resources?|recruiter|recruiters|recruiting|recruitment|talent)\b",
        r"\bpeople\s+(ops|operations|partner|team|and culture)\b",
        r"\b(legal|counsel|lawyer|attorney|paralegal|compliance|company secretary)\b",
        r"\b(designer|design|ux|ui|illustrator|animator|graphic)\b",
        r"\b(engineer|engineers|engineering|developer|developers|sde|sdet|devops|sre|programmer|qa"
        r"|tester|architect|frontend|backend|full ?stack|firmware|sysadmin|scientist|cto)\b",
        r"\b(machine learning|ml|data science|it support)\b",
        r"\bproduct\s+(manager|owner|lead|analyst|head)\b|\bhead of product\b|\bvp product\b|\bcpo\b",
        r"\b(data|business|financial|research|quantitative)\s+analyst\b",
        r"\b(executive|personal|administrative)\s+assistant\b|\boffice\s+(admin|administrator|manager|assistant)\b",
        r"\b(nurse|doctor|teacher|tutor|driver|warehouse|chef|cook|receptionist|facilities"
        r"|housekeeping|electrician|technician|mechanic|pharmacist)\b",
    )),
    ("revops", "revops", _any(
        r"\brev ?ops\b|\brevenue\s+op(s|erations?)\b",
        r"\b(sales|marketing|gtm|go to market)\s+(and\s+(sales|marketing)\s+)?(strategy\s+and\s+)?op(s|erations?)\b",
        r"\bmops\b",
        r"\b(business|gtm|sales|revenue)\s+systems\b",
        r"\bdeal desk\b",
        r"\b(sales|revenue|gtm)\s+enablement\b",
        r"\bmarketing automation\b",
        r"\b(sales|revenue)\s+(analyst|analytics)\b",
    )),
    ("growth", "founders office", _any(
        r"\bfounders?\s+office\b|\bfounding\s+office\b|\bchief\s+of\s+staff\b",
        r"\boffice\s+of\s+the\s+(ceo|founders?)\b",
        r"\bfounders?\s+associate\b|\bassociate\s+to\s+(the\s+)?(ceo|founders?)\b",
    )),
    ("growth", "growth", _any(r"(?<!business )\bgrowth\b")),
    ("demand_gen", "demand gen", _any(
        r"\bdemand\s*gen(eration)?\b",
        r"\bperformance\s+market(ing|er)\b",
        r"\bpaid\s+(media|marketing|acquisition|ads|search|social)\b",
        r"\b(ppc|sem)\b",
        r"\bfield\s+marketing\b|\babm\b|\baccount\s+based\s+marketing\b",
        r"\bcampaigns?\s+(manager|marketing|marketer|specialist|lead)\b",
        r"\b(digital|email|pipeline|events?)\s+marketing\b",
        r"\b(head|vp|vice president|svp|director|chief)\s+(of\s+)?marketing\b",
        r"\bmarketing\s+(head|director|lead|leader)\b",
        r"\b(cmo|chief marketing officer)\b",
    )),
    ("not_gtm", "non-gtm marketing", _any(
        r"\bproduct\s+market(ing|er)\b",
        r"\b(content|copywriter|copywriting|writer|editor|seo|community|brand|pr|communications?"
        r"|comms|video|videographer|photographer)\b",
        r"\bsocial\s+media\b|\bpublic\s+relations\b",
    )),
    ("sales_lead", "senior sales leader", _or(
        _all(_LEADER_STRONG, r"\b" + _SALES_WORD + r"\b"),
        _any(r"\b(sales|business development|bd|revenue)\s+(head|director|vp|leader)\b",
             r"\b(cro|cbo|chief revenue officer|chief business officer|chief commercial officer)\b"),
    )),
    ("ae", "ae manager title", _any(
        r"\b(business development|bd)\s+manager\b|\bmanager\s+(of\s+)?business development\b|\bbdm\b",
        r"\baccounts?\s+manager\b",
        r"\b(area|territory|zonal)\s+sales\s+manager\b",
    )),
    ("sales_lead", "sales manager", _all(_LEADER_WEAK, r"\b" + _SALES_WORD + r"\b")),
    ("sales_lead", "founding gtm", _any(
        r"\b(founding|first)\s+(gtm|go to market|sales|revenue|commercial)\b",
    )),
    ("sdr", "sdr", _any(
        r"\b(sdr|sdrs|bdr|bdrs|bde|bda|mdr)\b",
        r"\bsales development\b|\bmarket development rep(resentative)?\b",
        r"\bbusiness development\s+(representative|rep|executive|associate|trainee|intern|specialist|officer)\b",
        r"\b(associate|executive|representative|trainee|intern)\b.*\bbusiness development\b",
        r"\binside sales\b",
        r"\blead\s+(gen|generation|qualification)\b",
        r"\bappointment setter\b|\btele ?sales\b|\btele ?callers?\b|\btele ?marketing\b",
        r"\boutbound\s+(sales|representative|rep|specialist|caller)\b",
    )),
    ("ae", "ae", _any(
        r"\b(account executive|account executives|ae|aes)\b",
        r"\baccount\s+(manager|director)\b",
        r"\bsales\s+(executive|executives|representative|representatives|rep|reps|associate|consultant"
        r"|specialist|officer|professional)\b",
        r"\b(enterprise|mid market|midmarket|smb|channel|field|b2b|saas)\s+sales\b",
        r"\bcloser\b",
    )),
]

_FIRST = _re(r"\b(founding|first)\b")


def rule_family(title: str) -> str | None:
    """The family from the rules, or None when no rule places the title."""
    t = normalise(title)
    if not t:
        return None
    for family, _name, match in RULES:
        if match(t):
            return family
    return None


def offer_type(family: str | None, title: str) -> str | None:
    """candidate | adjacent | None (not_gtm or unclassified)."""
    if family is None or family == "not_gtm":
        return None
    if family in CANDIDATE_FAMILIES:
        return "candidate"
    if family in ADJACENT_FAMILIES:
        return "candidate" if _FIRST.search(normalise(title)) else "adjacent"
    raise ValueError(f"unknown role family {family!r}")


# --- model fallback -------------------------------------------------------------------

def model_family(
    title: str, *, conn: sqlite3.Connection, budget: Budget | None = None
) -> str:
    """Ask the title_classifier route; cached per title. Raises the gateway's errors."""
    out = gateway.call(
        STAGE, None, prompt_version_for(title), ROUTE, build_prompt(title), SCHEMA,
        conn=conn, budget=budget,
    )
    return out["role_family"]


def is_cached(conn: sqlite3.Connection, title: str) -> bool:
    hit, _ = gateway.cached_output(conn, STAGE, None, prompt_version_for(title))
    return hit


# --- the run over the signals table ---------------------------------------------------

@dataclass
class Report:
    rows: int = 0  # unclassified rows considered
    by_rules: int = 0  # rows placed by the rules
    by_model: int = 0  # rows placed by the model (cached or fresh)
    model_titles: int = 0  # distinct titles that needed the model
    model_cached: int = 0  # of those, already in the cache
    updated: int = 0
    families: Counter = field(default_factory=Counter)
    offers: Counter = field(default_factory=Counter)
    unresolved: list[str] = field(default_factory=list)  # titles left null
    errors: list[str] = field(default_factory=list)  # "title: reason"
    stopped: str | None = None  # why the run stopped early (budget)


def _pending(
    conn: sqlite3.Connection, limit: int | None, company_id: str | None
) -> dict[str, list[str]]:
    sql = "SELECT signal_id, title FROM signals WHERE role_family IS NULL"
    params: list = []
    if company_id is not None:
        sql += " AND company_id = ?"
        params.append(company_id)
    sql += " ORDER BY signal_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    titles: dict[str, list[str]] = {}
    for row in conn.execute(sql, params):
        titles.setdefault(row["title"], []).append(row["signal_id"])
    return titles


def _update(conn: sqlite3.Connection, ids: list[str], family: str, offer: str | None) -> int:
    with conn:
        n = 0
        for sid in ids:
            cur = conn.execute(
                "UPDATE signals SET role_family = ?, offer_type = ?"
                " WHERE signal_id = ? AND role_family IS NULL",
                (family, offer, sid),
            )
            n += cur.rowcount
    return n


def run(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    company_id: str | None = None,
    dry_run: bool = False,
    use_model: bool = True,
    budget: Budget | None = None,
) -> Report:
    """Classify every signal whose role_family is null. Classified rows are untouched."""
    from engine.guard.budget import BudgetExceeded

    rep = Report()
    pending = _pending(conn, limit, company_id)
    rep.rows = sum(len(ids) for ids in pending.values())
    ruled: dict[str, str] = {}
    to_model: dict[str, list[str]] = {}  # cache_title -> titles sharing that key
    for title in pending:
        fam = rule_family(title)
        if fam is None:
            to_model.setdefault(cache_title(title), []).append(title)
        else:
            ruled[title] = fam
    rep.by_rules = sum(len(pending[t]) for t in ruled)
    rep.model_titles = len(to_model)
    rep.model_cached = sum(1 for titles in to_model.values() if is_cached(conn, titles[0]))
    if dry_run:
        rep.by_model = rep.rows - rep.by_rules
        return rep

    def apply(title: str, fam: str) -> None:
        offer = offer_type(fam, title)
        n = _update(conn, pending[title], fam, offer)
        rep.updated += n
        rep.families[fam] += n
        rep.offers[offer or "none"] += n

    for title, fam in ruled.items():
        apply(title, fam)

    for titles in to_model.values():
        if not use_model or rep.stopped:
            rep.unresolved.extend(titles)
            continue
        try:
            fam = model_family(titles[0], conn=conn, budget=budget)
        except BudgetExceeded as exc:
            rep.stopped = str(exc)
            rep.unresolved.extend(titles)
            continue
        except LlmError as exc:
            rep.errors.append(f"{titles[0]}: {type(exc).__name__}")
            rep.unresolved.extend(titles)
            continue
        for title in titles:
            apply(title, fam)
            rep.by_model += len(pending[title])
    return rep
