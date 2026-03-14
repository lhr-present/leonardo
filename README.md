# Leonardo

Autonomous prediction market edge-finding system. Scans Polymarket, sports betting markets, and weather markets 24/7 for mispricings, logs every pick with a verifiable timestamp before kickoff/resolution, and builds a public track record.

**Live record**: [moltbook.com/u/edgefinderbot2](https://moltbook.com/u/edgefinderbot2)

---

## How it works

Leonardo runs three edge-detection pipelines in parallel:

### 1. LMSR mispricing (`lmsr_scanner.py`)
Uses the [Logarithmic Market Scoring Rule](https://en.wikipedia.org/wiki/Scoring_rule#Logarithmic_scoring_rule) to detect when Polymarket prices diverge from multi-source probability estimates. Cross-references:
- **Kalshi** — different trader pool, genuine divergence signal
- **Manifold Markets** — community forecasts
- **Metaculus** — aggregated expert forecasts
- **Volume momentum** — detects unusual 24h price drift
- **Orderbook depth** — LMSR cost-of-trade to size positions correctly

Only trades when `edge > price_impact_cost + 5%`.

### 2. Weather arbitrage (`weather_edge.py`)
Compares multi-source meteorological consensus against Polymarket temperature/precipitation markets. Sources:
- Open-Meteo (free, no key)
- Met.no (free, no key)
- Tomorrow.io (optional, free tier)
- NOAA (US markets)

Only acts on markets resolving within 48 hours with MEDIUM+ confidence (2+ forecast models agree within 10%).

### 3. Sports edge (`analysis.py`, `data.py`)
Poisson-based BTTS and Over/Under probability model using API-Football season stats. Kelly criterion sizing (25% fractional).

---

## Architecture

```
scheduler.py          — master job runner
├── job_daily_picks   — 08:00 UTC: fetch fixtures, analyse, log picks
├── job_settle_picks  — 23:00 UTC: auto-settle via API-Football results
├── job_weekly_digest — Monday 09:00 UTC: weekly performance post
├── job_polymarket_scan — every 5 min: LMSR scan (paper only)
├── post_daily_scan_summary — 09:00 UTC: Moltbook daily post
├── engage_community  — every 6h: comment on relevant Moltbook posts
├── weekly_x_tweet    — Sunday 11:00 UTC: X performance summary
└── flush_outbox      — every 30 min: retry failed X posts

weather_edge.py --monitor   — 30-min weather scan loop
polymarket_monitor.py       — continuous Polymarket monitoring
whale_monitor.py --loop     — 15-min whale position detection
```

---

## Track record integrity

Every pick is posted to [Moltbook](https://moltbook.com) **before kickoff or market resolution**. The Moltbook timestamp is the proof — it cannot be backdated.

Losing picks are posted with the same prominence as winning picks. The goal is a verified track record, not a highlight reel. `tracker.py` stores every pick with:
- Our model probability
- Market implied probability
- Edge percentage
- Kelly-sized paper stake
- Result and P&L (after settlement)

---

## Setup

```bash
git clone https://github.com/lhr-present/leonardo
cd leonardo
pip install -r requirements.txt   # see dependencies below
cp .env.example .env              # fill in API keys
python3 scheduler.py              # run the master scheduler
```

### Required API keys (`.env`)

| Key | Where to get it | Required |
|-----|----------------|----------|
| `API_FOOTBALL_KEY` | [api-football.com](https://www.api-football.com/) — free tier | Yes (sports) |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` | [developer.twitter.com](https://developer.twitter.com/en/portal) | No |
| `TOMORROW_API_KEY` | [app.tomorrow.io](https://app.tomorrow.io/signup) — free tier | No |
| `POLYMARKET_PRIVATE_KEY` | Polymarket account | No (paper mode works without) |

Moltbook credentials: `~/.config/moltbook/credentials.json`

---

## Key files

| File | Purpose |
|------|---------|
| `lmsr_engine.py` | LMSR math: cost_of_trade, infer_q_from_price, kelly sizing |
| `lmsr_scanner.py` | Polymarket market scan with multi-source signal aggregation |
| `weather_edge.py` | Weather market edge finder and monitor |
| `whale_tracker.py` | Large Polymarket position detection |
| `analysis.py` | Sports probability models (Poisson, BTTS, Over/Under) |
| `data.py` | API-Football: fixtures, team stats, final scores |
| `tracker.py` | Prediction store: log, settle, stats, export |
| `moltbook_bot.py` | Moltbook API: post picks, results, weekly digest |
| `moltbook_presence.py` | Bio, intro, daily scan summary, milestone posts |
| `x_poster.py` | X (Twitter) posting with outbox retry queue |
| `scheduler.py` | Master job scheduler |
| `sync_predictions.py` | Merge prediction stores, create symlink |

---

## Paper trades only

All positions are paper trades. The system is building a verifiable track record before any real capital is deployed. See `MONETIZATION_GUIDE.md` for the planned progression.

---

## Dependencies

```
requests
python-dotenv
schedule
tweepy
```
