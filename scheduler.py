#!/usr/bin/env python3
"""
scheduler.py — Leonardo master scheduler
==========================================
Runs four recurring jobs:

  1. Daily 08:00 UTC  — Fetch fixtures + analyse → log picks + post to Moltbook
  2. Daily 23:00 UTC  — Settle yesterday's picks (manual helper prompt)
  3. Monday 09:00 UTC — Post weekly digest to Moltbook
  4. Every 5 minutes  — Polymarket market scan (paper only)

Usage:
    python scheduler.py          # run forever
    python scheduler.py --once   # run all jobs once then exit (dry-run / test)
"""

import sys
import os
import json
import time
import logging
from datetime import datetime

import schedule

sys.path.insert(0, os.path.dirname(__file__))

try:
    from moltbook_presence import (
        post_daily_scan_summary, engage_community,
        post_introduction, update_profile_bio,
    )
    from x_poster import post_to_x, format_weekly_tweet
    _PRESENCE_AVAILABLE = True
    _X_AVAILABLE        = True
except ImportError:
    _PRESENCE_AVAILABLE = False
    _X_AVAILABLE        = False

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "leonardo.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("leonardo")

STATE_FILE = os.path.join(os.path.dirname(__file__), "scheduler_state.json")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_daily": None, "last_weekly": None, "last_polymarket": None, "cycles": 0}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.warning(f"Could not save state: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  JOB 1 — Daily picks
# ══════════════════════════════════════════════════════════════════════════════

def job_daily_picks() -> None:
    log.info("═" * 60)
    log.info("JOB 1 — Daily picks analysis")

    try:
        from data     import get_todays_fixtures
        from analysis import analyse_all
        from tracker  import log_prediction, load_predictions, current_bankroll
        from moltbook_bot import post_daily_picks
    except ImportError as exc:
        log.error(f"Import failed: {exc}")
        return

    fixtures = get_todays_fixtures()
    log.info(f"  Fixtures today: {len(fixtures)}")

    if not fixtures:
        log.info("  No fixtures — skipping analysis.")
        return

    predictions = load_predictions()
    bankroll    = current_bankroll(predictions)
    picks       = analyse_all(fixtures, bankroll)

    log.info(f"  Picks with positive edge: {len(picks)}")
    if not picks:
        log.info("  No picks today.")
        return

    saved = []
    for pick in picks:
        record = log_prediction(pick)
        log.info(
            f"  Saved #{record['id']}: {record['match']} | "
            f"{record['market']} {record['selection']} | "
            f"edge={record['edge_percent']:+.2f}%"
        )
        saved.append(record)

    # Post batch to Moltbook
    url = post_daily_picks(saved)
    if url:
        log.info(f"  Posted to Moltbook: {url}")

    state = _load_state()
    state["last_daily"] = datetime.utcnow().isoformat() + "Z"
    _save_state(state)
    log.info("JOB 1 done.")


# ══════════════════════════════════════════════════════════════════════════════
#  JOB 2 — Settle yesterday's picks
# ══════════════════════════════════════════════════════════════════════════════

