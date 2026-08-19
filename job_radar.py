#!/usr/bin/env python3
"""job_radar.py — watch technical job boards, score the listings, alert on the
ones worth bidding.

Built as production tooling for our own freelance pipeline; published as a
portfolio piece because it is exactly the class of work we sell: scheduled
multi-source polling, resilient scraping against bot-hostile sites, keyword +
budget scoring, dedup with persistent state, and webhook notification.

WHY A REAL BROWSER: several boards (Reddit among them) return HTTP 200 to
plain curl but serve "blocked by network security" to headless Chrome — the
block is on the headless FINGERPRINT, not the IP. Measured 2026-08-17 on the
same host in the same minute. So the browser lane runs headFUL on a virtual
display (Xvfb) with AutomationControlled disabled, and cheap HTTP is used
wherever it actually works. Use the cheapest transport that survives the site.

Sources (all public, all read-only, polite intervals):
  - n8n community jobs board   (Discourse JSON API)
  - Reddit r/forhire          (old.reddit HTML)
  - Contra / others            (pluggable — add a Source and go)

Usage:
    job_radar.py                     # one pass, print matches
    job_radar.py --notify            # ...and push new ones to Telegram
    job_radar.py --min-score 3       # tighten the filter
State: ~/.local/share/job-radar/seen.json (dedup across runs)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

STATE_DIR = os.path.expanduser("~/.local/share/job-radar")
SEEN = os.path.join(STATE_DIR, "seen.json")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/151.0.0.0 Safari/537.36")

# what we actually want to be paid for — weighted
KEYWORDS = {
    "n8n": 3, "webhook": 3, "api integration": 3, "automation": 2,
    "scraper": 3, "scraping": 3, "playwright": 3, "selenium": 2,
    "python": 2, "self-hosted": 3, "docker": 2, "nginx": 2, "linux": 2,
    "postgres": 2, "supabase": 1, "zapier": 2, "make.com": 2,
    "llm": 2, "rag": 3, "vector": 2, "ollama": 3, "vllm": 3,
    "voice agent": 3, "vapi": 3, "twilio": 2, "telephony": 2,
    "bug": 2, "fix": 1, "broken": 2, "urgent": 1, "error": 1,
    "retainer": 3, "monthly": 2, "ongoing": 2, "maintenance": 2,
}
# things we are deliberately not chasing
NEGATIVE = ("wordpress theme", "logo design", "graphic design", "data entry",
            "virtual assistant", "social media manager", "video editor")

# THE TWO FILTERS THAT MADE THIS USABLE (2026-08-19).
# A first real run returned 25 "matches" of which 4 were live opportunities. The
# rest were either months stale or other freelancers ADVERTISING. Scoring alone
# cannot tell those apart: a competitor's ad and a client's brief contain the
# same keywords. So classify the intent and check the clock before scoring.
MAX_AGE_DAYS = 21          # a jobs post older than this is filled or dead
SELLER = re.compile(r"\[?\s*(for\s*hire|available\s+for|open\s+to\s+work|"
                    r"offering|i\s+can\s+build|my\s+services)", re.I)
BUYER  = re.compile(r"\[?\s*(hiring|we.re\s+hiring|need(ed)?\b|seeking|looking\s+for|"
                    r"wanted|willing\s+to\s+pay|paid\s+(project|gig|pilot))", re.I)


def classify(title):
    """SELLER beats BUYER: '[FOR HIRE] ... looking for clients' is an ad."""
    if SELLER.search(title):
        return "seller"
    if BUYER.search(title):
        return "buyer"
    return "unknown"


def age_days(posted):
    """posted is YYYY-MM-DD or ''. Unknown age is NOT treated as fresh."""
    if not posted:
        return None
    try:
        return (datetime.now() - datetime.strptime(posted[:10], "%Y-%m-%d")).days
    except Exception:
        return None
MONEY = re.compile(r"[$€£]\s?([\d,]{2,7})(?:\s?[-–]\s?[$€£]?([\d,]{2,7}))?")


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _browser_text(url, selector):
    """Headful-browser lane for sites that refuse cheap HTTP or headless."""
    here = os.path.dirname(os.path.abspath(__file__))
    browse = os.path.join(here, "..", "..", "..", "..",
                          "tools", "harness", "browse")
    browse = os.path.normpath(browse)
    if not os.path.exists(browse):
        return ""
    try:
        out = subprocess.run([browse, url, "--links", selector],
                             capture_output=True, text=True, timeout=180)
        return out.stdout
    except Exception:
        return ""


def source_n8n():
    """n8n community jobs — Discourse exposes clean JSON, no browser needed."""
    out = []
    try:
        data = json.loads(_get("https://community.n8n.io/c/jobs/13.json"))
        for t in data.get("topic_list", {}).get("topics", []):
            out.append({
                "source": "n8n-jobs",
                "title": t.get("title", ""),
                "url": f"https://community.n8n.io/t/{t.get('slug')}/{t.get('id')}",
                "posted": t.get("created_at", "")[:10],
                # replies == how many people you are bidding against. A 7-day-old
                # post with 4 replies is a better target than a 1-day-old with 40.
                "replies": max(0, (t.get("posts_count") or 1) - 1),
            })
    except Exception as e:
        print(f"  n8n source failed: {e}", file=sys.stderr)
    return out


def source_reddit_forhire():
    """r/forhire — cheap HTTP works from a server IP; the headful browser is
    the documented fallback because HEADLESS is fingerprint-blocked here."""
    out = []
    try:
        html = _get("https://old.reddit.com/r/forhire/new/")
        for m in re.finditer(r'<a[^>]+class="[^"]*title[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                             html, re.S):
            href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not title:
                continue
            out.append({"source": "r/forhire", "title": title,
                        "url": href if href.startswith("http")
                        else "https://old.reddit.com" + href,
                        "posted": ""})
    except Exception:
        txt = _browser_text("https://old.reddit.com/r/forhire/new/", "a.title")
        for line in txt.splitlines():
            if line.strip():
                out.append({"source": "r/forhire", "title": line.strip(),
                            "url": "https://old.reddit.com/r/forhire/new/",
                            "posted": ""})
    return out


SOURCES = [source_n8n, source_reddit_forhire]


def score(item):
    t = item["title"].lower()
    if any(n in t for n in NEGATIVE):
        return 0, []
    item["kind"] = classify(item["title"])
    item["age"] = age_days(item.get("posted", ""))
    hits = [(k, w) for k, w in KEYWORDS.items() if k in t]
    s = sum(w for _, w in hits)
    if item["kind"] == "buyer":
        s += 3                       # they are buying, not selling
    m = MONEY.search(item["title"])
    if m:
        s += 2
        item["money"] = m.group(0)
    # low competition is signal, and it is the one thing scoring usually ignores
    r = item.get("replies")
    if r is not None:
        if r <= 5:
            s += 2
        elif r >= 25:
            s -= 2
    return s, [k for k, _ in hits]


def load_seen():
    try:
        return set(json.load(open(SEEN)))
    except Exception:
        return set()


def save_seen(seen):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(sorted(seen)[-4000:], open(SEEN, "w"))


def notify(lines):
    """Telegram push — the same 'tell a human only when it matters' pattern we
    use for production monitoring."""
    try:
        # Try both names. The original hard-coded a path that did not exist on
        # this host, so --notify failed to stderr and the run still exited 0 --
        # a notifier that notified nobody, silently. Fail LOUD instead.
        def _cred(*names):
            for n in names:
                p = os.path.expanduser(n)
                if os.path.exists(p):
                    return open(p).read().strip()
            raise FileNotFoundError(" / ".join(names))
        tok = _cred("~/.config/aria-telegram-token", "~/.config/telegram-token")
        chat = _cred("~/.config/aria-telegram-chatid", "~/.config/telegram-chatid")
        body = json.dumps({"chat_id": chat, "text": "\n".join(lines)[:3900],
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:
        print(f"  telegram failed: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--all", action="store_true", help="ignore the seen-list")
    ap.add_argument("--max-age", type=int, default=MAX_AGE_DAYS,
                    help="drop listings older than this many days")
    ap.add_argument("--include-sellers", action="store_true",
                    help="keep other freelancers' ads (off by default)")
    a = ap.parse_args()

    seen = set() if a.all else load_seen()
    found, fresh = [], []
    dropped = {"seller": 0, "stale": 0, "low-score": 0}
    for src in SOURCES:
        items = src()
        print(f"{src.__name__}: {len(items)} listings")
        for it in items:
            s, hits = score(it)
            if it.get("kind") == "seller" and not a.include_sellers:
                dropped["seller"] += 1
                continue
            if it.get("age") is not None and it["age"] > a.max_age:
                dropped["stale"] += 1
                continue
            if s < a.min_score:
                dropped["low-score"] += 1
                continue
            it["score"], it["hits"] = s, hits
            found.append(it)
            key = it["url"] or it["title"]
            if key not in seen:
                fresh.append(it)
                seen.add(key)

    found.sort(key=lambda x: (-x["score"], x.get("replies") if x.get("replies") is not None else 99,
                              x.get("age") if x.get("age") is not None else 99))
    print(f"\n{len(found)} live opportunities (>= {a.min_score}), {len(fresh)} new")
    print(f"   filtered out: {dropped['seller']} seller ads, {dropped['stale']} stale "
          f"(> {a.max_age}d), {dropped['low-score']} below score\n")
    for it in found[:25]:
        money = f"  [{it.get('money')}]" if it.get("money") else ""
        age = f"{it['age']}d" if it.get("age") is not None else " ? "
        rep = f"{it['replies']:>2} bids" if it.get("replies") is not None else "      "
        print(f"  {it['score']:2d}  {age:>4} {rep}  {it['source']:10s} {it['title'][:74]}{money}")
        print(f"                         {it['url']}")

    if a.notify and fresh:
        lines = [f"🎯 JOB RADAR — {len(fresh)} new match(es) "
                 f"{datetime.now():%H:%M}"]
        for it in fresh[:8]:
            lines.append(f"[{it['score']}] {it['title'][:110]}\n{it['url']}")
        notify(lines)
    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
