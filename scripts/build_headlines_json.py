# scripts/build_headlines_json.py
"""
Build a Base44-friendly JSON file of international affairs headlines.

Outputs: public/headlines.json
Format: [{ "title": "...", "url": "...", "source": "...", "publishedAt": "..." }]

Goal:
- 20 unique, major, international affairs stories
- NO US domestic policy/news (unless clearly foreign policy / intl context)
- Return ONLY one link/headline per story (even if multiple sources cover it)
- Update on a schedule (recommended: GitHub Actions every 4 hours)

Install deps (requirements.txt):
  feedparser
  requests
  python-dateutil
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import feedparser
import requests
from dateutil import parser as dtparser


# ---------------- CONFIG ----------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 20
MAX_RETRIES = 3
RETRY_SLEEP = 1.2

WINDOW_HOURS = 72
LIMIT = 20

# Open-access world/international RSS feeds (no Google News, no AP, no Reuters)
OFFICIAL_RSS: Dict[str, str] = {
    "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "PBS": "https://www.pbs.org/newshour/feeds/rss/world",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "DW": "https://rss.dw.com/rdf/rss-en-world",
    "CBC": "https://rss.cbc.ca/lineup/world.xml",
    "UN News": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "NPR": "https://www.npr.org/rss/rss.php?id=1004",  # NPR World
}

# ---- Filtering strategy ----
# 1) Keep if clear foreign policy / international context
# 2) Otherwise drop if it looks US domestic by title or URL path patterns

FOREIGN_POLICY_HINTS = [
    "state department", "pentagon", "national security",
    "diplom", "sanction", "tariff", "trade", "nato", "un ", "united nations",
    "ceasefire", "peace talks", "embassy", "ambassador", "summit",
    "g7", "g20", "eu", "european union", "asean", "opec",
    "ukraine", "russia", "china", "taiwan", "iran", "israel", "gaza",
    "syria", "yemen", "venezuela", "myanmar", "sudan", "haiti",
    "north korea", "south korea", "saudi", "uae", "qatar", "iraq",
    "pakistan", "india", "afghanistan", "japan", "philippines",
    "red sea", "strait", "missile", "drone", "hostage", "coup", "military",
]

US_DOMESTIC_MARKERS = [
    "campaign", "primary", "caucus", "ballot", "election", "polls",
    "congress", "senate", "house", "governor", "mayor",
    "supreme court", "federal court", "trial", "indicted", "sentenced", "arrested",
    "ice ", "border czar", "border patrol",
    "wall street", "dow", "nasdaq",
    "nfl", "nba", "mlb", "grammys", "oscars",
]

US_STATE_WORDS = [
    "alabama","alaska","arizona","arkansas","california","colorado",
    "connecticut","delaware","florida","georgia","hawaii","idaho",
    "illinois","indiana","iowa","kansas","kentucky","louisiana",
    "maine","maryland","massachusetts","michigan","minnesota",
    "mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york",
    "north carolina","north dakota","ohio","oklahoma","oregon",
    "pennsylvania","rhode island","south carolina","south dakota",
    "tennessee","texas","utah","vermont","virginia","washington",
    "west virginia","wisconsin","wyoming",
]

US_COUNTRY_TERMS = ["u.s.", "us ", "united states", "washington"]

# Tight URL-based domestic blockers (outlet-specific)
OUTLET_DOMESTIC_URL_BLOCKLIST: Dict[str, List[str]] = {
    "PBS": ["/politics/", "/nation/", "/economy/"],
    "BBC": ["/news/us", "/news/world/us", "/news/articles/cq", "/news/articles/c8"],  # BBC can mix; URL hints help a bit
    "The Guardian": ["/us-news", "/world/usa", "/us/"],
    "NPR": ["/sections/politics/", "/sections/national/", "/sections/business/"],
    "CBC": ["/canada", "/business", "/politics"],
    # DW/UN News are typically international; keep minimal
}

# Title cleanup: remove presentation junk
TITLE_PREFIXES_TO_DROP = [
    r"^watch( now)?:\s*",
    r"^live( now)?:\s*",
    r"^video:\s*",
    r"^analysis:\s*",
]
TITLE_SUFFIXES_TO_DROP = [
    r"\s*-\s*ap news\s*$",
    r"\s*-\s*reuters\s*$",
    r"\s*-\s*bbc news\s*$",
    r"\s*-\s*pbs newshour\s*$",
    r"\s*\|\s*pbs news(hour)?\s*$",
    r"\s*\|\s*bbc news\s*$",
    r"\s*\|\s*npr\s*$",
    r"\s*-\s*cbc news\s*$",
]


# ---------------- HELPERS ----------------

def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())

def _hash(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()

def clean_headline(title: str) -> str:
    if not title:
        return ""
    t = title.strip()
    for pat in TITLE_PREFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    for pat in TITLE_SUFFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def strip_utm(url: str) -> str:
    try:
        u = urlparse(url)
        qs = parse_qs(u.query, keep_blank_values=True)
        for p in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
            qs.pop(p, None)
        new_query = "&".join(f"{k}={v[0]}" for k, v in qs.items() if v)
        return u._replace(query=new_query).geturl()
    except Exception:
        return url

def fetch_text(url: str) -> Optional[str]:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(RETRY_SLEEP * attempt)
    return None

def parse_dt(entry: dict) -> Optional[datetime]:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(k)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for k in ("published", "updated", "created"):
        v = entry.get(k)
        if v:
            try:
                dt = dtparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None

def _url_path(url: str) -> str:
    try:
        return (urlparse(url).path or "").lower()
    except Exception:
        return (url or "").lower()

def _blocked_by_outlet_url(source: str, url: str) -> bool:
    path = _url_path(url)
    for seg in OUTLET_DOMESTIC_URL_BLOCKLIST.get(source, []):
        if seg in path:
            return True
    return False

def is_us_domestic(title: str, url: str, source: str) -> bool:
    """
    True = drop (US domestic)
    False = keep
    """
    t = _norm(title)
    path = _url_path(url)

    # If it's clearly foreign policy / international, keep even if it mentions US
    if any(h in t for h in FOREIGN_POLICY_HINTS):
        return False

    # Outlet-specific domestic URL blockers (tightening)
    if _blocked_by_outlet_url(source, url):
        return True

    # General URL domestic sections
    if any(seg in path for seg in ("/politics/", "/nation/", "/us/", "/u.s/", "/usa/")):
        return True

    # Title-based domestic markers
    if any(m in t for m in US_DOMESTIC_MARKERS):
        return True

    # Strong US framing without foreign-policy hints
    if any(c in t for c in US_COUNTRY_TERMS):
        return True

    # State/local references
    if any(state in t for state in US_STATE_WORDS):
        return True

    return False

def similarity_key(title: str) -> str:
    t = _norm(title)
    for ch in [":", "-", "—", "–", "(", ")", "[", "]", ",", ".", "!", "?", '"', "'"]:
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    return " ".join(t.split()[:12])

def fetch_feed(source: str, url: str, window_hours: int) -> List[dict]:
    txt = fetch_text(url)
    if not txt:
        return []
    d = feedparser.parse(txt)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    items: List[dict] = []
    for e in getattr(d, "entries", []):
        raw_title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not raw_title or not link:
            continue

        title = clean_headline(raw_title)
        link = strip_utm(link)

        if not title or not link:
            continue

        dt = parse_dt(e)
        if dt and dt < cutoff:
            continue

        if is_us_domestic(title, link, source):
            continue

        items.append({
            "title": title,
            "url": link,
            "source": source,
            "publishedAt": dt.isoformat().replace("+00:00", "Z") if dt else None,
        })
    return items

def rank_and_select_unique(items: List[dict], limit: int) -> List[dict]:
    """
    Approx “most talked about”:
      - group similar stories across sources
      - prefer clusters with more source coverage
      - then prefer newer
    Return ONE representative item per story cluster.
    """
    seen_exact = set()
    exact: List[dict] = []
    for it in items:
        k = _hash(it.get("source", ""), _norm(it.get("title", "")))
        if k not in seen_exact:
            seen_exact.add(k)
            exact.append(it)

    groups: Dict[str, List[dict]] = {}
    for it in exact:
        groups.setdefault(similarity_key(it["title"]), []).append(it)

    ranked: List[tuple] = []
    for _, group in groups.items():
        rep = group[0]
        rep_ts = 0.0
        for g in group:
            try:
                ts = dtparser.parse(g["publishedAt"]).timestamp() if g.get("publishedAt") else 0.0
            except Exception:
                ts = 0.0
            if ts >= rep_ts:
                rep, rep_ts = g, ts

        unique_sources = len(set(g["source"] for g in group))
        score = (unique_sources * 1_000_000) + rep_ts
        ranked.append((score, rep))

    ranked.sort(key=lambda x: x[0], reverse=True)

    out: List[dict] = []
    seen_story = set()
    for _, rep in ranked:
        sk = similarity_key(rep["title"])
        if sk in seen_story:
            continue
        seen_story.add(sk)
        out.append(rep)
        if len(out) >= limit:
            break
    return out

def main():
    all_items: List[dict] = []
    for src, url in OFFICIAL_RSS.items():
        all_items.extend(fetch_feed(src, url, WINDOW_HOURS))

    final = rank_and_select_unique(all_items, LIMIT)

    out_dir = Path("public")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "headlines.json"
    out_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Wrote {len(final)} unique headlines to {out_path.resolve()}")

if __name__ == "__main__":
    main()
