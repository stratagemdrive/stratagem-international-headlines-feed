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

import json
import hashlib
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
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15
MAX_RETRIES = 3
RETRY_SLEEP = 1.2

# How far back to look for candidate stories (hours)
WINDOW_HOURS = 72

# How many unique stories to output
LIMIT = 20

# Official RSS feeds
OFFICIAL_RSS: Dict[str, str] = {
    "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "PBS": "https://www.pbs.org/newshour/feeds/rss/headlines",
}

# Reuters/AP: practical “most talked about” proxy via Google News RSS rankings
GOOGLE_NEWS_RSS: Dict[str, str] = {
    "Reuters": (
        "https://news.google.com/rss/search?q="
        "site:reuters.com%20("
        "Ukraine%20OR%20Russia%20OR%20Gaza%20OR%20Israel%20OR%20Iran%20OR%20Syria%20OR%20"
        "China%20OR%20Taiwan%20OR%20North%20Korea%20OR%20NATO%20OR%20EU%20OR%20UN%20OR%20"
        "sanctions%20OR%20ceasefire%20OR%20diplomacy%20OR%20trade%20OR%20tariffs%20OR%20refugees%20OR%20coup%20OR%20election"
        ")"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "AP": (
        "https://news.google.com/rss/search?q="
        "site:apnews.com%20("
        "world%20OR%20international%20OR%20Ukraine%20OR%20Russia%20OR%20Gaza%20OR%20Israel%20OR%20Iran%20OR%20"
        "China%20OR%20Taiwan%20OR%20NATO%20OR%20EU%20OR%20UN%20OR%20sanctions%20OR%20ceasefire%20OR%20diplomacy%20OR%20refugees"
        ")"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
}

# Filters: exclude US domestic unless it clearly relates to foreign policy / intl context
FOREIGN_POLICY_HINTS = [
    "state department", "pentagon", "white house", "national security",
    "diplom", "sanction", "tariff", "trade deal", "nato", "un ", "united nations",
    "ceasefire", "peace talks", "embassy", "ambassador", "summit",
    "g7", "g20", "eu", "european union", "asean", "opec",
    "ukraine", "russia", "china", "taiwan", "iran", "israel", "gaza",
    "syria", "yemen", "venezuela", "myanmar", "sudan", "haiti",
]

US_DOMESTIC_EXCLUDE = [
    # US internal politics & governance
    "congress", "senate", "house gop", "house democrats", "supreme court",
    "primary", "campaign", "ballot", "governor", "mayor",
    "abortion", "gun", "medicare", "medicaid",
    "student loan", "tax bill", "budget fight", "shutdown",
    # US local & courts/crime
    "shooting", "homicide", "trial", "sentenced", "indicted", "arrested",
    # US weather/disasters (usually domestic)
    "wildfire", "tornado", "hurricane", "blizzard",
    # celebrity/sports
    "grammys", "oscars", "nfl", "nba", "mlb", "celebrity",
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

def is_us_domestic(title: str) -> bool:
    t = _norm(title)
    # If it has obvious foreign policy hints, keep it.
    if any(h in t for h in FOREIGN_POLICY_HINTS):
        return False

    # If it looks US-focused and no foreign-policy hints, drop
    if ("u.s." in t or "united states" in t or "washington" in t) and not any(h in t for h in FOREIGN_POLICY_HINTS):
        return True

    # Exclude if it contains US-domestic markers, unless it also has foreign context
    if any(bad in t for bad in US_DOMESTIC_EXCLUDE):
        foreign_terms = ["ukraine", "russia", "china", "taiwan", "iran", "israel", "gaza", "nato", "eu", "un "]
        if any(ft in t for ft in foreign_terms):
            return False
        return True

    return False

def similarity_key(title: str) -> str:
    t = _norm(title)
    for ch in [":", "-", "—", "–", "(", ")", "[", "]", ",", ".", "!", "?", '"', "'"]:
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    return " ".join(t.split()[:12])  # first ~12 words

def fetch_feed(source: str, url: str, window_hours: int) -> List[dict]:
    txt = fetch_text(url)
    if not txt:
        return []
    d = feedparser.parse(txt)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    items: List[dict] = []
    for e in getattr(d, "entries", []):
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        if is_us_domestic(title):
            continue

        dt = parse_dt(e)
        if dt and dt < cutoff:
            continue

        items.append({
            "title": title,
            "url": strip_utm(link),
            "source": source,
            "publishedAt": dt.isoformat().replace("+00:00", "Z") if dt else None,
        })
    return items

def rank_and_select_unique(items: List[dict], limit: int) -> List[dict]:
    """
    Approximate “most talked about”:
      - Google News RSS results already have attention-ish ranking
      - Boost stories that appear across multiple sources
      - Then newest first
    Return ONE representative item per story-group.
    """
    # exact dedupe
    seen_exact = set()
    exact = []
    for it in items:
        k = _hash(it["source"], _norm(it["title"]))
        if k not in seen_exact:
            seen_exact.add(k)
            exact.append(it)

    # group by similarity key
    groups: Dict[str, List[dict]] = {}
    for it in exact:
        groups.setdefault(similarity_key(it["title"]), []).append(it)

    ranked: List[tuple] = []
    for k, group in groups.items():
        # representative = newest item in the group
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
        # score: cross-source boost + recency
        score = (unique_sources * 1_000_000) + rep_ts
        ranked.append((score, rep))

    ranked.sort(key=lambda x: x[0], reverse=True)

    # final: ensure uniqueness (belt & suspenders)
    out = []
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

    # Pull from official RSS
    for src, url in OFFICIAL_RSS.items():
        all_items.extend(fetch_feed(src, url, WINDOW_HOURS))

    # Pull Reuters/AP via Google News RSS
    for src, url in GOOGLE_NEWS_RSS.items():
        all_items.extend(fetch_feed(src, url, WINDOW_HOURS))

    final = rank_and_select_unique(all_items, LIMIT)

    # Write output
    out_dir = Path("public")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "headlines.json"
    out_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Wrote {len(final)} unique headlines to {out_path.resolve()}")

if __name__ == "__main__":
    main()
