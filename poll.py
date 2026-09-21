#!/usr/bin/env python3
"""Poll Greenhouse / Lever / Ashby job boards for Engineering Manager postings.

Reads companies.json, fetches each board's public JSON endpoint, keeps postings
whose title looks like a software engineering management role, and writes:

  data/postings.json  every currently-open match, with first_seen / last_seen
  data/new.json       matches first seen on this run
  data/errors.json    boards that failed this run (so silent gaps are visible)

Run it anywhere with normal network access (GitHub Actions, a laptop cron).
Only dependency: `requests`.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
COMPANIES = os.path.join(ROOT, "companies.json")
POSTINGS = os.path.join(DATA, "postings.json")
NEW = os.path.join(DATA, "new.json")
ERRORS = os.path.join(DATA, "errors.json")

TIMEOUT = 20
HEADERS = {"User-Agent": "em-job-poller/1.0 (+personal job search)"}

# ---- title matching --------------------------------------------------------
# A posting is kept when its title reads as a software-engineering management
# role. Tune these two lists rather than the code.
INCLUDE = re.compile(
    r"""
    (?:^|\b)(?:
        (?:senior\s+|sr\.?\s+|staff\s+|group\s+)?engineering\s+manager
      | manager,?\s+(?:software|engineering|platform|backend|infrastructure)
      | (?:software|platform|backend|infrastructure|payments|billing|data|mobile|
         product|growth|fintech|fullstack|full-stack|front-?end|application)\s+
         (?:engineering\s+)?manager
      | (?:engineering|software)\s+(?:team\s+)?lead(?:er)?\s*manager
      | tech(?:nical)?\s+lead\s+manager
    )
    """,
    re.I | re.X,
)
# Scope: first-line and senior EM only. Director / Head of / VP / CTO are out.
EXCLUDE = re.compile(
    r"\bdirector\b|\bhead\s+of\b|\bvp\b|vice\s+president|\bcto\b|chief\b|"
    r"product\s+manager|program\s+manager|project\s+manager|account\s+manager|"
    r"engineering\s+program|marketing|sales|success|recruit|talent|community|"
    r"solutions?\s+(?:engineer|architect)|support\s+engineer|customer|"
    r"security\s+engineering\s+manager|it\s+manager|hardware|electrical|mechanical|"
    r"\bqa\b|quality|manufactur|facilities|construction|civil",
    re.I,
)

US_EXPLICIT = re.compile(
    r"\b(us|usa|u\.s\.a?\.?|united states|north america|anywhere)\b", re.I
)
US_STATE = re.compile(
    r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|ia|ks|ky|la|md|ma|mi|mn|ms|mo|mt|"
    r"ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)\b",
    re.I,
)
NON_US = re.compile(
    r"\b(canada|uk|united kingdom|ireland|india|bangalore|bengaluru|poland|germany|"
    r"france|spain|netherlands|israel|tel aviv|mexico|brazil|argentina|colombia|"
    r"australia|anz|singapore|japan|emea|apac|latam|europe|london|dublin|berlin|"
    r"toronto|vancouver|montreal)\b",
    re.I,
)


# Culture / pace phrases worth a second look. Matched against the JD text of
# postings that pass the title filter; surfaced as `flags` in the tracker.
WARN = re.compile(
    r"fast[- ]paced|aggressive (?:sprint|goal|timeline|deadline)s?|hustle|"
    r"wear (?:many|multiple) hats|high[- ]intensity|(?:the )?environment is intense|"
    r"relentless(?:ly)?|scrappy|sense of urgency|thrive (?:in|on) ambiguity|"
    r"navigate ambiguity|hairy|not remote[- ]only|in[- ]person (?:events|surges|days)|"
    r"on[- ]call rotation|incident (?:commander|triag)|player[- ]coach|"
    r"execute (?:on )?the roadmap|influence without authority|do the work yourself",
    re.I,
)


# Scope gate from the JD text: >5 years managing, or managing managers.
YEARS_MGMT = re.compile(
    r"(\d+)\s*\+?\s*(?:or more\s+)?years?[^.;]{0,60}?"
    r"(?:managing|management|leading|leadership|people[- ]manag)", re.I)
MANAGES_MANAGERS = re.compile(
    r"manag(?:e|ing)\s+(?:engineering\s+)?managers|managers?\s+(?:of|reporting to)\s+(?:engineering\s+)?managers|"
    r"lead(?:ing)?\s+(?:a\s+)?(?:team|group|org)\s+of\s+(?:engineering\s+)?managers|"
    r"(?:engineering\s+)?managers\s+(?:and\s+tech\s+leads\s+)?(?:will\s+)?report\s+to\s+(?:you|this role)|"
    r"second[- ]line\s+manag|manager\s+of\s+managers|multiple\s+teams\s+of\s+managers", re.I)


def scope_check(text: str):
    """Returns (scope_ok, mgmt_years_required, reason)."""
    yrs = None
    for m in YEARS_MGMT.finditer(text or ""):
        n = int(m.group(1))
        if n <= 25:                     # ignore "10+ years of software experience"-style noise
            yrs = n if yrs is None else min(yrs, n)
    if MANAGES_MANAGERS.search(text or ""):
        return False, yrs, "manages managers"
    if yrs is not None and yrs > 5:
        return False, yrs, f"asks {yrs}+ years managing"
    return True, yrs, ""


def warning_flags(text: str):
    seen, out = set(), []
    for m in WARN.finditer(text or ""):
        k = m.group(0).lower()
        if k not in seen:
            seen.add(k)
            out.append(m.group(0))
    return out


def strip_html(s: str) -> str:
    import html as _h
    return _h.unescape(re.sub(r"<[^>]+>", " ", s or ""))


def looks_like_em(title: str) -> bool:
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


def remote_us(location: str, remote_flag) -> bool:
    """True when the posting is remote and either names the US or names no other country."""
    loc = location or ""
    is_remote = bool(remote_flag) or "remote" in loc.lower() or "anywhere" in loc.lower()
    if not is_remote:
        return False
    if US_EXPLICIT.search(loc):
        return True          # "Remote - USA, CAN, MEX" counts: the US is named
    if NON_US.search(loc):
        return False         # "Canada - Remote (ON, AB, BC)": another country, US unnamed
    if US_STATE.search(loc):
        return True
    return True              # bare "Remote" with no country named: assume US, verify on the posting


# ---- fetchers ----------------------------------------------------------------
def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield {
            "id": f"gh:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "remote_flag": None,
            "posted": (j.get("updated_at") or "")[:10],
            "_desc": lambda jid=j["id"]: strip_html(requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}",
                timeout=TIMEOUT, headers=HEADERS).json().get("content", "")),
        }


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    for j in r.json():
        cats = j.get("categories") or {}
        yield {
            "id": f"lv:{slug}:{j['id']}",
            "title": j.get("text", ""),
            "location": ", ".join(filter(None, [cats.get("location"), cats.get("allLocations") and "; ".join(cats["allLocations"])])) or cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "remote_flag": (j.get("workplaceType") or "").lower() == "remote",
            "_desc": lambda j=j: j.get("descriptionPlain") or strip_html(j.get("description", "")),
            "posted": time.strftime("%Y-%m-%d", time.gmtime((j.get("createdAt") or 0) / 1000)) if j.get("createdAt") else "",
        }


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield {
            "id": f"ab:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
            "remote_flag": j.get("isRemote"),
            "_desc": lambda j=j: j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "posted": (j.get("publishedAt") or "")[:10],
        }


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def poll_company(c):
    try:
        found = []
        for j in FETCHERS[c["ats"]](c["slug"]):
            if not looks_like_em(j["title"]):
                continue
            try:
                desc = j["_desc"]()
            except Exception:  # noqa: BLE001
                desc = ""
            flags = warning_flags(desc)
            scope_ok, yrs, why = scope_check(desc)
            found.append({
                "id": j["id"],
                "company": c["company"],
                "category": c["category"],
                "title": j["title"].strip(),
                "location": j["location"].strip(),
                "remote_us": remote_us(j["location"], j["remote_flag"]),
                "url": j["url"],
                "posted": j["posted"],
                "flags": flags,
                "scope_ok": scope_ok,
                "mgmt_years_required": f"{yrs}+" if yrs is not None else "not stated",
                "scope_reason": why,
            })
        return c, found, None
    except Exception as e:  # noqa: BLE001 - we want every failure recorded, not raised
        return c, [], f"{type(e).__name__}: {e}"


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main():
    os.makedirs(DATA, exist_ok=True)
    companies = load(COMPANIES, [])
    previous = {p["id"]: p for p in load(POSTINGS, [])}
    today = date.today().isoformat()

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(poll_company, companies))

    current, errors = {}, []
    for c, found, err in results:
        if err:
            errors.append({"company": c["company"], "ats": c["ats"], "slug": c["slug"], "error": err})
        for p in found:
            prev = previous.get(p["id"])
            p["first_seen"] = prev["first_seen"] if prev else today
            p["last_seen"] = today
            current[p["id"]] = p

    # A board that errored keeps its previous postings so a transient failure
    # doesn't make everything look "new" again on the next successful run.
    failed = {(e["company"]) for e in errors}
    for pid, p in previous.items():
        if p["company"] in failed and pid not in current:
            current[pid] = p

    new = [p for pid, p in current.items() if pid not in previous]
    ordered = sorted(current.values(), key=lambda p: (not p["remote_us"], p["first_seen"], p["company"]), reverse=False)

    with open(POSTINGS, "w") as f:
        json.dump(ordered, f, indent=1)
    with open(NEW, "w") as f:
        json.dump(sorted(new, key=lambda p: (not p["remote_us"], p["company"])), f, indent=1)
    with open(ERRORS, "w") as f:
        json.dump({"date": today, "errors": errors}, f, indent=1)

    print(f"{today}: {len(companies)} boards, {len(errors)} failed, "
          f"{len(current)} open EM-like postings ({sum(p['remote_us'] for p in current.values())} remote-US), "
          f"{len(new)} new")
    for p in new:
        flag = "REMOTE-US" if p["remote_us"] else "         "
        warn = f"  ⚠ {', '.join(p['flags'])}" if p.get("flags") else ""
        if not p.get("scope_ok", True):
            warn = f"  OUT OF SCOPE ({p['scope_reason']})" + warn
        print(f"  NEW {flag} {p['company']:20} {p['title']} — {p['location']}{warn}")
    if errors:
        print("Failed boards:")
        for e in errors:
            print(f"  {e['company']:20} {e['ats']}/{e['slug']}: {e['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
