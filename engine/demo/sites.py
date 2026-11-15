"""The three invented companies the demo runs on, as the websites, job boards and
DNS records a capture would see. Nothing here names a real company; every
domain is under .example, every board slug is invented.

    vantorell   clinic shift staffing; hiring a founding SDR. The demo's
                teardown: the judge names G3 and the page is rendered.
    corvessa    expense audits; hiring an Account Executive into a sales team
                that already runs. The judge abstains (no_gap).
    ambrisk     its robots.txt disallows everything, so capture sees too
                little and drops it (no_site_evidence). A blocked site is an
                outcome, never something to work around.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class DemoCompany:
    slug: str  # the invented greenhouse board slug
    name: str
    domain: str
    segment: str
    pages: dict[str, str]  # path on the company's domain -> html
    jobs: list[dict[str, str]]  # the board: title, location, path of the post, content
    robots: str = "User-agent: *\nAllow: /\n"
    dns: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # empty: lookups fail


GREENHOUSE_API = "boards-api.greenhouse.io"
GREENHOUSE_JOBS = "job-boards.greenhouse.io"


def job_url(c: DemoCompany, job: dict[str, str]) -> str:
    return f"https://{GREENHOUSE_JOBS}/{c.slug}/jobs/{job['id']}"


def board_json(c: DemoCompany, today: date) -> bytes:
    """The board as the greenhouse API shapes it, posted three days before today
    so every role is fresh."""
    posted = (today - timedelta(days=3)).isoformat() + "T09:00:00Z"
    jobs = [{"id": int(j["id"]), "title": j["title"], "company_name": c.name,
             "location": {"name": j["location"]}, "absolute_url": job_url(c, j),
             "updated_at": posted, "first_published": posted,
             "content": j.get("content", "")} for j in c.jobs]
    return json.dumps({"jobs": jobs, "meta": {"total": len(jobs)}}, sort_keys=True).encode()


VANTORELL_HOME = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Vantorell | Shift staffing for clinics and dental chains</title>
  <script async src="//js.hs-scripts.com/7720031.js"></script>
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-VNT0001"></script>
</head>
<body>
  <header>
    <a href="/" aria-label="Vantorell home">Vantorell</a>
    <nav>
      <a href="/pricing">Pricing</a>
      <a href="/customers">Customers</a>
      <a href="/industries/dental">Dental chains</a>
      <a href="/industries/diagnostics">Diagnostics labs</a>
      <a href="/compare">Compare</a>
      <a href="/careers">Careers</a>
      <a href="/login">Log in</a>
    </nav>
  </header>
  <main>
    <h1>Fill every clinic shift in 48 hours</h1>
    <p>Vantorell matches nurses and technicians to open shifts across 30 cities. Clinic
    managers post a shift, qualified staff nearby accept it, and the clinic sees who is
    coming before the day starts. Dental chains, diagnostics labs and day-care hospitals use
    it to cover leave, peak days and new branches without calling an agency. Every match
    carries the licence check, the shift history and the rating from the last three clinics,
    so a manager can approve in one tap. Payroll exports go out weekly in the format the
    accountant already uses.</p>
    <a class="btn" href="/demo">Book a demo</a>
  </main>
  <footer>
    <a href="/about">About</a>
    <a href="/contact">Contact</a>
    <p>&copy; 2026 Vantorell Health Private Limited, Pune, India</p>
  </footer>
</body>
</html>
"""

VANTORELL_PRICING = """<!doctype html>
<html><head><title>Pricing | Vantorell</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <main>
    <h1>Simple pricing for every clinic</h1>
    <div><h2>Starter</h2><p>&#8377;4,999 / month for up to 5 locations</p></div>
    <div><h2>Enterprise</h2><p>Talk to our team for a custom quote</p></div>
  </main>
</body></html>
"""

VANTORELL_CUSTOMERS = """<!doctype html>
<html><head><title>Customers | Vantorell</title></head>
<body>
  <main>
    <h1>Clinics that run on Vantorell</h1>
    <article><h2>Saffronleaf Dental</h2><p>A 40-chair dental chain across West India cut
    unfilled shifts by half.</p></article>
    <article><h2>Lumenpath Diagnostics</h2><p>Opened 12 collection centres in Singapore with
    Vantorell.</p></article>
    <article><h2>Oasisvale Care</h2><p>Staffing night shifts across the UAE.</p></article>
  </main>
  <footer><p>Offices in the UK and Pune</p></footer>
</body></html>
"""

