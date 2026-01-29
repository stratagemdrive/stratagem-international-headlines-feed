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
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",  # broad feed; we filter hard
    "DW": "https://rss.dw.com/rdf/rss-en-world",
    "CBC": "https://rss.cbc.ca/lineup/world.xml",
    "UN News": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "NPR": "https://www.npr.org/rss/rss.php?id=1004",  # NPR World
}

# Title cleanup
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

# ---- International affairs gate ----
# If a story doesn't look like "international affairs", drop it even if it's international.

# Keywords that strongly indicate international affairs / geopolitics
AFFAIRS_KEYWORDS = [
    # diplomacy/governance
    "diplom", "sanction", "tariff", "trade deal", "embassy", "ambassador",
    "summit", "treaty", "negotiat", "ceasefire", "peace talks", "talks",
    "united nations", "un ", "security council", "human rights council",
    "european union", "eu ", "nato", "asean", "opec", "g7", "g20",
    "imf", "world bank", "wto",

    # conflict/security
    "war", "invasion", "strike", "airstrike", "missile", "drone", "rocket",
    "shelling", "front line", "troops", "military", "defence", "defense",
    "armed", "insurgent", "militant", "hostage", "prisoner", "siege",
    "terror", "terrorist", "attack", "bomb", "explosion",

    # politics abroad / state power
    "election", "referendum", "coup", "protest", "crackdown",
    "parliament", "president", "prime minister", "opposition",

    # crisis / forced movement
    "refugee", "migrant", "displaced", "humanitarian", "aid convoy",
]

# Non-US geographic anchors (very lightweight list — just enough to avoid false positives)
# This acts as a fallback: if it mentions a non-US place + isn't a banned topic, it's likely okay.
NON_US_ANCHORS = [
    "ukraine","russia","moscow","kyiv","kiev",
    "china","beijing","shanghai","taiwan","hong kong",
    "iran","tehran","israel","gaza","jerusalem","hamas","hezbollah",
    "syria","damascus","yemen","saudi","uae","qatar","iraq",
    "afghanistan","pakistan","india",
    "north korea","south korea","seoul","pyongyang","japan","tokyo",
    "philippines","vietnam","thailand","myanmar",
    "sudan","ethiopia","somalia","nigeria","congo","sahel",
    "venezuela","cuba","haiti",
    "europe","germany","france","italy","spain","poland","uk ",
    "london","brussels","geneva","the hague",
]

# Topics we usually do NOT want in an "international affairs" feed
# (unless the title also has clear affairs keywords)
NON_AFFAIRS_BLOCKLIST = [
    # pure finance/markets/companies
    "stocks", "shares", "wall street", "earnings", "profit", "quarter",
    "microsoft", "apple", "google", "meta", "tesla", "bitcoin", "crypto",
    "gold", "oil price", "market", "bond", "nasdaq", "dow",

    # lifestyle/entertainment
    "movie", "film", "music", "album", "celebrity", "fashion",
    "oscars", "grammys",

    # sports
    "football", "soccer", "nba", "nfl", "mlb", "tennis", "olympics",

    # science/animals/human interest that isn't geopolitics
    "polar bear", "recipe", "health", "diet", "travel", "weather",
    "wildfire", "hurricane", "tornado", "blizzard",
]

# ---- US domestic blocker ----

FOREIGN_POLICY_HINTS = [
    "state department", "pentagon", "national security",
    "diplom", "sanction", "tariff", "trade", "nato", "un ", "united nations",
    "summit", "g7", "g20", "eu", "european union",
    "ukraine", "russia", "china", "taiwan", "iran", "israel", "gaza",
]

US_DOMESTIC_MARKERS = [
    "campaign", "primary", "caucus", "ballot", "election", "polls",
    "congress", "senate", "house", "governor", "mayor",
    "supreme court", "federal court", "trial", "indicted", "sentenced", "arrested",
    "ice ", "border czar", "border patrol",
    "school board", "district attorney",
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

US_COUNTRY_TERMS = ["u.s.", " us ", "united states", "washington"]

# Outlet-specific domestic URL blockers (extra tight)
OUTLET_DOMESTIC_URL_BLOCKLIST: Dict[str, List[str]] = {
    "PBS": ["/politics/", "/nation/", "/economy/", "/arts/", "/science/", "/health/"],
    "The Guardian": ["/us-news", "/world/usa", "/us/"],
    "NPR": ["/sections/politics/", "/sections/national/", "/sections/business/", "/sections/health/"],
    "CBC": ["/canada", "/business", "/politics"],
    # BBC World RSS is usually okay; still block obvious US sections
    "BBC": ["/news/us", "/news/world/us"],
}


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

    # If it screams foreign policy / international, keep
    if any(h in t for h in FOREIGN_POLICY_HINTS):
        return False

    # Outlet-specific domestic URL blockers
    if _blocked_by_outlet_url(source, url):
        return True

    # Generic domestic URL sections
    if any(seg in path for seg in ("/politics/", "/nation/", "/us/", "/u.s/", "/usa/")):
        return True

    # Domestic markers in title
    if any(m in t for m in US_DOMESTIC_MARKERS):
        return True

    # Strong US framing without foreign-policy hints
    if any(c in t for c in US_COUNTRY_TERMS):
        return True

    # State/local references
    if any(state in t for state in US_STATE_WORDS):
        return True

    return False

def looks_like_international_affairs(title: str) -> bool:
    """
    True = keep (looks like international affairs)
    False = drop
    """
    t = _norm(title)

    # If it matches strong affairs keywords, keep
    if any(k in t for k in AFFAIRS_KEYWORDS):
        return True

    # If it matches a non-affairs topic, drop (unless it ALSO matches affairs keywords)
    if any(b in t for b in NON_AFFAIRS_BLOCKLIST):
        return False

    # Otherwise, keep only if it references non-US anchors (countries/regions/institutions)
    if any(a in t for a in NON_US_ANCHORS):
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

        # Must be international affairs
        if not looks_like_international_affairs(title):
            continue

        # Must not be US domestic
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
