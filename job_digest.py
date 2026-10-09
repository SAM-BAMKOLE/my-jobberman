#!/usr/bin/env python3
"""Daily remote-job digest for Samuel Olagoroye.

Pulls remote developer jobs from free public job feeds, keeps the ones you can
plausibly apply to from Nigeria, scores them against your stack (0-100), groups
them into High / Medium / Stretch, and emails you only the NEW ones each day.

Usage:
    python job_digest.py              # fetch, email new jobs, update seen_jobs.json
    python job_digest.py --dry-run    # fetch + write digest.html, no email, no state change
    python job_digest.py --selftest   # offline check of the scoring logic

Email settings come from environment variables (GitHub Actions secrets):
    SMTP_USER, SMTP_PASS, EMAIL_TO  (required)
    SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465), EMAIL_FROM
    SEND_EMPTY=1  -> also email when there is nothing new
"""
import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; personal-job-digest/1.0)"
SEEN_FILE = "seen_jobs.json"
SEEN_KEEP_DAYS = 60
MAX_AGE_DAYS = 30
MAX_PER_TIER = 15
TIERS = (("High probability", 75), ("Medium probability", 58), ("Stretch", 42))

# ----------------------------------------------------------------- profile --
STACK = {  # name: (regex, points)
    "Next.js": (r"next\.?js", 10),
    "React": (r"\breact(\.?js)?\b", 10),
    "TypeScript": (r"typescript", 8),
    "Node.js": (r"\bnode(\.?js)?\b", 6),
    "Supabase": (r"supabase", 5),
    "Postgres": (r"postgres(ql)?", 4),
    "Express": (r"\bexpress(\.?js)?\b", 3),
    "Vue": (r"\bvue(\.?js)?\b", 3),
    "Hono": (r"\bhono\b", 3),
    "Tailwind": (r"tailwind", 2),
    "Headless CMS": (r"contentful|sanity|headless cms|strapi", 2),
    "Testing": (r"playwright|vitest|\bjest\b", 2),
    "REST": (r"rest(ful)? api", 2),
    "JavaScript": (r"javascript", 2),
    "WordPress": (r"wordpress", 2),
}
TITLE_OK = re.compile(
    r"full[\s-]?stack|front[\s-]?end|back[\s-]?end|web developer|software (engineer|developer)"
    r"|javascript|\breact\b|next\.?js|\bnode\b|web engineer",
    re.I,
)
TITLE_BAD = re.compile(
    r"devops|\bsre\b|data (scientist|engineer|analyst)|machine learning|\bml\b|\bios\b|android"
    r"|mobile|embedded|security|manager|director|head of|designer|\bjava\b|\.net|c\+\+|golang"
    r"|\bgo\b|rust|ruby|scala|kotlin|salesforce|\bsap\b|blockchain|solidity|unity|game engine",
    re.I,
)
RESTRICTED_TEXT = re.compile(
    r"(must|need to|required to)\s+(be|reside|live)\s+(located\s+)?in\s+(the\s+)?(us|usa|u\.s\.|united states|uk|eu|canada)\b"
    r"|authori[sz]ed to work in (the )?(us|usa|united states|uk|eu|canada)"
    r"|us (work )?authori[sz]ation|us citizens? only|w-?2 (only|employee)"
    r"|(us|usa|uk|eu|canada)[- ]based (candidates )?only",
    re.I,
)


# ------------------------------------------------------------------ helpers --
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc)
        s = str(v).strip()
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def job(source, title, company, url, location, desc, posted, tags=None, salary=""):
    return {
        "source": source,
        "title": strip_html(title),
        "company": strip_html(company),
        "url": url,
        "location": strip_html(location),
        "desc": strip_html(desc)[:6000],
        "posted": parse_date(posted),
        "tags": tags or [],
        "salary": salary or "",
    }


# ------------------------------------------------------------------ sources --
def from_remotive():
    data = json.loads(http_get("https://remotive.com/api/remote-jobs?category=software-dev&limit=150"))
    return [
        job("Remotive", j.get("title"), j.get("company_name"), j.get("url"),
            j.get("candidate_required_location"), j.get("description"),
            j.get("publication_date"), j.get("tags"), j.get("salary"))
        for j in data.get("jobs", [])
    ]


def from_remoteok():
    data = json.loads(http_get("https://remoteok.com/api"))
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is a legal notice
        sal = ""
        if j.get("salary_min"):
            sal = f"${j['salary_min']:,}-${j.get('salary_max', 0):,}"
        out.append(job("Remote OK", j.get("position"), j.get("company"), j.get("url"),
                       j.get("location") or "", j.get("description"), j.get("date"),
                       j.get("tags"), sal))
    return out