VANTORELL_COMPARE = """<!doctype html>
<html><head><title>Compare | Vantorell</title></head>
<body>
  <main>
    <h1>Vantorell vs Routemark: which fits a clinic chain?</h1>
    <p>Routemark was built for hospitals. Vantorell is built for clinic chains.</p>
  </main>
</body></html>
"""

VANTORELL_DEMO = """<!doctype html>
<html><head><title>Book a demo | Vantorell</title>
<script src="https://assets.calendly.com/assets/external/widget.js"></script>
</head>
<body>
  <main>
    <h1>See Vantorell in action</h1>
    <form id="demo" action="/api/demo" method="post">
      <label for="f1">First name</label><input id="f1" name="first_name">
      <label for="f2">Last name</label><input id="f2" name="last_name">
      <label for="f3">Work email</label><input id="f3" name="email" type="email">
      <label for="f4">Phone</label><input id="f4" name="phone" type="tel">
      <label for="f5">Company</label><input id="f5" name="company">
      <label for="f6">Job title</label><input id="f6" name="job_title">
      <label for="f7">Number of locations</label>
      <select id="f7" name="locations"><option>1-5</option></select>
      <label for="f8">City</label><input id="f8" name="city">
      <label for="f9">How did you hear about us?</label><textarea id="f9" name="source"></textarea>
      <button type="submit">Request demo</button>
    </form>
    <p>After you submit, pick a time that suits you.</p>
  </main>
</body></html>
"""

VANTORELL_JOB = """<!doctype html>
<html><head><title>Founding Sales Development Representative | Vantorell</title></head>
<body>
  <main>
    <h1>Founding Sales Development Representative</h1>
    <p>Pune, India (hybrid)</p>
    <h2>What you will do</h2>
    <ul>
      <li>Build and own target account lists of dental and diagnostics chains from scratch.</li>
      <li>Define our ICP with the founders as we expand into the Middle East.</li>
      <li>Write and run outbound sequences in HubSpot and Apollo, and keep the CRM clean.</li>
      <li>Set up HubSpot properties and pipelines as the first sales hire.</li>
      <li>Book 15 qualified meetings a month against a monthly quota.</li>
    </ul>
    <h2>You have</h2>
    <ul><li>Two years of outreach to clinic owners; comfort with Excel and Google Sheets.</li></ul>
  </main>
</body></html>
"""


def _simple(title: str, body: str) -> str:
    return (f"<!doctype html>\n<html><head><title>{title}</title></head>\n"
            f"<body><main>{body}</main></body></html>\n")


VANTORELL = DemoCompany(
    slug="vantorell", name="Vantorell", domain="vantorell.example", segment="in_saas",
    pages={
        "/": VANTORELL_HOME,
        "/pricing": VANTORELL_PRICING,
        "/customers": VANTORELL_CUSTOMERS,
        "/compare": VANTORELL_COMPARE,
        "/demo": VANTORELL_DEMO,
        "/about": _simple("About | Vantorell", "<h1>About Vantorell</h1><p>We started in "
                          "Pune in 2022 to fix last-minute clinic staffing.</p>"),
        "/careers": _simple("Careers | Vantorell", "<h1>Careers</h1><p>Every open role is "
                            "on our board.</p>"),
        "/contact": _simple("Contact | Vantorell", "<h1>Contact</h1><p>Write to us from the "
                            "demo page and a person answers within a day.</p>"),
    },
    jobs=[
        {"id": "7001", "title": "Founding Sales Development Representative",
         "location": "Pune, India", "page": VANTORELL_JOB,
         "content": "&lt;p&gt;Build target lists of clinic chains.&lt;/p&gt;"},
        {"id": "7002", "title": "Account Executive", "location": "Pune, India",
         "content": "&lt;p&gt;Close clinic chains.&lt;/p&gt;"},
        {"id": "7003", "title": "Growth Marketing Manager", "location": "Remote, India",
         "content": "&lt;p&gt;Run campaigns for clinic owners.&lt;/p&gt;"},
        {"id": "7004", "title": "Senior Software Engineer", "location": "Pune, India",
         "content": "&lt;p&gt;Build the matching engine.&lt;/p&gt;"},
    ],
    dns={
        ("vantorell.example", "TXT"): [
            "v=spf1 include:_spf.google.com include:amazonses.com -all"],
        ("_dmarc.vantorell.example", "TXT"): ["v=DMARC1; p=none"],
        ("vantorell.example", "MX"): ["1 aspmx.l.google.com"],
    },
)

