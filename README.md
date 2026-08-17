# job-radar

Multi-source technical job board monitor: polls several boards, scores listings against a
weighted keyword model, dedups across runs, and pushes only the *new* matches to a webhook.

Built as production tooling — it runs nightly and feeds a real pipeline. Published because
it is a compact, honest example of the class of automation work I do: scheduled polling of
heterogeneous sources, resilient fetching against bot-hostile sites, scoring, persistent
state, and notification routing.

```
$ python3 job_radar.py --min-score 3
source_n8n: 30 listings
source_reddit_forhire: 25 listings

26 matches (>= 3), 26 new

   8  n8n-jobs   Hi, I need a Vapi + n8n + Twilio integration built. Paid project
   7  n8n-jobs   Seeking n8n/GHL Automation Developer — Insurance Lead Pipeline {Ongoing}
   6  n8n-jobs   [FOR HIRE] Async n8n workflow build or rescue, fixed $1,000  [$1,000]
```

## What's interesting in here

**A transport ladder, cheapest-first.** Plain HTTP handles most sources in ~200ms. Sites
that refuse it escalate to a real browser. You should not pay browser latency for a page
that a `urlopen` would have served.

**A measured finding about headless browsers.** On one host, in one minute:
`old.reddit.com` returned **HTTP 200** to plain `curl` and **"You've been blocked by
network security"** to headless Chrome. The block was on the *headless fingerprint*, not
the IP. Most scraper stacks reach for headless first, hit that wall, and conclude the site
is unscrapable. The browser lane here runs **headful on a virtual display (Xvfb)** with
`--disable-blink-features=AutomationControlled` — and walks straight through.

**Scoring, not just matching.** Keywords are weighted (`n8n: 3`, `python: 2`, `fix: 1`),
negative terms subtract whole categories, an explicit budget in the title adds signal, and
a "[Hiring]" prefix is worth more than "[For Hire]" — because one of those is a buyer.

**Dedup that survives restarts.** State is a persisted seen-set keyed on URL, capped so it
cannot grow without bound. Re-running does not re-alert.

**Adding a source is one function.** Return `{source, title, url, posted}` and append it to
`SOURCES`. The scoring, dedup, and alerting are source-agnostic.

## Usage

```bash
python3 job_radar.py                    # one pass, print matches
python3 job_radar.py --notify           # ...and push new ones to Telegram
python3 job_radar.py --min-score 5      # tighten the filter
python3 job_radar.py --all              # ignore the seen-list

# nightly
*/30 9-23 * * * /usr/bin/python3 /path/to/job_radar.py --notify
```

State lives at `~/.local/share/job-radar/seen.json`. Notification is Telegram here; swap
`notify()` for Slack, Discord, or an email relay in about four lines.

## Notes

Read-only, public endpoints, polite intervals. No authentication, no scraping behind
logins, no automated submission of anything anywhere.

Requires Python 3.9+ and no third-party packages for the HTTP path. The optional browser
lane expects a Chromium reachable over CDP.

MIT licensed.
