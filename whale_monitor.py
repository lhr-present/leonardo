#!/usr/bin/env python3
"""
whale_monitor.py — Background whale position monitor for Polymarket
====================================================================
Runs every 15 minutes, detects new large positions (> $500 USD),
logs them, and posts alerts via Moltbook.

Usage:
    python3 whale_monitor.py              # run once
    python3 whale_monitor.py --loop       # run every 15 minutes
    nohup python3 whale_monitor.py --loop > ~/leonardo/whale_monitor.log 2>&1 &
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from whale_tracker import get_new_whale_positions, WHALE_THRESHOLD_USD

# ── Optional Moltbook integration ────────────────────────────────────────────
try:
    from moltbook import post as _moltbook_post
    _MOLTBOOK = True
except ImportError:
    _MOLTBOOK = False

# ── Optional lmsr_scanner for context ────────────────────────────────────────
try:
    from lmsr_scanner import get_whale_signal as _scan_context
    _SCANNER = True
except ImportError:
    _SCANNER = False

LOG_FILE    = os.path.join(_DIR, "whale_alerts.log")
STATE_FILE  = os.path.join(_DIR, "whale_state.json")
INTERVAL    = 900   # 15 minutes


# ══════════════════════════════════════════════════════════════════════════════
#  ALERT FORMATTING
# ══════════════════════════════════════════════════════════════════════════════

def _format_alert(pos: dict) -> str:
    side_emoji = "🟢" if pos["side"] == "YES" else "🔴"
    prev       = pos["prev_size_usd"]
    delta_str  = f"  (+${pos['size_usd'] - prev:,.0f} new)" if prev > 0 else ""
    return (
        f"{side_emoji} WHALE {pos['side']} ${pos['size_usd']:,.0f}{delta_str}\n"
        f"   {pos['question'][:80]}\n"
        f"   Wallet: {pos['wallet']}  |  {pos['detected_at'][:19]} UTC"
    )


def _log(msg: str) -> None:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _post_to_moltbook(text: str) -> None:
    if not _MOLTBOOK:
        return
    try:
        _moltbook_post(text, submolt="whales")
    except Exception as exc:
        _log(f"[whale_monitor] Moltbook post failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE SCAN PASS
# ══════════════════════════════════════════════════════════════════════════════

def run_once(min_size: float = WHALE_THRESHOLD_USD) -> list[dict]:
    """
    One scan pass. Returns list of new whale positions found.
    """
    _log(f"[whale_monitor] Scanning positions ≥ ${min_size:,.0f}…")

    try:
        new_positions = get_new_whale_positions(size_threshold=min_size)
    except Exception as exc:
        _log(f"[whale_monitor] ERROR fetching positions: {exc}")
        return []

    if not new_positions:
        _log("[whale_monitor] No new whale positions detected.")
        return []

    _log(f"[whale_monitor] {len(new_positions)} new whale position(s) detected.")

    for pos in new_positions:
        alert_text = _format_alert(pos)
        _log(alert_text)

        # Optional: get LMSR scanner context for this market
        if _SCANNER:
            try:
                whale_prob = _scan_context(pos["question"])
                if whale_prob is not None:
                    context = f"   [Whale signal: {whale_prob:.1%} YES implied]"
                    _log(context)
                    alert_text += f"\n{context}"
            except Exception:
                pass

        _post_to_moltbook(alert_text)

    # Save alerts to JSON log
    alerts_file = os.path.join(_DIR, "whale_alerts.json")
    try:
        existing: list = []
        if os.path.exists(alerts_file):
            with open(alerts_file) as f:
                existing = json.load(f)
        existing.extend(new_positions)
        # Keep last 1000 entries
        existing = existing[-1_000:]
        with open(alerts_file, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as exc:
        _log(f"[whale_monitor] Failed to save alerts JSON: {exc}")

    return new_positions


# ══════════════════════════════════════════════════════════════════════════════
#  LOOP MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_loop(interval: int = INTERVAL, min_size: float = WHALE_THRESHOLD_USD) -> None:
    """Run scan every `interval` seconds indefinitely."""
    _log(f"[whale_monitor] Starting loop (interval={interval}s, min=${min_size:,.0f})")
    while True:
        try:
            run_once(min_size=min_size)
        except Exception as exc:
            _log(f"[whale_monitor] Unhandled error in run_once: {exc}")
        _log(f"[whale_monitor] Sleeping {interval}s…")
        time.sleep(interval)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket whale position monitor")
    parser.add_argument("--loop",     action="store_true", help="Run every 15 minutes")
    parser.add_argument("--interval", type=int,   default=INTERVAL,
                        help=f"Loop interval in seconds (default: {INTERVAL})")
    parser.add_argument("--min-size", type=float, default=WHALE_THRESHOLD_USD,
                        help=f"Minimum position size USD (default: {WHALE_THRESHOLD_USD})")
    args = parser.parse_args()

    if args.loop:
        run_loop(interval=args.interval, min_size=args.min_size)
    else:
        positions = run_once(min_size=args.min_size)
        print(f"\nTotal new positions found: {len(positions)}")