CORVESSA_HOME = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Corvessa | Expense audits for finance teams</title></head>
<body>
  <header><nav>
    <a href="/pricing">Pricing</a>
    <a href="/product">Product</a>
    <a href="/customers">Customers</a>
    <a href="/careers">Careers</a>
  </nav></header>
  <main>
    <h1>Every expense checked before it is paid</h1>
    <p>Corvessa reads each claim, receipt and card line, flags the ones that break policy,
    and routes them to the right approver. Finance teams at mid-market companies in India
    and Singapore use it to close the month two days sooner. It connects to the accounting
    system the team already runs, keeps an audit trail for every decision, and sends the
    controller one weekly summary of what was flagged, approved and recovered. Setup takes
    an afternoon with the policy document the team already has.</p>
    <a class="btn" href="/signup">Start free trial</a>
  </main>
  <footer><p>&copy; 2026 Corvessa Software Private Limited, Bengaluru, India</p></footer>
</body>
</html>
"""

CORVESSA_JOB = _simple(
    "Account Executive | Corvessa",
    "<h1>Account Executive</h1><p>Bengaluru, India</p><ul><li>Run the sales cycle from first "
    "call to signed order for mid-market finance teams.</li><li>Work inbound trials with our "
    "SDR team and the documented sales playbook.</li></ul>")

CORVESSA = DemoCompany(
    slug="corvessa", name="Corvessa", domain="corvessa.example", segment="in_saas",
    pages={
        "/": CORVESSA_HOME,
        "/pricing": _simple("Pricing | Corvessa", "<h1>Pricing</h1><div><h2>Team</h2><p>"
                            "&#8377;2,500 per user per month, billed yearly.</p></div><div>"
                            "<h2>Business</h2><p>&#8377;4,000 per user per month, with SSO."
                            "</p></div>"),
        "/product": _simple("Product | Corvessa", "<h1>How Corvessa works</h1><p>Policy "
                            "rules, receipt matching and approval routing in one place.</p>"),
        "/customers": _simple("Customers | Corvessa", "<h1>Customers</h1><article><h2>"
                              "Tallowmere Logistics</h2><p>Cut month-end close by two days."
                              "</p></article>"),
        "/signup": _simple("Start free | Corvessa", "<h1>Start your free trial</h1><form "
                           "action=\"/api/signup\" method=\"post\"><label for=\"e\">Work "
                           "email</label><input id=\"e\" name=\"email\" type=\"email\">"
                           "<button type=\"submit\">Start</button></form>"),
        "/careers": _simple("Careers | Corvessa", "<h1>Careers</h1><p>We are hiring an "
                            "Account Executive in Bengaluru.</p>"),
    },
    jobs=[
        {"id": "8001", "title": "Account Executive", "location": "Bengaluru, India",
         "page": CORVESSA_JOB, "content": "&lt;p&gt;Run the sales cycle.&lt;/p&gt;"},
        {"id": "8002", "title": "Sales Development Representative",
         "location": "Bengaluru, India", "content": "&lt;p&gt;Work inbound trials.&lt;/p&gt;"},
    ],
    dns={
        ("corvessa.example", "TXT"): ["v=spf1 include:spf.protection.outlook.com -all"],
        ("_dmarc.corvessa.example", "TXT"): ["v=DMARC1; p=reject"],
        ("corvessa.example", "MX"): ["0 corvessa-example.mail.protection.outlook.com"],
    },
)

AMBRISK = DemoCompany(
    slug="ambrisk", name="Ambrisk", domain="ambrisk.example", segment="in_saas",
    pages={"/": _simple("Ambrisk", "<h1>Ambrisk</h1><p>Never fetched: robots.txt says no."
                        "</p>")},
    robots="User-agent: *\nDisallow: /\n",
    jobs=[
        {"id": "9001", "title": "Growth Marketing Manager", "location": "Mumbai, India",
         "content": "&lt;p&gt;Run paid campaigns.&lt;/p&gt;"},
    ],
    dns={},  # every lookup fails: the record is unseen, not absent
)

COMPANIES: tuple[DemoCompany, ...] = (VANTORELL, CORVESSA, AMBRISK)
