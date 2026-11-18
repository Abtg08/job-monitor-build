#!/usr/bin/env python3
"""
Job Monitor — scans 200+ company career pages for analyst roles in India.
Runs via GitHub Actions (hourly cron). Sends email digests via Gmail SMTP.
"""

import json
import os
import re
import sys
import time
import hashlib
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, quote

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "manviahuja1212@gmail.com")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # Gmail App Password
SEEN_FILE = Path("seen_jobs.json")
COMPANIES_FILE = Path("companies.json")

# How many seconds to wait between API calls to be polite
REQUEST_DELAY = 0.5
REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# TITLE FILTERS
# ---------------------------------------------------------------------------
TITLE_POSITIVE = re.compile(
    r"\b("
    r"data.?analyst|business.?analyst|product.?analyst|marketing.?analyst|"
    r"growth.?analyst|bi.?analyst|revenue.?analyst|analytics.?analyst|"
    r"reporting.?analyst|insights.?analyst|commercial.?analyst|"
    r"decision.?analyst|strategy.?analyst|operations.?analyst|"
    r"customer.?analyst|digital.?analyst|performance.?analyst|"
    r"analyst.{0,10}(?:data|business|product|marketing|growth|bi|revenue|analytics|reporting)|"
    r"(?:junior|associate|entry).{0,10}analyst|"
    r"data.?analytics|business.?intelligence"
    r")\b",
    re.IGNORECASE,
)

TITLE_NEGATIVE = re.compile(
    r"\b("
    r"senior|sr\.?|staff|principal|lead|manager|director|vp|head|"
    r"(?:5|6|7|8|9|10|11|12|13|14|15)\+?\s*(?:years?|yrs?)|"
    r"intern(?!al)|trainee|apprentice"
    r")\b",
    re.IGNORECASE,
)

EXPERIENCE_REJECT = re.compile(
    r"\b(?:5|6|7|8|9|1[0-5])\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# LOCATION FILTERS
# ---------------------------------------------------------------------------
LOCATION_INDIA = re.compile(
    r"\b("
    r"india|bengaluru|bangalore|mumbai|delhi|ncr|gurgaon|gurugram|noida|"
    r"hyderabad|pune|chennai|kolkata|ahmedabad|jaipur|kochi|chandigarh|"
    r"remote.{0,20}india|india.{0,20}remote|pan.?india|anywhere.?in.?india|"
    r"all.?india|in\b"
    r")\b",
    re.IGNORECASE,
)

LOCATION_BENGALURU_BOOST = re.compile(
    r"\b(bengaluru|bangalore)\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# KEYWORD SCORING (higher = better match for Manvi's profile)
# ---------------------------------------------------------------------------
SKILL_KEYWORDS = {
    "sql": 3,
    "bigquery": 4,
    "marketing analytics": 5,
    "marketing attribution": 5,
    "attribution": 4,
    "retention": 4,
    "ltv": 4,
    "lifetime value": 4,
    "cohort": 3,
    "looker studio": 4,
    "looker": 3,
    "power bi": 3,
    "tableau": 3,
    "ga4": 3,
    "google analytics": 3,
    "python": 2,
    "pandas": 2,
    "mysql": 2,
    "postgresql": 2,
    "data pipeline": 3,
    "etl": 2,
    "ecommerce": 3,
    "e-commerce": 3,
    "d2c": 3,
    "shopify": 3,
    "stripe": 2,
    "northbeam": 4,
    "root cause": 3,
    "forecasting": 3,
    "a/b test": 3,
    "ab test": 3,
    "experimentation": 2,
    "dashboard": 2,
    "reporting": 2,
    "growth": 3,
    "paid media": 4,
    "performance marketing": 4,
    "cac": 3,
    "roas": 3,
    "arpu": 3,
    "funnel": 3,
}


def compute_score(title: str, description: str = "") -> int:
    """Score a job based on keyword matches in title + description."""
    text = f"{title} {description}".lower()
    score = 0
    matched = []
    for kw, pts in SKILL_KEYWORDS.items():
        if kw in text:
            score += pts
            matched.append(kw)
    # Bengaluru boost
    if LOCATION_BENGALURU_BOOST.search(text):
        score += 5
    return score, matched


# ---------------------------------------------------------------------------
# SEEN-JOBS CACHE (persisted across runs via GitHub Actions artifact/cache)
# ---------------------------------------------------------------------------
def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_seen(seen: dict):
    # Keep only last 30 days of entries to avoid bloat
    cutoff = time.time() - 30 * 86400
    trimmed = {k: v for k, v in seen.items() if v.get("ts", 0) > cutoff}
    SEEN_FILE.write_text(json.dumps(trimmed, indent=1))


def job_id(company: str, title: str, url: str) -> str:
    raw = f"{company}|{title}|{url}"
    return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ATS FETCHERS
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": "JobMonitor/1.0 (personal job alert, polite scraping)",
    "Accept": "application/json",
})


