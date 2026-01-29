# scripts/build_headlines_json.py
"""
Build a Base44-friendly JSON file of international affairs headlines.

Outputs: public/headlines.json
Format: [{ "title": "...", "url": "...", "source": "...", "publishedAt": "..." }]

Goal:
- 20 unique, major, international affairs stories
- Focus on: major economic deals, trade agreements, sanctions, geopolitics, conflicts,
  diplomacy, security crises, and major abroad stories with US/global impact
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
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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

# -------- Headline cleaning / junk removal --------
TITLE_PREFIXES_TO_DROP = [
    r"^watch( now)?:\s*",
    r"^live( now)?:\s*",
    r"^video:\s*",
    r"^analysis:\s*",
    r"^explainer:\s*",
    r"^opinion:\s*",
    r"^what to know:\s*",
    r"^fact check:\s*",
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
    r"\s*\|\s*the guardian\s*$",
    r"\s*-\s*dw\s*$",
    r"\s*\|\s*dw\s*$",
]
# Remove trailing clutter like "(Video)", "[Update]", etc.
TITLE_TRAILING_BRACKETS = [
    r"\s*\((?:video|watch|live|updated?|update|analysis|opinion)\)\s*$",
    r"\s*\[(?:video|watch|live|updated?|update|analysis|opinion)\]\s*$",
]


# ---------------- AFFAIRS FOCUS FILTERS ----------------

HARD_KEEP = [
    # conflict/security
    "war", "invasion", "strike", "airstrike", "missile", "drone", "rocket",
    "shelling", "front line", "troops", "military", "defence", "defense",
    "armed", "insurgent", "militant", "terror", "terrorist", "attack", "bomb",
    "hostage", "prisoner", "siege", "blockade",

    # diplomacy / geopolitics / governance
    "diplom", "sanction", "tariff", "trade", "trade deal", "trade pact",
    "agreement", "deal", "talks", "negotiat", "treaty", "summit",
    "embassy", "ambassador", "normaliz", "recognition",
    "nato", "un ", "united nations", "security council", "eu ", "european union",
    "g7", "g20", "asean", "opec", "wto", "imf", "world bank",

    # energy / supply / strategic economy
    "oil", "gas", "lng", "pipeline", "shipping", "red sea", "strait",
    "supply chain", "export ban", "import ban", "shipping lane",
    "rare earth", "chip", "semiconductor",

    # major instability abroad
    "coup", "martial law", "protest", "crackdown", "uprising",
    "refugee", "migrant", "displaced", "humanitarian", "aid",
]

HARD_DROP = [
    # entertainment/sports/lifestyle
    "celebrity", "movie", "film", "music", "album", "fashion",
    "oscars", "grammys", "royal family",
    "nfl", "nba", "mlb", "tennis", "soccer", "football", "olympics",

    # soft science / animals / quirky human interest
    "polar bear", "recipe", "cooking", "diet", "wellness",
    "travel", "tourism", "festival",

    # consumer tech / product launches
    "iphone", "android", "netflix", "tiktok", "gaming",

    # narrow markets/earnings (unless trade/sanctions/energy/geopolitics)
    "earnings", "quarter", "shares", "stock", "stocks", "wall street", "nasdaq", "dow",
]

IMPORTANCE_HINTS = [
    "major", "crisis", "urgent", "deadly", "massive", "historic", "largest",
    "escalat", "standoff", "showdown", "collapse", "surge",
    "global", "worldwide", "international", "regional",
    "markets", "prices", "inflation", "growth", "recession",
    "shipping", "trade routes",
]

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

US_IMPACT_HINTS = [
    "u.s.", "united states", "washington",
    "american", "pentagon", "state department",
    "nato", "allies", "alliance",
    "sanction", "tariff", "trade", "chip", "semiconductor",
    "oil", "gas", "lng", "shipping", "supply chain",
]


# ---------------- US DOMESTIC BLOCKERS ----------------

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

OUTLET_DOMESTIC_URL_BLOCKLIST: Dict[str, List[str]] = {
    "PBS": ["/politics/", "/nation/", "/economy/", "/arts/", "/science/", "/health/"],
    "The Guardian": ["/us-news", "/world/usa", "/us/"],
    "NPR": ["/sections/politics/", "/sections/national/", "/sections/business/", "/sections/health/"],
    "CBC": ["/canada", "/business", "/politics"],
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

    # Drop common prefixes
    for pat in TITLE_PREFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)

    # Drop common suffix branding
    for pat in TITLE_SUFFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)

    # Drop trailing bracketed junk like "(Video)" or "[Update]"
    for pat in TITLE_TRAILING_BRACKETS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)

    # Remove double branding like " - Something | Something"
    t = re.sub(r"\s+[\|\-]\s*(?:news|newshour|world|international)\s*$", "", t, flags=re.IGNORECASE)

    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # Final: if title still starts with a label-like token, strip it
    t = re.sub(r"^(?:watch|live|video|analysis|opinion)\s*[:\-]\s*", "", t, flags=re.IGNORECASE).strip()

    return t

def canonicalize_url(url: str) -> str:
    """
    More aggressive URL normalization to reduce duplicates:
    - Remove utm params
    - Remove fragments
    - For some sites, remove trailing slashes
    """
    try:
        u = urlparse(url)
        qs = parse_qs(u.query, keep_blank_values=True)

        # Strip common tracking params
        drop_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "mc_cid", "mc_eid"
        }
        for p in list(qs.keys()):
            if p.lower() in drop_params:
                qs.pop(p, None)

        # Rebuild query
        new_q = urlencode({k: v[0] for k, v in qs.items() if v}, doseq=False)

        path = u.path or ""
        if path != "/" and path.endswith("/"):
            path = path[:-1]

        return urlunparse((u.scheme, u.netloc, path, u.params, new_q, ""))  # remove fragment
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

    if any(h in t for h in FOREIGN_POLICY_HINTS):
        return False

    if _blocked_by_outlet_url(source, url):
        return True

    if any(seg in path for seg in ("/politics/", "/nation/", "/us/", "/u.s/", "/usa/")):
        return True

    if any(m in t for m in US_DOMESTIC_MARKERS):
        return True

    if any(c in t for c in US_COUNTRY_TERMS):
        return True

    if any(state in t for state in US_STATE_WORDS):
        return True

    return False

def looks_like_international_affairs(title: str) -> bool:
    """
    True = keep
    False = drop
    """
    t = _norm(title)

    # Drop non-affairs (unless also has a hard keep signal)
    if any(b in t for b in HARD_DROP) and not any(k in t for k in HARD_KEEP):
        return False

    # Hard keep signals
    if any(k in t for k in HARD_KEEP):
        return True

    # Otherwise, require (non-US anchor) AND (importance OR US-impact framing)
    if any(a in t for a in NON_US_ANCHORS):
        if any(i in t for i in IMPORTANCE_HINTS) or any(u in t for u in US_IMPACT_HINTS):
            return True

    return False

def story_signature(title: str) -> str:
    """
    Stronger duplicate grouping:
    - normalize
    - remove stopwords & short tokens
    - take first ~10 informative tokens
    """
    t = _norm(title)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    tokens = [x for x in t.split() if len(x) >= 3]
    stop = {
        "the","and","for","with","from","that","this","after","over","into",
        "says","say","said","will","could","would","should","amid","about",
        "new","more","than","they","their","its","his","her","your","our",
        "report","reports","update","latest","live","watch"
    }
    tokens = [x for x in tokens if x not in stop]
    return " ".join(tokens[:10])

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
        link = canonicalize_url(link)

        if not title or not link:
            continue

        dt = parse_dt(e)
        if dt and dt < cutoff:
            continue

        # Must be international affairs (focused)
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
      - group similar stories across sources using story_signature
      - prefer clusters with more source coverage
      - then prefer newer
    Return ONE representative item per story cluster.
    """
    # Exact dedupe by (canonical url) first
    seen_url = set()
    url_dedup: List[dict] = []
    for it in items:
        u = it.get("url") or ""
        if u and u not in seen_url:
            seen_url.add(u)
            url_dedup.append(it)

    # Group by story signature
    groups: Dict[str, List[dict]] = {}
    for it in url_dedup:
        sig = story_signature(it["title"])
        if not sig:
            continue
        groups.setdefault(sig, []).append(it)

    ranked: List[Tuple[float, dict]] = []
    for _, group in groups.items():
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

        # score = cross-source boost + recency + slight boost if it has importance hints
        t = _norm(rep["title"])
        importance_bonus = 50_000 if any(i in t for i in IMPORTANCE_HINTS) else 0
        us_impact_bonus = 50_000 if any(i in t for i in US_IMPACT_HINTS) else 0

        score = (unique_sources * 1_000_000) + rep_ts + importance_bonus + us_impact_bonus
        ranked.append((score, rep))

    ranked.sort(key=lambda x: x[0], reverse=True)

    out: List[dict] = []
    seen_sig = set()
    for _, rep in ranked:
        sig = story_signature(rep["title"])
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
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
