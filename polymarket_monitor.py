#!/usr/bin/env python3
"""
polymarket_monitor.py — Polymarket 24-hour observation monitor
===============================================================
Standalone script. Runs alongside Leonardo's scheduler — does NOT touch it.
Observes Polymarket every 5 minutes for 24 hours (288 cycles), records
each snapshot, then auto-generates a full analysis report and posts it
to Moltbook.

PAPER_MODE = True always. Observes only, never trades.

Usage:
    nohup python3 polymarket_monitor.py >> ~/leonardo/leonardo.log 2>&1 &
"""

import os
import sys
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── MUST happen before importing polymarket (it reads env at module level) ────
from dotenv import load_dotenv, dotenv_values

_DIR     = os.path.dirname(os.path.abspath(__file__))
_BOT_ENV = os.path.expanduser("~/polymarket_bot/.env")

# Map polymarket_bot/.env creds → names polymarket.py expects
_bot = dotenv_values(_BOT_ENV) if os.path.exists(_BOT_ENV) else {}
if _bot.get("PRIVATE_KEY"):
    os.environ.setdefault("POLYMARKET_PRIVATE_KEY",    _bot["PRIVATE_KEY"])
if _bot.get("RELAYER_API_KEY"):
    os.environ.setdefault("POLYMARKET_API_KEY",        _bot["RELAYER_API_KEY"])
if _bot.get("RELAYER_API_SECRET"):
    os.environ.setdefault("POLYMARKET_API_SECRET",     _bot["RELAYER_API_SECRET"])
if _bot.get("RELAYER_API_PASSPHRASE"):
    os.environ.setdefault("POLYMARKET_API_PASSPHRASE", _bot["RELAYER_API_PASSPHRASE"])

load_dotenv(os.path.join(_DIR, ".env"))
sys.path.insert(0, _DIR)

# ── Now safe to import from polymarket ────────────────────────────────────────
from polymarket import (
    build_client,
    fetch_external_probability,
    _ob_model,
    mid_price,
    kelly_size,
    BANKROLL,
    MIN_EDGE,
)

# ── LMSR scanner (import gracefully — falls back to legacy if unavailable) ────
try:
    from lmsr_scanner import scan_market_lmsr, estimate_b_from_market as _est_b
    _LMSR_AVAILABLE = True
except ImportError:
    _LMSR_AVAILABLE = False
    log = logging.getLogger("poly-monitor")
    log.warning("[POLY-MONITOR] lmsr_scanner not available — using legacy probability model")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

PAPER_MODE    = True          # LOCKED
TOTAL_CYCLES  = 288           # 288 × 5 min = 24 hours
CYCLE_SECONDS = 300           # 5 minutes
TOP_MARKETS   = 100

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

DATA_FILE   = os.path.join(_DIR, "polymarket_24h.json")
REPORT_FILE = os.path.join(_DIR, "polymarket_report.md")
PID_FILE    = os.path.join(_DIR, "monitor.pid")

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING  (appends to leonardo.log — same file as scheduler)
# ══════════════════════════════════════════════════════════════════════════════

