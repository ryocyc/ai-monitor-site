# AI Signal Feed (aisignalfeed.com)

Monitors the AI landscape for **everyday AI users**: new models, capability
rankings, price changes, outages, and tools worth trying. Zero-server
architecture: local patrol builds a JSON feed → GitHub Pages serves it.

> Reborn 2026-11. The original pipeline (2026-04) died from an un-versioned
> "ghost publisher" layer plus whole-page-hash noise; post-mortem in
> `PHASE0_REPORT.md` (workspace file, not in repo).

## Architecture

```
ai_monitor/
├── run_patrol.py            orchestrator: single-instance lock, timeouts,
│                            progress log (logs/patrol.log), failure alert
│                            (logs/ALERT.txt), CREATE_NO_WINDOW everywhere
├── build_feed.py            merge pillar outputs -> site/data/feed.json
│                            (cap 60) + site/data/archive.json (memory, 2000)
├── discover_trending.py     [pillar 5] GitHub Search API: exploding AI repos
│                            (velocity tiers, star-farm spam filters)
├── discover_hn.py           [pillar 5] Hacker News early signal (Algolia API)
├── collect/
│   ├── collect_status.py    [pillar 4] provider status APIs, indicator diff
│   │                        -> outage + recovery events, zero noise
│   ├── collect_prices.py    [pillar 3] LiteLLM price-registry JSON diff
│   └── collect_feeds.py     [pillar 1] 14 GitHub release atoms + 10 company
│                            RSS/Atom feeds (config: feeds.json)
├── probe_rss.py             one-time source probe (RSS discovery + ban-risk)
├── test_scrapling_blocked.py  one-time Scrapling ladder test (Fetcher ->
│                            StealthyFetcher), evidence for engine choice
├── docs/source_strategy.md  the 5-pillar source strategy + verified endpoints
├── site/                    what GitHub Pages serves (index.html renders
│                            ./data/feed.json client-side; AdSense included)
└── state/                   patrol state: velocity snapshot, seen-ids,
                             status indicators (audit-friendly, in git)
```

Rankings (pillar 2, arena.ai) is planned but deferred: the leaderboard is
JS-rendered; needs a StealthyFetcher-based diff (D4).

## Run

```cmd
python ai_monitor\run_patrol.py
```

One pass ≈ 2 minutes: discovery + collection + feed build. Scheduled every
2 hours via schtasks (see workspace AGENTS.md for the approval record).
Site deploys on push to `main` (GitHub Actions → `ai_monitor/site/`).

## Design rules (hard-earned)

1. All production code lives in git — no local ghost layers, ever.
2. Structured data first: JSON/API/RSS over HTML; HTML via the Scrapling
   ladder (plain Fetcher first, stealth browser only as automatic fallback).
3. Browsers auto-open-and-close per call; process-count verified leak-free.
4. Diff signals, not pages: indicator fields, price snapshots, entry ids.
5. Honest failure: any failed step writes logs/ALERT.txt; nothing fails
   silently.