def from_jobicy():
    out = []
    for tag in ("react", "node"):
        data = json.loads(http_get(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={tag}"))
        for j in data.get("jobs", []):
            geo = j.get("jobGeo")
            geo = ", ".join(geo) if isinstance(geo, list) else (geo or "")
            out.append(job("Jobicy", j.get("jobTitle"), j.get("companyName"), j.get("url"),
                           geo, (j.get("jobExcerpt") or "") + " " + (j.get("jobDescription") or ""),
                           j.get("pubDate")))
    return out


def from_wwr():
    out = []
    for cat in ("remote-full-stack-programming-jobs", "remote-front-end-programming-jobs",
                "remote-back-end-programming-jobs"):
        root = ET.fromstring(http_get(f"https://weworkremotely.com/categories/{cat}.rss"))
        for it in root.iter("item"):
            raw = it.findtext("title") or ""
            company, _, title = raw.partition(":")
            if not title:
                company, title = "", raw
            out.append(job("We Work Remotely", title, company, it.findtext("link"),
                           it.findtext("region") or "", it.findtext("description"),
                           it.findtext("pubDate")))
    return out


SOURCES = (from_remotive, from_remoteok, from_jobicy, from_wwr)


def fetch_all():
    jobs, status = [], []
    for fn in SOURCES:
        try:
            got = fn()
            jobs += got
            status.append(f"{fn.__name__[5:]}: {len(got)}")
        except Exception as e:  # one broken feed must not kill the run
            status.append(f"{fn.__name__[5:]}: FAILED ({type(e).__name__})")
    return jobs, status


# ------------------------------------------------------------------ scoring --
def location_points(loc, text):
    """Return (points, note) or None if you almost certainly can't apply."""
    l = (loc or "").lower()
    if RESTRICTED_TEXT.search(text):
        return None
    if any(k in l for k in ("nigeria", "africa")):
        return 20, "open to Nigeria/Africa"
    if any(k in l for k in ("worldwide", "anywhere", "global", "any location", "international", "world")):
        return 15, "worldwide"
    if "emea" in l:
        return 10, "EMEA"
    if not l.strip() or l.strip() in ("remote", "remote job"):
        return 3, "location not stated - check before applying"
    return None  # a specific country/region list that doesn't include you


def score(j):
    """Return (score, matched_keywords, location_note) or None to drop."""
    title, text = j["title"], f'{j["title"]} {" ".join(j["tags"])} {j["desc"]}'
    low = text.lower()
    if not TITLE_OK.search(title) or TITLE_BAD.search(title):
        return None
    if j["posted"] and datetime.now(timezone.utc) - j["posted"] > timedelta(days=MAX_AGE_DAYS):
        return None
    loc = location_points(j["location"], low)
    if loc is None:
        return None
    pts, note = loc

    t = title.lower()
    if re.search(r"full[\s-]?stack", t):
        pts += 25
    elif re.search(r"front[\s-]?end|back[\s-]?end", t):
        pts += 20
    else:
        pts += 15

    stack, matched = 0, []
    for name, (rx, w) in STACK.items():
        if re.search(rx, low):
            stack += w
            matched.append(name)
            if re.search(rx, t):
                stack += 3
    pts += min(stack, 50)

    if re.search(r"staff|principal|architect|head of|director", t):
        pts -= 30
    elif re.search(r"\bsenior\b|\bsr\b\.?", t):
        pts -= 12
    elif re.search(r"\blead\b", t):
        pts -= 10
    if re.search(r"junior|\bjr\b|associate|mid[\s-]?level|intermediate", t):
        pts += 5
    if re.search(r"intern(ship)?\b", t):
        pts -= 20

    yrs = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d+\s*)?(?:years|yrs)", low)]
    if yrs:
        y = min(yrs)
        pts += 5 if y <= 3 else -8 if y == 5 else -18 if y in (6, 7) else -30 if y >= 8 else 0

    if j["posted"]:
        age = (datetime.now(timezone.utc) - j["posted"]).days
        pts += 10 if age <= 2 else 6 if age <= 7 else 3 if age <= 14 else 0

    return max(0, min(100, pts)), matched, note


def tier_of(s):
    for name, cut in TIERS:
        if s >= cut:
            return name
    return None


def rank(jobs):
    seen_keys, ranked = set(), []
    for j in jobs:
        res = score(j)
        if not res or not j["url"]:
            continue
        key = (j["company"].lower(), j["title"].lower())
        if key in seen_keys or j["url"] in seen_keys:
            continue
        seen_keys.update((key, j["url"]))
        s, matched, note = res
        t = tier_of(s)
        if t:
            ranked.append({**j, "score": s, "matched": matched, "loc_note": note, "tier": t})
    ranked.sort(key=lambda x: (-x["score"], x["posted"] is None))
    return ranked


# -------------------------------------------------------------------- state --
def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen, new_urls):
    now = datetime.now(timezone.utc)
    for u in new_urls:
        seen[u] = now.isoformat()
    cutoff = now - timedelta(days=SEEN_KEEP_DAYS)
    seen = {u: d for u, d in seen.items() if (parse_date(d) or now) > cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=1, sort_keys=True)


