#!/usr/bin/env python3
"""
lmsr_backtest.py — Backtest the LMSR scanner against resolved Polymarket markets
=================================================================================
Fetches resolved markets from Gamma API, reconstructs pre-resolution state,
runs scan_market_lmsr() as if live, and measures prediction accuracy vs outcome.

Usage:
    cd ~/leonardo && python3 lmsr_backtest.py
    python3 lmsr_backtest.py --n 200

Output:
    Printed stats table
    ~/leonardo/lmsr_backtest_results.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from lmsr_scanner import scan_market_lmsr, classify_market

GAMMA_API    = "https://gamma-api.polymarket.com"
RESULTS_FILE = os.path.join(_DIR, "lmsr_backtest_results.json")


# ══════════════════════════════════════════════════════════════════════════════
#  FETCH RESOLVED MARKETS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_resolved_markets(n: int = 500) -> list[dict]:
    """
    Fetch up to n resolved binary markets from Gamma API.
    Filters: closed=true, volume > $1000, has 2 outcomes (binary).
    """
    found   = []
    offset  = 0
    batch   = min(n, 100)

    while len(found) < n:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":    "false",
                    "closed":    "true",
                    "limit":     batch,
                    "offset":    offset,
                    "order":     "volume",
                    "ascending": "false",
                },
                timeout=20,
            )
            resp.raise_for_status()
            data    = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        except Exception as exc:
            print(f"[backtest] Gamma API error (offset={offset}): {exc}")
            break

        if not markets:
            break

        for m in markets:
            # Must be binary (2 outcome prices) with volume > $1000
            prices_raw = m.get("outcomePrices")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
            except Exception:
                prices = []

            if len(prices) != 2:
                continue

            vol = float(m.get("volume") or m.get("volume24hr") or 0)
            if vol < 1_000:
                continue

            # Must have a last trade price for pre-resolution reconstruction
            if not m.get("lastTradePrice"):
                continue

            found.append(m)
            if len(found) >= n:
                break

        offset += batch
        if len(markets) < batch:
            break   # no more pages
        time.sleep(0.3)

    print(f"[backtest] Fetched {len(found)} resolved binary markets (vol > $1k).")
    return found[:n]


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE MARKET BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def backtest_market(market: dict) -> Optional[dict]:
    """
    For a resolved market:
      1. Determine actual outcome (1.0 = YES won, 0.0 = NO won)
      2. Reconstruct pre-resolution market dict using lastTradePrice
      3. Run scan_market_lmsr() as if live
      4. Compute what profit would have been if traded

    Returns None if scanner found no edge (correctly sat out).
    """
    prices_raw = market.get("outcomePrices")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
        yes_resolution = float(prices[0])
    except Exception:
        return None

    # Outcome: YES won if resolved price is 1.0, NO won if 0.0
    if yes_resolution > 0.99:
        outcome = 1.0   # YES won
    elif yes_resolution < 0.01:
        outcome = 0.0   # NO won
    else:
        return None   # Ambiguous resolution — skip

    # Reconstruct pre-resolution state from lastTradePrice
    last_price = market.get("lastTradePrice")
    if not last_price:
        return None
    try:
        pre_yes_price = float(last_price)
    except (TypeError, ValueError):
        return None

    if not (0.01 <= pre_yes_price <= 0.99):
        return None

    # Build synthetic pre-resolution market dict
    synth_spread = 0.04   # typical Polymarket spread
    synth_market = {
        "question":     market.get("question") or market.get("title") or "",
        "bestBid":      str(round(pre_yes_price - synth_spread / 2, 4)),
        "bestAsk":      str(round(pre_yes_price + synth_spread / 2, 4)),
        "outcomePrices": json.dumps([str(pre_yes_price), str(1.0 - pre_yes_price)]),
        "volume24hr":   market.get("volume24hr") or market.get("volume") or 0,
        "volume":       market.get("volume") or 0,
        "spread":       synth_spread,
        "clobTokenIds": market.get("clobTokenIds") or "[]",
        "enableOrderBook":  True,
        "acceptingOrders":  True,
    }

    # Run scanner
    result = scan_market_lmsr(synth_market)
    if result is None:
        return None   # No edge found — correctly sat out

    # Determine profit if trade had been executed
    trade_side  = result["trade_side"]
    avg_fill    = result["expected_avg_fill"]
    n_shares    = result["optimal_size_usd"] / avg_fill if avg_fill > 0 else 0
    cost_usd    = result["optimal_size_usd"]

    if trade_side == "YES":
        # Bought YES at avg_fill; outcome=1.0 means profit, outcome=0.0 means loss
        if outcome == 1.0:
            profit_usd  = n_shares * (1.0 - avg_fill)
            trade_won   = True
        else:
            profit_usd  = -cost_usd
            trade_won   = False
    else:
        # Bought NO — no_price = 1 - yes_price; outcome=0.0 means NO won
        no_price = result["market_price"]   # already flipped in scanner
        n_no_shares = cost_usd / no_price if no_price > 0 else 0
        if outcome == 0.0:
            profit_usd = n_no_shares * (1.0 - no_price)
            trade_won  = True
        else:
            profit_usd = -cost_usd
            trade_won  = False

    our_yes_prob = result["true_prob"] if trade_side == "YES" else (1.0 - result["true_prob"])

    return {
        "question":       (market.get("question") or "")[:80],
        "pre_yes_price":  pre_yes_price,
        "outcome":        outcome,
        "our_true_prob":  our_yes_prob,
        "trade_side":     trade_side,
        "realized_ev":    result["realized_ev"],
        "confidence":     result["confidence"],
        "category":       result["category"],
        "risk_flags":     result["risk_flags"],
        "cost_usd":       round(cost_usd, 4),
        "profit_usd":     round(profit_usd, 4),
        "trade_won":      trade_won,
        "our_err":        abs(our_yes_prob - outcome),
        "market_err":     abs(pre_yes_price - outcome),
        "we_beat_market": abs(our_yes_prob - outcome) < abs(pre_yes_price - outcome),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  FULL BACKTEST RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(n: int = 500) -> dict:
    """
    Run backtest on N resolved markets.
    Prints accuracy stats and saves full results.
    """
    print(f"\n{'═'*65}")
    print(f"  LMSR SCANNER BACKTEST  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═'*65}")

    markets = fetch_resolved_markets(n)
    if not markets:
        print("[backtest] No markets fetched — cannot run backtest.")
        return {}

    print(f"\nRunning analysis on {len(markets)} markets…")

    trades     : list[dict] = []
    sat_out    : int         = 0
    errors     : int         = 0

    for i, m in enumerate(markets, 1):
        if i % 50 == 0:
            print(f"  … {i}/{len(markets)}")
        try:
            result = backtest_market(m)
            if result is None:
                sat_out += 1
            else:
                trades.append(result)
        except Exception:
            errors += 1
        time.sleep(0.05)   # light rate limiting

    if not trades:
        print("\nNo edge signals found across all resolved markets.")
        summary = {
            "markets_scanned": len(markets),
            "edges_found":     0,
            "sat_out":         sat_out,
        }
        with open(RESULTS_FILE, "w") as f:
            json.dump({"summary": summary, "trades": []}, f, indent=2)
        return summary

    n_total    = len(markets)
    n_edge     = len(trades)
    n_won      = sum(1 for t in trades if t["trade_won"])
    n_we_beat  = sum(1 for t in trades if t["we_beat_market"])

    total_profit  = sum(t["profit_usd"] for t in trades)
    total_cost    = sum(t["cost_usd"]   for t in trades)
    roi           = total_profit / total_cost * 100 if total_cost > 0 else 0

    mean_ev_win   = (sum(t["realized_ev"] for t in trades if t["trade_won"])
                     / max(1, n_won))
    mean_ev_loss  = (sum(t["realized_ev"] for t in trades if not t["trade_won"])
                     / max(1, n_edge - n_won))

    # Brier score on YES probability vs actual outcome
    brier = sum((t["our_true_prob"] - t["outcome"]) ** 2 for t in trades) / n_edge

    # By confidence
    by_conf: dict[str, dict] = {}
    for t in trades:
        c = t["confidence"]
        by_conf.setdefault(c, {"win": 0, "total": 0})
        by_conf[c]["total"] += 1
        if t["trade_won"]:
            by_conf[c]["win"] += 1

    # By category
    by_cat: dict[str, dict] = {}
    for t in trades:
        c = t["category"]
        by_cat.setdefault(c, {"win": 0, "total": 0, "profit": 0.0})
        by_cat[c]["total"] += 1
        by_cat[c]["profit"] += t["profit_usd"]
        if t["trade_won"]:
            by_cat[c]["win"] += 1

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n{'─'*65}")
    print(f"  RESULTS")
    print(f"{'─'*65}")
    print(f"  Total markets scanned        : {n_total}")
    print(f"  Markets with edge found      : {n_edge}  ({n_edge/n_total:.1%})")
    print(f"  Sat out (no edge)            : {sat_out}  ({sat_out/n_total:.1%})")
    print(f"  Would have been profitable   : {n_won}  ({n_won/n_edge:.1%} of trades)")
    print(f"  Our prob closer than market  : {n_we_beat}/{n_edge}  ({n_we_beat/n_edge:.1%})")
    print(f"")
    print(f"  Mean realized EV — winning   : {mean_ev_win:+.2%}")
    print(f"  Mean realized EV — losing    : {mean_ev_loss:+.2%}")
    print(f"  Simulated P&L on $100 roll   : ${total_profit:+.2f}  (ROI {roi:+.1f}%)")
    print(f"  Brier score (true_prob)      : {brier:.4f}  (0=perfect, 0.25=random)")
    print(f"")
    print(f"  By confidence level:")
    for conf_level in ["HIGH", "MEDIUM", "LOW"]:
        d = by_conf.get(conf_level)
        if d:
            pct = d["win"] / d["total"] * 100
            print(f"    {conf_level:<8}: {d['win']:>3}W / {d['total']:>3} total  ({pct:.0f}%)")

    print(f"")
    print(f"  By market category:")
    for cat, d in sorted(by_cat.items(), key=lambda x: -x[1]["total"]):
        pct = d["win"] / d["total"] * 100
        print(f"    {cat:<12}: {d['win']:>3}W / {d['total']:>3}  ({pct:.0f}%)  P&L ${d['profit']:+.2f}")

    print(f"{'═'*65}\n")

    # ── Save results ──────────────────────────────────────────────────────────
    summary = {
        "run_at":              datetime.now(timezone.utc).isoformat(),
        "markets_scanned":     n_total,
        "edges_found":         n_edge,
        "sat_out":             sat_out,
        "profitable_trades":   n_won,
        "win_rate":            round(n_won / n_edge, 4),
        "we_beat_market_rate": round(n_we_beat / n_edge, 4),
        "mean_ev_winning":     round(mean_ev_win,  4),
        "mean_ev_losing":      round(mean_ev_loss, 4),
        "total_profit_usd":    round(total_profit, 4),
        "total_cost_usd":      round(total_cost,   4),
        "roi_pct":             round(roi,           4),
        "brier_score":         round(brier,         4),
        "by_confidence":       {k: {"win": v["win"], "total": v["total"],
                                    "win_rate": round(v["win"]/v["total"], 4)}
                                 for k, v in by_conf.items()},
        "by_category":         {k: {"win": v["win"], "total": v["total"],
                                    "profit": round(v["profit"], 4),
                                    "win_rate": round(v["win"]/v["total"], 4)}
                                 for k, v in by_cat.items()},
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump({"summary": summary, "trades": trades}, f, indent=2)
    print(f"Full results saved to: {RESULTS_FILE}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LMSR backtest on resolved Polymarket markets")
    parser.add_argument("--n", type=int, default=500, help="Number of resolved markets to test")
    args = parser.parse_args()
    run_backtest(n=args.n)
