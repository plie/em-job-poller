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
JD_DIR = os.path.join(DATA, "jd")

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
    r"\bdirector\b|\bhead\s+of\b|\bvp\b|vice\s+president|\bcto\b|chief\b|of\s+managers|"
    r"finance|financial\s+planning|strategic|analytics\s+manager|"
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
    r"(\d+)\s*\+?\s*(?:or more\s+)?years?(?:(?!\d+\s*\+?\s*years?)[^.;]){0,60}?"
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
    """Greenhouse returns HTML-escaped HTML, so unescape first, then strip tags (twice is safe)."""
    import html as _h
    s = _h.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d|/div)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _h.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


# ---- JD field extraction -----------------------------------------------------
MONEY = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])")


def _to_int(s):
    s = s.replace(",", "").strip()
    if s[-1] in "kK":
        return int(float(s[:-1]) * 1000)
    return int(s)


def salary_tiers(text: str):
    """All $min–$max annual ranges in the JD, deduped, highest first.

    Returns {"ranges": [{"min","max","label"}...], "top": {...}|None, "mine": {...}|None}.
    Jenee's rule: when a posting lists several tiers she is in the second-highest
    (Tier 1 is SF/NY); with one range that range is hers.
    """
    pairs = []
    for m in re.finditer(
        r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])",
        text or "",
    ):
        try:
            lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
        except ValueError:
            continue
        if not (40_000 <= lo < hi <= 900_000):
            continue
        before = (text[max(0, m.start() - 120):m.start()]).lower()
        label = ""
        lm = re.search(r"(tier\s*\d|zone\s*\d|\bnational\b|\bremote\b|san francisco|new york|bay area|seattle|"
                       r"nyc|sf\b|hub|premium|standard|metro|base)", before)
        if lm:
            label = lm.group(1)
        pairs.append((lo, hi, label))
    seen, ranges = set(), []
    for lo, hi, label in pairs:
        if (lo, hi) in seen:
            continue
        seen.add((lo, hi))
        ranges.append({"min": lo, "max": hi, "label": label})
    ranges.sort(key=lambda r: (r["max"], r["min"]), reverse=True)
    top = ranges[0] if ranges else None
    mine = ranges[1] if len(ranges) >= 2 else top
    return {"ranges": ranges, "top": top, "mine": mine}


PREF_HEAD = re.compile(r"prefer|nice[- ]to[- ]have|bonus|a plus|plus:|ideally|great if|not required", re.I)
REQ_HEAD = re.compile(r"requir|minimum|must[- ]have|qualification|what you(?:'ll)? bring|you have|about you|who you are", re.I)


def mgmt_years(text: str):
    """Years of people-management: required vs preferred, judged by the nearest preceding heading."""
    req, pref = [], []
    for m in YEARS_MGMT.finditer(text or ""):
        n = int(m.group(1))
        if n > 25:
            continue
        window = text[max(0, m.start() - 600):m.start()]
        p = max((x.end() for x in PREF_HEAD.finditer(window)), default=-1)
        r = max((x.end() for x in REQ_HEAD.finditer(window)), default=-1)
        line = text[max(0, m.start() - 60):m.end()]
        if PREF_HEAD.search(line) or p > r:
            pref.append(n)
        else:
            req.append(n)
    return {"required": min(req) if req else None, "preferred": min(pref) if pref else None}


FRONT = re.compile(r"\b(react|typescript|javascript|front[- ]?end|ios|android|mobile|swift|kotlin|react native|"
                   r"design system|web platform|ui\b|ux\b|css)\b", re.I)
BACK = re.compile(r"\b(back[- ]?end|distributed systems|microservices|apis?\b|infrastructure|platform|data pipeline|"
                  r"postgres|sql|kafka|aws|gcp|kubernetes|ruby|rails|python|go\b|golang|java|scala|payments? systems?)\b", re.I)


def focus(title: str, text: str):
    t = (title or "")
    body = (text or "")[:6000]
    f = len(FRONT.findall(t)) * 3 + len(FRONT.findall(body))
    b = len(BACK.findall(t)) * 3 + len(BACK.findall(body))
    if f < 2 and b < 2:
        return "unspecified"
    if f >= 2 and b >= 2 and min(f, b) / max(f, b) > 0.5:
        return "both"
    return "frontend/mobile" if f > b else "backend"


def looks_like_em(title: str) -> bool:
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


HUB_CITY = re.compile(
    r"\bHQ\b|\boffice\b|new york|san francisco|seattle|chicago|boston|austin|denver|"
    r"los angeles|palo alto|menlo park|mountain view|atlanta|washington,? d\.?c",
    re.I,
)


def remote_us(location: str, remote_flag) -> bool:
    """True when the posting is remote and either names the US or names no other country."""
    loc = location or ""
    says_remote = "remote" in loc.lower() or "anywhere" in loc.lower()
    # Ashby's isRemote is set on hybrid postings too; a bare city with the flag is not remote.
    is_remote = says_remote or (bool(remote_flag) and not HUB_CITY.search(loc))
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
            jd_path = os.path.join(JD_DIR, j["id"].replace(":", "-").replace(".", "_") + ".md")
            desc = ""
            if os.path.exists(jd_path):
                with open(jd_path) as f:
                    raw = f.read()
                head, _, body = raw.partition("\n---\n")
                desc = strip_html(body)
                if desc != body.strip():
                    with open(jd_path, "w") as f:
                        f.write(head + "\n---\n" + desc + "\n")
            else:
                try:
                    desc = j["_desc"]()
                except Exception:  # noqa: BLE001
                    desc = ""
                if desc.strip():
                    os.makedirs(JD_DIR, exist_ok=True)
                    with open(jd_path, "w") as f:
                        f.write(f"# {c['company']} — {j['title']}\n\n"
                                f"- url: {j['url']}\n- location: {j['location']}\n"
                                f"- captured: {date.today().isoformat()}\n---\n{desc.strip()}\n")
            flags = warning_flags(desc)
            scope_ok, yrs, why = scope_check(desc)
            sal = salary_tiers(desc)
            my = mgmt_years(desc)
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
                "mgmt_years_required": (my["required"] if my["required"] is not None else yrs),
                "scope_reason": why,
                "mgmt_years_preferred": my["preferred"],
                "salary_ranges": sal["ranges"],
                "salary_top": sal["top"],
                "salary_mine": sal["mine"],
                "focus": focus(j["title"], desc),
                "jd_file": os.path.relpath(jd_path, ROOT) if os.path.exists(jd_path) else "",
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