def fetch_greenhouse(slug: str) -> list:
    """Fetch jobs from Greenhouse boards API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = j.get("location", {}).get("name", "")
            jobs.append({
                "title": j.get("title", ""),
                "url": j.get("absolute_url", ""),
                "location": loc,
                "description": j.get("content", "")[:500] if j.get("content") else "",
                "id": str(j.get("id", "")),
            })
        return jobs
    except Exception as e:
        log.warning(f"Greenhouse [{slug}]: {e}")
        return []


def fetch_greenhouse_eu(slug: str) -> list:
    """Fetch jobs from Greenhouse EU boards."""
    url = f"https://job-boards.eu.greenhouse.io/api/v1/boards/{slug}/jobs"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = j.get("location", {}).get("name", "")
            jobs.append({
                "title": j.get("title", ""),
                "url": j.get("absolute_url", f"https://job-boards.eu.greenhouse.io/{slug}/jobs/{j.get('id','')}"),
                "location": loc,
                "description": "",
                "id": str(j.get("id", "")),
            })
        return jobs
    except Exception as e:
        log.warning(f"Greenhouse EU [{slug}]: {e}")
        return []


def fetch_lever(slug: str) -> list:
    """Fetch jobs from Lever postings API."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data:
            loc = j.get("categories", {}).get("location", "")
            desc_plain = j.get("descriptionPlain", "")[:500]
            jobs.append({
                "title": j.get("text", ""),
                "url": j.get("hostedUrl", ""),
                "location": loc,
                "description": desc_plain,
                "id": j.get("id", ""),
            })
        return jobs
    except Exception as e:
        log.warning(f"Lever [{slug}]: {e}")
        return []


