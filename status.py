#!/usr/bin/env python3
"""
status.py — Leonardo live dashboard
=====================================
Prints a quick status overview: bankroll, stats, pending picks,
upcoming fixtures, and last scheduler run times.

Usage:
    python status.py
"""

import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PRED_FILE   = os.path.join(os.path.dirname(__file__), "predictions.json")
STATE_FILE  = os.path.join(os.path.dirname(__file__), "scheduler_state.json")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "leonardo.log")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main():
    predictions = _load_json(PRED_FILE, [])
    state       = _load_json(STATE_FILE, {})

    settled  = [p for p in predictions if p.get("settled")]
    pending  = [p for p in predictions if not p.get("settled")]
    wins     = [p for p in settled if p.get("result") == "WIN"]
    n        = len(settled)

    bankroll     = 100.0 + sum(p.get("profit_loss", 0) for p in settled)
    win_rate     = (len(wins) / n * 100) if n else 0.0
    total_staked = sum(p.get("paper_stake_usd", 0) for p in settled)
    total_pl     = sum(p.get("profit_loss", 0) for p in settled)
    roi          = (total_pl / total_staked * 100) if total_staked else 0.0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'═'*58}")
    print(f"  LEONARDO STATUS  —  {now}")
    print(f"{'═'*58}")
    print(f"  Bankroll          : ${bankroll:.2f}  (started $100.00)")
    print(f"  Total P&L         : ${total_pl:+.2f}")
    print(f"  ROI               : {roi:+.2f}%")
    print(f"  Win rate          : {win_rate:.1f}%  ({len(wins)}W / {n - len(wins)}L / {n} settled)")
    print(f"  Total predictions : {len(predictions)}  ({len(pending)} pending)")

    # ── Pending picks ─────────────────────────────────────────────────────────
    if pending:
        print(f"\n  Pending picks ({len(pending)}):")
        for p in pending:
            kickoff = p.get("kickoff_utc", "?")[:16].replace("T", " ")
            print(
                f"    #{p['id']:>3}  {p['match']:<30}  {p['market']} {p['selection']}"
                f"  @ {p['market_odds']}  ({kickoff})"
            )
        print(f"\n  Settle: python tracker.py settle <ID> WIN|LOSS")
    else:
        print(f"\n  No pending picks.")

    # ── Scheduler state ────────────────────────────────────────────────────────
    print(f"\n  Scheduler last runs:")
    print(f"    Daily picks    : {state.get('last_daily', 'never')}")
    print(f"    Weekly digest  : {state.get('last_weekly', 'never')}")
    print(f"    Polymarket     : {state.get('last_polymarket', 'never')}")
    print(f"    Scan cycles    : {state.get('cycles', 0)}")

    # ── Polymarket monitor ────────────────────────────────────────────────────
    poly_data   = os.path.join(os.path.dirname(__file__), "polymarket_24h.json")
    poly_report = os.path.join(os.path.dirname(__file__), "polymarket_report.md")
    if os.path.exists(poly_report):
        print(f"\n  POLYMARKET REPORT  : ready — cat ~/leonardo/polymarket_report.md")
    elif os.path.exists(poly_data):
        try:
            with open(poly_data) as _f:
                _cycles = json.load(_f)
            _n     = len(_cycles) if isinstance(_cycles, list) else 0
            _opps  = sum(len(c.get("opportunities", [])) for c in _cycles) if isinstance(_cycles, list) else 0
            print(f"\n  POLYMARKET MONITOR : cycle {_n}/288 | {_opps} opps found so far")
        except Exception:
            print(f"\n  POLYMARKET MONITOR : data file unreadable")

    # ── Last 5 log lines ──────────────────────────────────────────────────────
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                lines = f.readlines()
            if lines:
                print(f"\n  Last log entries:")
                for line in lines[-5:]:
                    print(f"    {line.rstrip()}")
        except Exception:
            pass

    print(f"\n{'═'*58}\n")


if __name__ == "__main__":
    main()