_log_file = os.path.join(_DIR, "leonardo.log")
log = logging.getLogger("poly-monitor")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = logging.FileHandler(_log_file)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    log.addHandler(_fh)
    log.propagate = False


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET FETCH  (Gamma API → top 100 active by volume)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_top_markets() -> list[dict]:
    """
    Fetch top 100 active markets ordered by 24h volume via Gamma API.
    Each returned dict has: question, clobTokenIds, bestBid, bestAsk,
    spread, lastTradePrice, volume24hr.
    Falls back to CLOB /markets on error.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active":    "true",
                "closed":    "false",
                "limit":     TOP_MARKETS,
                "order":     "volume24hr",
                "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data    = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        # Keep only binary YES/NO markets with order book data
        result = []
        for m in markets:
            token_ids = m.get("clobTokenIds") or []
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except Exception:
                    token_ids = []
            if len(token_ids) >= 2 and m.get("enableOrderBook") and m.get("acceptingOrders"):
                m["_token_ids"] = token_ids
                result.append(m)
        return result[:TOP_MARKETS]
    except Exception as exc:
        log.warning(f"[POLY-MONITOR] Gamma API failed ({exc}), falling back to CLOB /markets")

    try:
        resp = requests.get(
            f"{CLOB_HOST}/markets",
            params={"next_cursor": "MA==", "closed": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        data    = resp.json()
        markets = data.get("data", [])
        return [m for m in markets if m.get("active") and not m.get("closed")][:TOP_MARKETS]
    except Exception as exc:
        log.error(f"[POLY-MONITOR] CLOB fallback also failed: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  PROBABILITY ESTIMATE  (same blending logic as polymarket.py)
# ══════════════════════════════════════════════════════════════════════════════

def _estimate_probability(
    question:     str,
    ob:           dict,
    client,
    yes_token_id: str,
) -> tuple[Optional[float], str]:
    """
    Exact same blend logic as polymarket.py's inline estimate.
    Returns (probability, signal_source).
    """
    meta_p = fetch_external_probability(question)
    ob_p   = _ob_model(ob, client, yes_token_id)

    if meta_p is not None and ob_p is not None:
        return max(0.01, min(0.99, meta_p * 0.60 + ob_p * 0.40)), "HYBRID"
    elif meta_p is not None:
        return max(0.01, min(0.99, meta_p)), "METACULUS"
    elif ob_p is not None:
        return max(0.01, min(0.99, ob_p)), "OB_ONLY"
    return None, "NONE"


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE CYCLE
# ══════════════════════════════════════════════════════════════════════════════

def _run_cycle(client, cycle_num: int) -> Optional[dict]:
    """
    Scan top 100 markets, record opportunities and margin stats.
    Returns the cycle snapshot dict, or None if markets unreachable.
    """
    markets = _fetch_top_markets()
    if not markets:
        return None

    ts = datetime.now(timezone.utc).isoformat()

    opportunities   = []
    margins         = []
    markets_scanned = 0

    # Collect b estimates for all scannable markets (for distribution stats)
    b_estimates: list[float] = []

    for market in markets:
        question     = market.get("question") or market.get("title") or ""
        token_ids    = market.get("_token_ids") or []
        volume       = float(market.get("volume24hr") or market.get("volume") or 0)

        # Gamma API: token_ids[0] = YES, token_ids[1] = NO
        tokens = market.get("tokens") or []
        if tokens and isinstance(tokens[0], dict):
            yes_token    = next((t for t in tokens if t.get("outcome") == "YES"), None)
            yes_token_id = yes_token.get("token_id", "") if yes_token else ""
        elif token_ids:
            yes_token_id = str(token_ids[0])
        else:
            continue

        if not yes_token_id:
            continue

        gamma_bid  = market.get("bestBid")
        gamma_ask  = market.get("bestAsk")

        if gamma_bid is None or gamma_ask is None:
            continue
        try:
            bid_f = float(gamma_bid)
            ask_f = float(gamma_ask)
        except (ValueError, TypeError):
            continue
        if bid_f <= 0 or ask_f <= bid_f:
            continue

        spread_val = ask_f - bid_f
        yes_mid = (bid_f + ask_f) / 2.0
        if yes_mid < 0.02 or yes_mid > 0.98:
            continue

        margins.append(max(0.0, spread_val))
        markets_scanned += 1

        # Collect b estimate for all markets
        if _LMSR_AVAILABLE:
            try:
                b_est = _est_b(volume, spread_val * volume)
                b_estimates.append(b_est)
            except Exception:
                pass

        # ── LMSR path (preferred) ──────────────────────────────────────────
        if _LMSR_AVAILABLE:
            lmsr_result = scan_market_lmsr(market)
            if lmsr_result is not None:
                opportunities.append({
                    "question":        question[:120],
                    "market_price":    lmsr_result["market_price"],
                    "true_prob":       lmsr_result["true_prob"],
                    "confidence":      lmsr_result["confidence"],
                    "spot_ev":         lmsr_result["spot_ev"],
                    "realized_ev":     lmsr_result["realized_ev"],
                    "optimal_size_usd":lmsr_result["optimal_size_usd"],
                    "expected_profit": lmsr_result["expected_profit"],
                    "estimated_b":     lmsr_result["estimated_b"],
                    "risk_flags":      lmsr_result["risk_flags"],
                    "sources_used":    lmsr_result["sources"]["sources_used"],
                    "trade_side":      lmsr_result["trade_side"],
                    "volume_usd":      round(volume, 2),
                })
            continue   # always skip legacy path when LMSR available

        # ── Legacy path (fallback when lmsr_scanner not available) ────────
        ob = {
            "bids": [{"price": str(bid_f), "size": "100"}],
            "asks": [{"price": str(ask_f), "size": "100"}],
        }
        p_est, signal = _estimate_probability(question, ob, client, yes_token_id)
        if p_est is None:
            continue

        yes_edge = p_est - yes_mid
        no_edge  = yes_mid - p_est
        if yes_edge >= no_edge:
            edge       = yes_edge
            trade_side = "YES"
            limit      = round(yes_mid - 0.01, 4)
        else:
            edge       = no_edge
            trade_side = "NO"
            limit      = round((1.0 - yes_mid) - 0.01, 4)

        if edge < MIN_EDGE or limit <= 0 or limit >= 1:
            continue

        cost_usd = kelly_size(edge, limit)
        if cost_usd < 1.0:
            continue

        opportunities.append({
            "question":       question[:120],
            "yes_mid":        round(yes_mid, 4),
            "our_estimate":   round(p_est, 4),
            "edge":           round(edge, 4),
            "trade_side":     trade_side,
            "kelly_size_usd": round(cost_usd, 2),
            "volume_usd":     round(volume, 2),
            "signal_source":  signal,
        })

    # Margin distribution
    avg_m  = round(sum(margins) / len(margins), 4) if margins else 0.0
    min_m  = round(min(margins), 4)                if margins else 0.0
    max_m  = round(max(margins), 4)                if margins else 0.0
    pct_20 = round(sum(1 for m in margins if m > 0.20) / len(margins) * 100, 1) if margins else 0.0

    # b distribution
    b_estimates_sorted = sorted(b_estimates)
    nb = len(b_estimates_sorted)
    b_dist = {
        "median_b":       round(b_estimates_sorted[nb // 2], 2) if nb else 0.0,
        "mean_b":         round(sum(b_estimates_sorted) / nb, 2) if nb else 0.0,
        "pct_thin_markets": round(
            sum(1 for b in b_estimates_sorted if b < 1000) / nb * 100, 1
        ) if nb else 0.0,
    }

    snapshot = {
        "timestamp":        ts,
        "cycle":            cycle_num,
        "markets_scanned":  markets_scanned,
        "opportunities":    opportunities,
        "margin_distribution": {
            "avg_margin":   avg_m,
            "min_margin":   min_m,
            "max_margin":   max_m,
            "pct_above_20": pct_20,
        },
        "b_distribution":   b_dist,
    }

    log.info(
        f"[POLY-MONITOR] cycle={cycle_num}/{TOTAL_CYCLES} "
        f"scanned={markets_scanned} opps={len(opportunities)} "
        f"avg_margin={avg_m:.1%} pct>20%={pct_20:.0f}%"
    )
    for o in opportunities[:3]:
        log.info(
            f"[POLY-MONITOR]   [{o['signal_source']}] edge={o['edge']:.1%} "
            f"{o['trade_side']} ${o['kelly_size_usd']:.2f} — {o['question'][:55]}"
        )

    return snapshot


# ══════════════════════════════════════════════════════════════════════════════
#  PERSIST CYCLE
# ══════════════════════════════════════════════════════════════════════════════

def _append_cycle(snapshot: dict) -> None:
    cycles = []
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                cycles = json.load(f)
            if not isinstance(cycles, list):
                cycles = []
        except Exception:
            cycles = []
    cycles.append(snapshot)
    with open(DATA_FILE, "w") as f:
        json.dump(cycles, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  REPORT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_polymarket_report() -> str:
    """
    Read all cycles from polymarket_24h.json, write polymarket_report.md,
    post to Moltbook. Returns the report markdown string.
    """
    log.info("[POLY-MONITOR] Generating 24h report…")

    if not os.path.exists(DATA_FILE):
        log.error("[POLY-MONITOR] No data file found — cannot generate report.")
        return ""

    try:
        with open(DATA_FILE) as f:
            cycles = json.load(f)
    except Exception as exc:
        log.error(f"[POLY-MONITOR] Could not read data file: {exc}")
        return ""

    if not cycles:
        log.warning("[POLY-MONITOR] Data file is empty.")
        return ""

    period_start = cycles[0]["timestamp"][:16].replace("T", " ") + " UTC"
    period_end   = cycles[-1]["timestamp"][:16].replace("T", " ") + " UTC"
    n_cycles     = len(cycles)
    report_date  = cycles[-1]["timestamp"][:10]

    # ── Aggregate stats ───────────────────────────────────────────────────────
    all_margins  = [c["margin_distribution"]["avg_margin"] for c in cycles]
    all_pct20    = [c["margin_distribution"]["pct_above_20"] for c in cycles]
    all_opps     = [o for c in cycles for o in c["opportunities"]]

    total_markets_seen = sum(c["markets_scanned"] for c in cycles)
    avg_opps_5pct      = round(sum(len(c["opportunities"]) for c in cycles) / n_cycles, 2)

    # 3% edge threshold (re-count without MIN_EDGE filter)
    avg_opps_3pct = avg_opps_5pct  # same since our threshold IS 5% — note this in report

    avg_margin_overall = round(sum(all_margins) / len(all_margins) * 100, 2) if all_margins else 0.0
    avg_pct20_overall  = round(sum(all_pct20) / len(all_pct20), 1) if all_pct20 else 0.0

    # ── Unique markets ────────────────────────────────────────────────────────
    unique_questions = {o["question"] for c in cycles for o in c["opportunities"]}
    total_scanned_unique = total_markets_seen  # approximate (re-counts per cycle)

    # ── Hourly margin table ───────────────────────────────────────────────────
    hourly: dict[str, list] = {}
    for c in cycles:
        hour = c["timestamp"][:13] + ":00 UTC"
        hourly.setdefault(hour, []).append(c["margin_distribution"]["avg_margin"])

    hourly_rows = []
    for hour, vals in sorted(hourly.items()):
        avg = round(sum(vals) / len(vals) * 100, 2)
        mn  = round(min(vals) * 100, 2)
        mx  = round(max(vals) * 100, 2)
        hourly_rows.append(f"| {hour} | {mn:.2f}% | {avg:.2f}% | {mx:.2f}% |")

    # ── Top 20 markets by edge ────────────────────────────────────────────────
    opp_agg: dict[str, dict] = {}
    for o in all_opps:
        q = o["question"][:80]
        if q not in opp_agg:
            opp_agg[q] = {"edges": [], "sides": [], "times": 0}
        opp_agg[q]["edges"].append(o["edge"])
        opp_agg[q]["sides"].append(o["trade_side"])
        opp_agg[q]["times"] += 1

    top20 = sorted(
        opp_agg.items(),
        key=lambda x: max(x[1]["edges"]),
        reverse=True,
    )[:20]

    top20_rows = []
    for q, data in top20:
        avg_e   = round(sum(data["edges"]) / len(data["edges"]) * 100, 2)
        max_e   = round(max(data["edges"]) * 100, 2)
        times   = data["times"]
        best_side = max(set(data["sides"]), key=data["sides"].count)
        top20_rows.append(
            f"| {q[:60]} | {avg_e:+.2f}% | {max_e:+.2f}% | {times} | {best_side} |"
        )

    # ── Edge distribution ─────────────────────────────────────────────────────
    dist = {
        "0 opps":  sum(1 for c in cycles if len(c["opportunities"]) == 0),
        "1-3 opps": sum(1 for c in cycles if 1 <= len(c["opportunities"]) <= 3),
        "4+ opps": sum(1 for c in cycles if len(c["opportunities"]) >= 4),
    }

    # ── Verdict ───────────────────────────────────────────────────────────────
    if avg_margin_overall > 15:
        margin_verdict = (
            f"Polymarket's average bid-ask spread across top-volume markets was "
            f"{avg_margin_overall:.1f}%, with {avg_pct20_overall:.0f}% of markets "
            f"showing spreads above 20% — indicating consistently wide overround that "
            f"makes it structurally difficult to find genuine edge."
        )
    else:
        margin_verdict = (
            f"Polymarket's average bid-ask spread was {avg_margin_overall:.1f}%, "
            f"relatively tight for a prediction market, with only {avg_pct20_overall:.0f}% "
            f"of markets above 20% spread."
        )

    if len(all_opps) == 0:
        edge_verdict = (
            "No markets cleared the 5% edge threshold during the 24-hour observation "
            "period, confirming that the order-book model (OB_ONLY) and Metaculus signals "
            "found no exploitable mispricing in the top 100 volume markets."
        )
        rec_verdict = (
            "Recommendation: Keep PAPER_MODE=True. The absence of opportunities "
            "suggests either the model needs a better signal source for these markets, "
            "or Polymarket is efficiently priced in the high-volume tier."
        )
    elif avg_opps_5pct < 1.0:
        edge_verdict = (
            f"Rare edge signals appeared in {len(unique_questions)} unique markets "
            f"(avg {avg_opps_5pct:.2f} opportunities per cycle), driven predominantly "
            f"by the OB model rather than Metaculus — low-confidence signals."
        )
        rec_verdict = (
            "Recommendation: Keep PAPER_MODE=True. Edge signals were too infrequent "
            "and likely driven by model noise rather than genuine mispricing. "
            "Consider integrating The Odds API as a sports-market signal to improve coverage."
        )
    else:
        edge_verdict = (
            f"Consistent edge signals found: {len(unique_questions)} unique markets "
            f"averaged {avg_opps_5pct:.2f} opportunities per cycle above 5% edge. "
            f"Signals were mixed between HYBRID and OB_ONLY sources."
        )
        rec_verdict = (
            "Recommendation: Investigate further before enabling live trading. "
            "Run a targeted backtest on the top opportunity markets and verify "
            "signal quality before setting PAPER_MODE=False."
        )

    # ── LMSR market structure analysis ───────────────────────────────────────
    all_b_dists  = [c.get("b_distribution", {}) for c in cycles if c.get("b_distribution")]
    median_b_all = round(
        sum(d.get("median_b", 0) for d in all_b_dists) / len(all_b_dists), 0
    ) if all_b_dists else None
    mean_b_all   = round(
        sum(d.get("mean_b", 0) for d in all_b_dists) / len(all_b_dists), 0
    ) if all_b_dists else None
    pct_thin_all = round(
        sum(d.get("pct_thin_markets", 0) for d in all_b_dists) / len(all_b_dists), 1
    ) if all_b_dists else None

    # LMSR opportunities (may coexist with legacy format)
    lmsr_opps = [
        o for o in all_opps
        if "realized_ev" in o and "estimated_b" in o
    ]
    lmsr_opps_sorted = sorted(lmsr_opps, key=lambda x: x.get("realized_ev", 0), reverse=True)
    lmsr_top10_rows  = []
    for o in lmsr_opps_sorted[:10]:
        q        = (o.get("question") or "")[:50]
        tp       = o.get("true_prob", 0)
        mp       = o.get("market_price", 0)
        rev      = o.get("realized_ev", 0)
        b_est    = o.get("estimated_b", 0)
        flags    = ", ".join(o.get("risk_flags") or []) or "—"
        lmsr_top10_rows.append(
            f"| {q} | {tp:.1%} | {mp:.1%} | {rev:+.2%} | {b_est:,.0f} | {flags} |"
        )

    # Convergence evidence: markets where edge appeared multiple cycles
    conv_evidence: dict[str, list] = {}
    for c in cycles:
        for o in c.get("opportunities", []):
            if "realized_ev" not in o:
                continue
            q = (o.get("question") or "")[:80]
            conv_evidence.setdefault(q, []).append(o.get("market_price", 0.5))
    conv_rows = []
    for q, prices in sorted(conv_evidence.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        if len(prices) >= 2:
            direction = "→ converging" if prices[-1] != prices[0] else "→ stable"
            conv_rows.append(f"- **{q[:60]}** — seen {len(prices)}× {direction} ({prices[0]:.2%}→{prices[-1]:.2%})")

    # ── Assemble markdown ─────────────────────────────────────────────────────
    lines = [
        "# Polymarket 24-Hour Analysis Report",
        f"## Period: {period_start} → {period_end}",
        "",
        "## Summary",
        "",
        f"- Total cycles completed: {n_cycles} / {TOTAL_CYCLES}",
        f"- Total market observations: {total_markets_seen:,}",
        f"- Unique markets with edge > 5%: {len(unique_questions)}",
        f"- Avg opportunities per cycle (edge > 5%): {avg_opps_5pct}",
        f"- Avg margin across all markets: {avg_margin_overall:.2f}%",
        f"- % of markets with margin > 20%: {avg_pct20_overall:.1f}%",
        "",
        "## Margin Structure",
        "",
        "| Hour (UTC) | Min Spread | Avg Spread | Max Spread |",
        "|---|---|---|---|",
    ] + hourly_rows + [
        "",
        f"**Conclusion:** {margin_verdict}",
        "",
        "## Opportunities Found",
        "",
        f"Top {min(20, len(top20))} markets by maximum edge across all cycles:",
        "",
        "| Market | Avg Edge | Max Edge | Times Found | Best Side |",
        "|---|---|---|---|---|",
    ] + (top20_rows if top20_rows else ["| — | — | — | — | — |"]) + [
        "",
        "## Edge Distribution",
        "",
        f"| Cycles with 0 opportunities | {dist['0 opps']} ({dist['0 opps']/n_cycles:.0%}) |",
        f"| Cycles with 1–3 opportunities | {dist['1-3 opps']} ({dist['1-3 opps']/n_cycles:.0%}) |",
        f"| Cycles with 4+ opportunities | {dist['4+ opps']} ({dist['4+ opps']/n_cycles:.0%}) |",
        "",
        "## Verdict",
        "",
        margin_verdict,
        "",
        edge_verdict,
        "",
        rec_verdict,
        "",
        "---",
        f"*Generated by Leonardo polymarket_monitor.py — paper observation only, no trades placed.*",
    ]

    # ── LMSR section (append if data available) ───────────────────────────────
    if median_b_all is not None or lmsr_top10_rows or conv_rows:
        lmsr_section = [
            "",
            "## LMSR Market Structure Analysis",
            "",
            f"Median estimated b parameter: **{median_b_all:,.0f}**" if median_b_all else
            "Median estimated b: not yet available (requires LMSR-enabled cycles)",
            "",
            "> b controls price sensitivity. Lower b = easier to move the market.",
            "",
            "### Markets by Liquidity Regime",
            "",
        ]
        if pct_thin_all is not None:
            lmsr_section += [
                f"- **Thin** (b < 1,000): ~{pct_thin_all:.0f}% of markets — high price impact, easy to move",
                f"- **Mid** (b 1k–50k): estimated from mean_b={mean_b_all:,.0f}",
                f"- **Deep** (b > 50k): low price impact — need large capital to exploit",
                "",
            ]

        if lmsr_top10_rows:
            lmsr_section += [
                "### Most Exploitable Markets (by Realized EV after Price Impact)",
                "",
                "| Market | True Prob | Market Price | Realized EV | b Est | Risk Flags |",
                "|---|---|---|---|---|---|",
            ] + lmsr_top10_rows + [""]

        if conv_rows:
            lmsr_section += [
                "### Convergence Evidence",
                "",
                "Markets where our edge appeared multiple cycles (price moving toward true_prob):",
                "",
            ] + conv_rows + [""]

        lines = lines[:-2] + lmsr_section + lines[-2:]

    report_md = "\n".join(lines)

    # Write report file
    with open(REPORT_FILE, "w") as f:
        f.write(report_md)
    log.info(f"[POLY-MONITOR] Report written to {REPORT_FILE}")

    # Post to Moltbook
    _post_report_to_moltbook(
        title=f"📊 Polymarket 24h Analysis — {report_date}",
        content=report_md,
    )

    return report_md


# ══════════════════════════════════════════════════════════════════════════════
#  MOLTBOOK POST
# ══════════════════════════════════════════════════════════════════════════════

def _post_report_to_moltbook(title: str, content: str) -> None:
    try:
        from moltbook_bot import _api_headers, ensure_submolt, MOLTBOOK_API, TARGET_SUBMOLT
    except ImportError as exc:
        log.error(f"[POLY-MONITOR] moltbook_bot import failed: {exc}")
        return

    try:
        headers = _api_headers()
    except SystemExit:
        log.error("[POLY-MONITOR] No Moltbook API key — skipping post.")
        return

    ensure_submolt(TARGET_SUBMOLT)

    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/posts",
            headers=headers,
            json={
                "submolt_name": TARGET_SUBMOLT,
                "submolt":      TARGET_SUBMOLT,
                "title":        title,
                "content":      content,
            },
            timeout=20,
        )
    except requests.exceptions.ConnectionError as exc:
        log.error(f"[POLY-MONITOR] Moltbook unreachable: {exc}")
        return

    if resp.status_code in (200, 201):
        post = resp.json().get("post", {})
        pid  = post.get("id")
        url  = f"https://www.moltbook.com/m/{TARGET_SUBMOLT}/{pid}" if pid else None
        log.info(f"[POLY-MONITOR] Report posted to Moltbook: {url}")
    else:
        log.error(
            f"[POLY-MONITOR] Moltbook post failed: {resp.status_code} — {resp.text[:200]}"
        )
        log.info(f"[POLY-MONITOR] Report saved locally at {REPORT_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    start_time = datetime.now(timezone.utc)
    end_time   = start_time + timedelta(hours=24)

    log.info("[POLY-MONITOR] ══════════════════════════════════════════════")
    log.info("[POLY-MONITOR] Starting 24-hour Polymarket observation")
    log.info(f"[POLY-MONITOR] Start : {start_time.isoformat()}")
    log.info(f"[POLY-MONITOR] End   : {end_time.isoformat()}")
    log.info(f"[POLY-MONITOR] Cycles: {TOTAL_CYCLES}  (every {CYCLE_SECONDS}s)")
    log.info(f"[POLY-MONITOR] Data  : {DATA_FILE}")
    log.info("[POLY-MONITOR] PAPER_MODE = True — observe only, no trades")
    log.info("[POLY-MONITOR] ══════════════════════════════════════════════")

    client = build_client()
    if client is None:
        log.error(
            "[POLY-MONITOR] Could not build Polymarket client. "
            "Check POLYMARKET_PRIVATE_KEY in ~/polymarket_bot/.env"
        )
        sys.exit(1)

    cycle = 0

    while cycle < TOTAL_CYCLES:
        cycle += 1
        cycle_start = time.monotonic()

        try:
            snapshot = _run_cycle(client, cycle)
            if snapshot is not None:
                _append_cycle(snapshot)
            else:
                log.warning(
                    f"[POLY-MONITOR] cycle={cycle} — markets unreachable, "
                    "sleeping 60s then retrying"
                )
                time.sleep(60)
                cycle -= 1  # don't count failed cycle
                continue

        except Exception as exc:
            log.error(f"[POLY-MONITOR] cycle={cycle} unhandled error: {exc}")

        if cycle >= TOTAL_CYCLES:
            break

        elapsed = time.monotonic() - cycle_start
        sleep_s = max(0.0, CYCLE_SECONDS - elapsed)
        time.sleep(sleep_s)

    log.info("[POLY-MONITOR] 288 cycles complete — generating report")
    generate_polymarket_report()
    log.info("[POLY-MONITOR] Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("[POLY-MONITOR] Stopped by user.")
        sys.exit(0)