def job_settle_picks() -> None:
    """
    Auto-settle unsettled picks via API-Football where possible.
    Falls back to Moltbook warning post for picks that can't be found.
    """
    log.info("═" * 60)
    log.info("JOB 2 — Auto-settle picks")

    try:
        from tracker import get_unsettled, settle_prediction
        from moltbook_bot import post_result, _api_headers, MOLTBOOK_API, ensure_submolt
        from data import fetch_final_score, search_fixture_by_teams
    except ImportError as exc:
        log.error(f"Import failed: {exc}")
        return

    import requests as _req

    unsettled = get_unsettled()
    if not unsettled:
        log.info("  No unsettled picks.")
        return

    log.info(f"  {len(unsettled)} unsettled picks — attempting auto-settle…")

    for p in unsettled:
        score = None

        # Case A: has fixture_id
        if p.get("fixture_id"):
            try:
                score = fetch_final_score(int(p["fixture_id"]))
            except Exception as exc:
                log.warning(f"  fetch_final_score({p['fixture_id']}) failed: {exc}")

        # Case B: no fixture_id — search by team names + date
        if not score and " vs " in p.get("match", ""):
            home, away = p["match"].split(" vs ", 1)
            date_str = (p.get("kickoff_utc") or p.get("timestamp", ""))[:10]
            if date_str:
                try:
                    score = search_fixture_by_teams(home.strip(), away.strip(), date_str)
                except Exception as exc:
                    log.warning(f"  search_fixture({p['match']}) failed: {exc}")

        if not score:
            log.info(f"  #{p['id']}: {p['match']} — not FT yet or not found")
            # Post Moltbook warning for picks that are > 6 hours past kickoff
            kickoff = p.get("kickoff_utc") or p.get("timestamp", "")
            try:
                from datetime import datetime, timezone, timedelta
                ko_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - ko_dt).total_seconds() / 3600
                if hours_since > 6:
                    headers = _api_headers()
                    ensure_submolt("sports")
                    _req.post(
                        f"{MOLTBOOK_API}/posts",
                        headers=headers,
                        json={
                            "submolt_name": "sports",
                            "submolt":      "sports",
                            "title":        f"⚠️ Manual settle needed: {p['match']}",
                            "content":      (
                                f"Pick #{p['id']}: **{p['match']}** "
                                f"({p['market']} {p['selection']}) needs manual settlement.\n\n"
                                f"Run: `python tracker.py settle {p['id']} WIN|LOSS`"
                            ),
                        },
                        timeout=10,
                    )
            except Exception:
                pass
            continue

        hg, ag = score["home_goals"], score["away_goals"]
        mkt = p.get("market", "").upper()
        sel = p.get("selection", "").upper()

        if mkt in ("BTTS", "BOTH TEAMS TO SCORE"):
            result = "WIN" if (hg and ag and int(hg) > 0 and int(ag) > 0) else "LOSS"
        elif "OVER" in mkt and "2.5" in mkt:
            result = "WIN" if (hg is not None and ag is not None and int(hg) + int(ag) > 2) else "LOSS"
        elif mkt in ("1X2", "MATCH WINNER"):
            hg, ag = int(hg or 0), int(ag or 0)
            if sel in ("HOME", "1"):
                result = "WIN" if hg > ag else "LOSS"
            elif sel in ("AWAY", "2"):
                result = "WIN" if ag > hg else "LOSS"
            else:
                result = "WIN" if hg == ag else "LOSS"
        else:
            log.info(f"  #{p['id']}: unknown market '{mkt}' — manual settle needed")
            continue

        try:
            rec = settle_prediction(p["id"], result)
            log.info(
                f"  #{p['id']}: {p['match']} {score['home_goals']}-{score['away_goals']} "
                f"→ {result}  P&L: ${rec['profit_loss']:+.2f}"
            )
            post_result(rec)
        except Exception as exc:
            log.error(f"  settle #{p['id']} failed: {exc}")

    log.info("JOB 2 done.")


# ══════════════════════════════════════════════════════════════════════════════
#  JOB 3 — Weekly digest
# ══════════════════════════════════════════════════════════════════════════════

def job_weekly_digest() -> None:
    log.info("═" * 60)
    log.info("JOB 3 — Weekly digest")

    try:
        from moltbook_bot import post_weekly_digest
    except ImportError as exc:
        log.error(f"Import failed: {exc}")
        return

    url = post_weekly_digest()
    if url:
        log.info(f"  Weekly digest posted: {url}")

    state = _load_state()
    state["last_weekly"] = datetime.utcnow().isoformat() + "Z"
    _save_state(state)
    log.info("JOB 3 done.")


# ══════════════════════════════════════════════════════════════════════════════
#  JOB 4 — Polymarket scan
# ══════════════════════════════════════════════════════════════════════════════