# ------------------------------------------------------------------- output --
def render(ranked, status):
    e = html.escape
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    parts = [
        "<div style='font-family:Arial,sans-serif;max-width:760px;margin:auto;color:#1a1a1a'>",
        f"<h2 style='margin-bottom:4px'>Your remote dev roles are ready</h2>",
        f"<p style='color:#666;margin-top:0'>{e(today)} &middot; {len(ranked)} new matches "
        "(scores are keyword-fit estimates against your CV; always read the posting)</p>",
    ]
    text = [f"Your remote dev roles are ready - {today}", f"{len(ranked)} new matches", ""]
    for tier, _ in TIERS:
        rows = [r for r in ranked if r["tier"] == tier][:MAX_PER_TIER]
        if not rows:
            continue
        parts.append(f"<h3 style='border-bottom:2px solid #ddd;padding-bottom:4px'>{e(tier)} ({len(rows)})</h3>")
        text.append(f"== {tier} ({len(rows)}) ==")
        for r in rows:
            age = ""
            if r["posted"]:
                d = (datetime.now(timezone.utc) - r["posted"]).days
                age = "today" if d <= 0 else f"{d}d ago"
            meta = " &middot; ".join(
                e(x) for x in (r["company"], r["location"] or "remote", age, r["salary"], r["source"]) if x
            )
            parts.append(
                "<div style='margin:10px 0;padding:10px;border:1px solid #e3e3e3;border-radius:6px'>"
                f"<b>{r['score']}/100</b> &nbsp;<a href='{e(r['url'])}'>{e(r['title'])}</a><br>"
                f"<span style='color:#555;font-size:13px'>{meta}</span><br>"
                f"<span style='color:#2a6;font-size:13px'>Matches: {e(', '.join(r['matched']) or 'n/a')}</span>"
                f" <span style='color:#a60;font-size:13px'>&middot; {e(r['loc_note'])}</span></div>"
            )
            text.append(f"[{r['score']}] {r['title']} - {r['company']} ({r['location'] or 'remote'})\n    {r['url']}")
        text.append("")
    parts.append(
        "<p style='color:#888;font-size:12px'>Sources: " + e(" | ".join(status)) +
        ". Job data from Remotive, Remote OK, We Work Remotely and Jobicy - "
        "please open the original postings to apply.</p></div>"
    )
    return "\n".join(parts), "\n".join(text)


def send_email(subject, html_body, text_body):
    user, pwd = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("EMAIL_FROM", user), to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)


# --------------------------------------------------------------------- main --
def selftest():
    now = datetime.now(timezone.utc).isoformat()
    samples = [
        job("t", "Full-Stack Engineer (Next.js, TypeScript)", "A", "https://a", "Worldwide",
            "We use Next.js, React, TypeScript, Supabase, Postgres, Playwright. 3+ years.", now),
        job("t", "Senior Backend Engineer (Java)", "B", "https://b", "Worldwide", "Java Spring", now),
        job("t", "Frontend Developer", "C", "https://c", "USA only", "React", now),
        job("t", "Backend Developer Node.js", "D", "https://d", "Africa", "Node, Express, Postgres, REST API. 2 years.", now),
        job("t", "Staff Full Stack Engineer", "E", "https://e", "Worldwide", "React Node 10+ years", now),
        job("t", "Full Stack Developer", "F", "https://f", "Remote", "Must be located in the US. React", now),
    ]
    for r in rank(samples):
        print(r["score"], r["tier"], "-", r["title"], "|", r["loc_note"])
    got = {r["company"] for r in rank(samples)}
    assert "A" in got and "D" in got and not ({"B", "C", "F"} & got), got
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    jobs, status = fetch_all()
    print("Fetched:", "; ".join(status))
    if not jobs:
        sys.exit("No jobs fetched from any source - check the network/feeds.")
    seen = load_seen()
    ranked = [r for r in rank(jobs) if r["url"] not in seen]
    print(f"{len(ranked)} new matches")

    html_body, text_body = render(ranked, status)
    if args.dry_run:
        with open("digest.html", "w") as f:
            f.write(html_body)
        print("Wrote digest.html (no email sent, no state saved)")
        return
    if ranked or os.environ.get("SEND_EMPTY") == "1":
        high = sum(r["tier"] == TIERS[0][0] for r in ranked)
        subject = f"{len(ranked)} new remote dev roles ready ({high} high-probability) - {datetime.now():%d %b}"
        send_email(subject, html_body, text_body)
        print("Email sent")
    save_seen(seen, [r["url"] for r in ranked])


if __name__ == "__main__":
    main()