def fetch_ashby(slug: str) -> list:
    """Fetch jobs from Ashby posting API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = j.get("location", "")
            jobs.append({
                "title": j.get("title", ""),
                "url": j.get("jobUrl", j.get("applyUrl", "")),
                "location": loc,
                "description": "",
                "id": j.get("id", ""),
            })
        return jobs
    except Exception as e:
        log.warning(f"Ashby [{slug}]: {e}")
        return []


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "greenhouse_eu": fetch_greenhouse_eu,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


# ---------------------------------------------------------------------------
# FILTERING
# ---------------------------------------------------------------------------
def is_relevant(job: dict) -> bool:
    """Return True if job title matches analyst roles and location is India."""
    title = job.get("title", "")
    loc = job.get("location", "")
    desc = job.get("description", "")
    combined = f"{title} {loc} {desc}"

    # Must match positive title pattern
    if not TITLE_POSITIVE.search(title):
        return False

    # Reject senior / high experience
    if TITLE_NEGATIVE.search(title):
        return False

    # Check location: either in location field or description
    if not LOCATION_INDIA.search(combined):
        return False

    # Reject if description explicitly asks 5+ years
    if EXPERIENCE_REJECT.search(desc):
        return False

    return True


# ---------------------------------------------------------------------------
# MAIN SCAN
# ---------------------------------------------------------------------------
def scan_all() -> list:
    """Scan all companies, return list of new relevant jobs."""
    companies = json.loads(COMPANIES_FILE.read_text())
    seen = load_seen()
    new_jobs = []
    stats = {"scanned": 0, "api_ok": 0, "api_fail": 0, "matched": 0, "new": 0}

    for co in companies:
        name = co["name"]
        system = co.get("system", "custom")
        slug = co.get("slug", "")

        # Skip custom career pages for now (would need HTML scraping)
        if system == "custom":
            stats["scanned"] += 1
            continue

        fetcher = FETCHERS.get(system)
        if not fetcher:
            continue

        stats["scanned"] += 1
        jobs = fetcher(slug)
        if jobs:
            stats["api_ok"] += 1
        else:
            stats["api_fail"] += 1

        for j in jobs:
            if not is_relevant(j):
                continue
            stats["matched"] += 1

            jid = job_id(name, j["title"], j["url"])
            if jid in seen:
                continue

            score, keywords = compute_score(j["title"], j.get("description", ""))
            entry = {
                "company": name,
                "title": j["title"],
                "location": j.get("location", ""),
                "url": j["url"],
                "score": score,
                "keywords": keywords,
                "sector": co.get("sector", ""),
                "found_at": datetime.now(timezone.utc).isoformat(),
            }
            new_jobs.append(entry)
            seen[jid] = {"ts": time.time()}
            stats["new"] += 1

        time.sleep(REQUEST_DELAY)

    save_seen(seen)
    log.info(
        f"Scan done — companies: {stats['scanned']}, "
        f"API OK: {stats['api_ok']}, API fail: {stats['api_fail']}, "
        f"matched: {stats['matched']}, NEW: {stats['new']}"
    )
    return sorted(new_jobs, key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
def build_email_html(jobs: list) -> str:
    """Build a nicely formatted HTML email digest."""
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    rows = []
    for i, j in enumerate(jobs, 1):
        kw_badges = " ".join(
            f'<span style="background:#e8f5e9;color:#2e7d32;padding:2px 6px;'
            f'border-radius:3px;font-size:11px;margin-right:3px;">{k}</span>'
            for k in j.get("keywords", [])[:5]
        )
        bengaluru = "📍" if LOCATION_BENGALURU_BOOST.search(j.get("location", "")) else ""
        rows.append(f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:10px 8px;vertical-align:top;">{i}</td>
          <td style="padding:10px 8px;">
            <a href="{j['url']}" style="color:#1a73e8;text-decoration:none;font-weight:600;">
              {j['title']}
            </a><br>
            <span style="color:#555;">{j['company']}</span>
            <span style="color:#888;font-size:12px;">({j.get('sector','')})</span>
          </td>
          <td style="padding:10px 8px;font-size:13px;">{j.get('location','')} {bengaluru}</td>
          <td style="padding:10px 8px;text-align:center;font-weight:bold;color:#1a73e8;">{j['score']}</td>
          <td style="padding:10px 8px;">{kw_badges}</td>
          <td style="padding:10px 8px;text-align:center;">
            <a href="{j['url']}" style="background:#1a73e8;color:white;padding:6px 14px;
              border-radius:4px;text-decoration:none;font-size:12px;font-weight:600;">Apply</a>
          </td>
        </tr>""")

    return f"""
    <html><body style="font-family:Inter,Arial,sans-serif;max-width:900px;margin:0 auto;padding:16px;">
      <div style="background:linear-gradient(135deg,#1a73e8,#6c47ff);color:white;padding:20px 24px;border-radius:10px;">
        <h1 style="margin:0;font-size:22px;">🔔 New Analyst Jobs Alert</h1>
        <p style="margin:6px 0 0;opacity:0.9;font-size:14px;">{len(jobs)} new jobs found · {now}</p>
      </div>
      <table style="width:100%;border-collapse:collapse;margin-top:16px;font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;text-align:left;">
            <th style="padding:8px;">#</th>
            <th style="padding:8px;">Role & Company</th>
            <th style="padding:8px;">Location</th>
            <th style="padding:8px;text-align:center;">Score</th>
            <th style="padding:8px;">Matching Skills</th>
            <th style="padding:8px;text-align:center;">Link</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:20px;text-align:center;">
        Scores: higher = better profile match.  📍 = Bengaluru.
        <br>Automated Job Monitor · GitHub Actions
      </p>
    </body></html>
    """


def send_email(jobs: list):
    """Send digest email via Gmail SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP credentials not set — skipping email. Set SMTP_USER and SMTP_PASS.")
        # Write to file as fallback
        Path("latest_jobs.json").write_text(json.dumps(jobs, indent=2))
        log.info(f"Wrote {len(jobs)} jobs to latest_jobs.json instead.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 {len(jobs)} New Analyst Jobs Found — {datetime.now().strftime('%d %b')}"
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENT_EMAIL

    html = build_email_html(jobs)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log.info(f"✅ Email sent to {RECIPIENT_EMAIL} with {len(jobs)} jobs")
    except Exception as e:
        log.error(f"❌ Email failed: {e}")
        Path("latest_jobs.json").write_text(json.dumps(jobs, indent=2))
        log.info("Wrote jobs to latest_jobs.json as fallback.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Job Monitor starting...")
    log.info("=" * 60)

    new_jobs = scan_all()

    if new_jobs:
        log.info(f"📬 {len(new_jobs)} new relevant jobs — sending digest")
        send_email(new_jobs)
    else:
        log.info("No new jobs this run.")

    # Always log a summary to stdout for GitHub Actions
    print(f"\n--- SUMMARY ---")
    print(f"New jobs found: {len(new_jobs)}")
    for j in new_jobs[:10]:
        print(f"  [{j['score']}] {j['title']} @ {j['company']} ({j['location']})")
    if len(new_jobs) > 10:
        print(f"  ... and {len(new_jobs) - 10} more")


if __name__ == "__main__":
    main()