def job_polymarket_scan() -> None:
    log.info("── Polymarket scan ──────────────────────────────────")

    try:
        from polymarket import scan_and_report
    except ImportError as exc:
        log.error(f"Import failed: {exc}")
        return

    opps = scan_and_report()
    if opps:
        log.info(f"  {len(opps)} opportunities found (paper only):")
        for o in opps[:5]:
            log.info(
                f"    [{o['signal_source']}] edge={o['edge']:.1%} {o['trade_side']} "
                f"${o['cost_usd']:.2f} — {o['question'][:55]}"
            )
    else:
        log.info("  No opportunities above threshold.")

    state = _load_state()
    state["last_polymarket"] = datetime.utcnow().isoformat() + "Z"
    state["cycles"]          = state.get("cycles", 0) + 1
    _save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    once_mode = "--once" in sys.argv

    log.info("═" * 60)
    log.info("  Leonardo — Starting up")
    log.info(f"  Mode: {'--once (dry run)' if once_mode else 'continuous'}")
    log.info("═" * 60)

    if once_mode:
        log.info("Running all jobs once…")
        job_daily_picks()
        job_settle_picks()
        job_weekly_digest()
        job_polymarket_scan()
        log.info("Done.")
        return

    # ── Schedule jobs ────────────────────────────────────────────────────────
    schedule.every().day.at("08:00").do(job_daily_picks)
    schedule.every().day.at("23:00").do(job_settle_picks)
    schedule.every().monday.at("09:00").do(job_weekly_digest)
    schedule.every(5).minutes.do(job_polymarket_scan)

    log.info("Scheduled:")
    log.info("  08:00 UTC daily    — fetch fixtures + log picks")
    log.info("  23:00 UTC daily    — settle picks reminder")
    log.info("  09:00 UTC Monday   — weekly digest to Moltbook")
    log.info("  every 5 minutes    — Polymarket scan (paper)")

    # ── Presence & X jobs (appended) ─────────────────────────────────────────

    def _safe_job(name: str, fn) -> None:
        """Run fn(), catching all exceptions so the scheduler never crashes."""
        try:
            fn()
        except Exception as exc:
            log.warning(f"Job '{name}' failed: {exc}")

    # Run intro + bio update once on startup (idempotent)
    if _PRESENCE_AVAILABLE:
        try:
            update_profile_bio()
            post_introduction()
        except Exception as e:
            log.warning(f"Presence setup failed: {e}")

    # Daily scan summary at 09:00 UTC
    if _PRESENCE_AVAILABLE:
        schedule.every().day.at("09:00").do(
            lambda: _safe_job("daily_scan", post_daily_scan_summary)
        )

    # Community engagement every 6 hours
    if _PRESENCE_AVAILABLE:
        schedule.every(6).hours.do(
            lambda: _safe_job("engage", engage_community)
        )

    # Weekly X tweet — Sunday 11:00 UTC
    if _PRESENCE_AVAILABLE and _X_AVAILABLE:
        def _weekly_x_tweet():
            from tracker import compute_stats, load_predictions
            stats = compute_stats(load_predictions())
            post_to_x(format_weekly_tweet(stats))
        schedule.every().sunday.at("11:00").do(
            lambda: _safe_job("weekly_x", _weekly_x_tweet)
        )

    # X outbox flush every 30 minutes
    if _X_AVAILABLE:
        from x_poster import flush_outbox
        schedule.every(30).minutes.do(
            lambda: _safe_job("x_flush", flush_outbox)
        )

    if _PRESENCE_AVAILABLE:
        log.info("  09:00 UTC daily    — Moltbook daily scan summary")
        log.info("  every 6 hours      — Moltbook community engagement")
    if _PRESENCE_AVAILABLE and _X_AVAILABLE:
        log.info("  11:00 UTC Sunday   — weekly X tweet")
    if _X_AVAILABLE:
        log.info("  every 30 minutes   — X outbox flush")

    log.info("Running… (Ctrl+C to stop)")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Leonardo stopped.")
        sys.exit(0)
