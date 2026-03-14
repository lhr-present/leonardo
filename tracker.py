#!/usr/bin/env python3
"""
tracker.py — Prediction store for Leonardo
============================================
Adapted from ~/prediction_tracker/tracker.py.
All interactive input() flows removed.
Predictions stored in ~/leonardo/predictions.json.

Functions:
    log_prediction(pick)        — save a pre-built pick dict
    settle_prediction(id, result) — settle WIN or LOSS
    compute_stats(predictions)  — compute full performance stats
    cmd_stats()                 — print stats to terminal
    cmd_export()                — generate report.md
"""

import sys
import json
import os
import math
from datetime import datetime

PREDICTIONS_FILE  = os.path.join(os.path.dirname(__file__), "predictions.json")
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "100.0"))


# ══════════════════════════════════════════════════════════════════════════════
#  PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def load_predictions() -> list:
    if not os.path.exists(PREDICTIONS_FILE):
        return []
    with open(PREDICTIONS_FILE) as f:
        return json.load(f)


def save_predictions(predictions: list) -> None:
    with open(PREDICTIONS_FILE, "w") as f:
        json.dump(predictions, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  BANKROLL
# ══════════════════════════════════════════════════════════════════════════════

def current_bankroll(predictions: list) -> float:
    bankroll = STARTING_BANKROLL
    for p in predictions:
        if p.get("settled") and p.get("profit_loss") is not None:
            bankroll += p["profit_loss"]
    return round(bankroll, 4)


# ══════════════════════════════════════════════════════════════════════════════
#  LOG PREDICTION  (programmatic — no input())
# ══════════════════════════════════════════════════════════════════════════════

def log_prediction(pick: dict) -> dict:
    """
    Save a pre-built prediction dict to predictions.json.
    Assigns next auto-increment ID and adds timestamp if missing.
    Returns the saved record.
    """
    predictions = load_predictions()
    bankroll    = current_bankroll(predictions)
    new_id      = max((p["id"] for p in predictions), default=0) + 1

    our_prob = float(pick["our_probability"])
    odds     = float(pick["market_odds"])
    net_odds = odds - 1.0

    record = {
        "id":                        new_id,
        "timestamp":                 pick.get("timestamp", datetime.utcnow().isoformat() + "Z"),
        "sport":                     pick.get("sport", "football"),
        "league":                    pick.get("league", ""),
        "match":                     pick.get("match", ""),
        "market":                    pick.get("market", ""),
        "selection":                 pick.get("selection", ""),
        "reasoning":                 pick.get("reasoning", ""),
        "our_probability":           round(our_prob, 4),
        "market_odds":               round(odds, 3),
        "implied_probability":       pick.get("implied_probability",
                                         round(1.0 / odds, 4)),
        "edge_percent":              pick.get("edge_percent",
                                         round((our_prob - 1.0 / odds) * 100, 2)),
        "kelly_fraction":            pick.get("kelly_fraction", 0.25),
        "recommended_stake_percent": pick.get("recommended_stake_percent", 0.0),
        "paper_stake_usd":           pick.get("paper_stake_usd",
                                         round(bankroll * pick.get("recommended_stake_percent", 0) / 100, 2)),
        "result":                    None,
        "profit_loss":               None,
        "settled":                   False,
        "fixture_id":                pick.get("fixture_id"),
        "kickoff_utc":               pick.get("kickoff_utc"),
        "moltbook_post_id":          pick.get("moltbook_post_id"),
        "moltbook_post_url":         pick.get("moltbook_post_url"),
    }

    predictions.append(record)
    save_predictions(predictions)
    return record


# ══════════════════════════════════════════════════════════════════════════════
#  SETTLE
# ══════════════════════════════════════════════════════════════════════════════

def settle_prediction(pred_id: int, result_str: str) -> dict:
    result_str = result_str.upper()
    if result_str not in ("WIN", "LOSS"):
        raise ValueError("Result must be WIN or LOSS")

    predictions = load_predictions()
    record      = next((p for p in predictions if p["id"] == pred_id), None)

    if record is None:
        raise KeyError(f"Prediction #{pred_id} not found.")
    if record["settled"]:
        raise ValueError(f"Prediction #{pred_id} already settled ({record['result']}).")

    stake = record["paper_stake_usd"]

    if result_str == "WIN":
        profit_loss = round(stake * (record["market_odds"] - 1.0), 4)
    else:
        profit_loss = round(-stake, 4)

    record["result"]      = result_str
    record["profit_loss"] = profit_loss
    record["settled"]     = True

    save_predictions(predictions)

    # Check milestones after every settlement
    try:
        from moltbook_presence import post_leaderboard_update
        post_leaderboard_update()
    except Exception:
        pass

    return record


def get_prediction_by_id(pred_id: int) -> dict | None:
    return next((p for p in load_predictions() if p["id"] == pred_id), None)


def get_unsettled() -> list[dict]:
    return [p for p in load_predictions() if not p.get("settled")]


def cmd_settle_all() -> None:
    """
    Attempt auto-settlement of all unsettled picks via API-Football.
    Prints which still need manual input.
    """
    unsettled = get_unsettled()
    if not unsettled:
        print("No unsettled picks.")
        return

    try:
        from data import fetch_final_score, search_fixture_by_teams
    except ImportError:
        print("data.py not available — cannot auto-settle.")
        return

    try:
        from moltbook_bot import post_result as _post_result
    except ImportError:
        _post_result = None

    for p in unsettled:
        score = None
        if p.get("fixture_id"):
            score = fetch_final_score(int(p["fixture_id"]))
        else:
            # Try to find by team names + kickoff date
            match = p.get("match", "")
            if " vs " in match:
                home, away = match.split(" vs ", 1)
                date_str = (p.get("kickoff_utc") or p.get("timestamp", ""))[:10]
                if date_str:
                    score = search_fixture_by_teams(home.strip(), away.strip(), date_str)

        if not score:
            print(f"  #{p['id']}: {p['match']} — not found / not FT yet (manual settle needed)")
            continue

        hg, ag = score["home_goals"], score["away_goals"]
        mkt = p.get("market", "").upper()
        sel = p.get("selection", "").upper()

        # Determine result based on market
        if mkt in ("BTTS", "BOTH TEAMS TO SCORE"):
            result = "WIN" if (hg and ag and hg > 0 and ag > 0) else "LOSS"
        elif mkt in ("OVER 2.5", "OVER2.5"):
            result = "WIN" if (hg is not None and ag is not None and hg + ag > 2.5) else "LOSS"
        elif mkt in ("1X2", "MATCH WINNER"):
            if sel in ("HOME", "1"):
                result = "WIN" if hg > ag else "LOSS"
            elif sel in ("AWAY", "2"):
                result = "WIN" if ag > hg else "LOSS"
            else:
                result = "WIN" if hg == ag else "LOSS"
        else:
            print(f"  #{p['id']}: {p['match']} — unknown market '{mkt}' (manual settle needed)")
            continue

        rec = settle_prediction(p["id"], result)
        print(f"  #{p['id']}: {p['match']} {hg}-{ag} → {result}  P&L: ${rec['profit_loss']:+.2f}")
        if _post_result:
            _post_result(rec)


# ══════════════════════════════════════════════════════════════════════════════
#  STATS
# ══════════════════════════════════════════════════════════════════════════════

def compute_stats(predictions: list) -> dict:
    settled      = [p for p in predictions if p["settled"]]
    wins         = [p for p in settled if p["result"] == "WIN"]
    n            = len(settled)
    win_rate     = len(wins) / n if n else 0.0
    total_staked = sum(p["paper_stake_usd"] for p in settled)
    total_pl     = sum(p["profit_loss"] for p in settled)
    roi          = (total_pl / total_staked * 100) if total_staked else 0.0

    brier = 0.0
    if n:
        brier = sum(
            (p["our_probability"] - (1.0 if p["result"] == "WIN" else 0.0)) ** 2
            for p in settled
        ) / n

    avg_edge = (sum(p["edge_percent"] for p in settled) / n) if n else 0.0
    bankroll = current_bankroll(predictions)

    return {
        "total":        len(predictions),
        "settled":      n,
        "wins":         len(wins),
        "win_rate":     round(win_rate * 100, 2),
        "total_staked": round(total_staked, 2),
        "total_pl":     round(total_pl, 2),
        "roi":          round(roi, 2),
        "brier":        round(brier, 4),
        "avg_edge":     round(avg_edge, 2),
        "bankroll":     round(bankroll, 2),
    }


def cmd_stats() -> dict:
    predictions = load_predictions()
    s           = compute_stats(predictions)

    print(f"\n{'═'*50}")
    print(f"  LEONARDO STATS")
    print(f"{'═'*50}")
    print(f"  Total predictions : {s['total']}  ({s['settled']} settled)")
    print(f"  Win / Loss        : {s['wins']} / {s['settled'] - s['wins']}")
    print(f"  Win rate          : {s['win_rate']:.1f}%")
    print(f"  Avg edge          : {s['avg_edge']:+.2f}%")
    print(f"  Total staked      : ${s['total_staked']:.2f}")
    print(f"  Total P&L         : ${s['total_pl']:+.2f}")
    print(f"  ROI               : {s['roi']:+.2f}%")
    print(f"  Brier score       : {s['brier']:.4f}")
    print(f"  Bankroll          : ${s['bankroll']:.2f}  (started ${STARTING_BANKROLL:.2f})")

    bankroll = STARTING_BANKROLL
    curve    = [bankroll]
    for p in [x for x in predictions if x.get("settled")]:
        bankroll += p["profit_loss"]
        curve.append(round(bankroll, 2))

    if len(curve) > 1:
        lo, hi = min(curve), max(curve)
        span   = hi - lo or 1
        blocks = "▁▂▃▄▅▆▇█"
        spark  = "".join(blocks[min(int((v - lo) / span * 7), 7)] for v in curve)
        print(f"  Bankroll curve    : {spark}")

    print(f"{'═'*50}\n")
    return s


def cmd_export() -> str:
    predictions = load_predictions()
    s           = compute_stats(predictions)
    settled     = [p for p in predictions if p["settled"]]
    pending     = [p for p in predictions if not p["settled"]]
    out_path    = os.path.join(os.path.dirname(__file__), "report.md")

    lines = [
        "# Leonardo Prediction Report",
        f"> Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Total predictions | {s['total']} |",
        f"| Settled | {s['settled']} |",
        f"| Win rate | {s['win_rate']:.1f}% |",
        f"| ROI | {s['roi']:+.2f}% |",
        f"| Brier score | {s['brier']:.4f} |",
        f"| Avg edge | {s['avg_edge']:+.2f}% |",
        f"| Bankroll | ${s['bankroll']:.2f} (started ${STARTING_BANKROLL:.2f}) |",
        "",
    ]

    if settled:
        lines += [
            "## Settled Predictions",
            "",
            "| # | Date | Match | Market | Sel | Odds | Our p | Edge | Stake | Result | P&L |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for p in settled:
            date = p["timestamp"][:10]
            lines.append(
                f"| {p['id']} | {date} | {p['match']} | {p['market']} | "
                f"{p['selection']} | {p['market_odds']} | {p['our_probability']:.2f} | "
                f"{p['edge_percent']:+.1f}% | ${p['paper_stake_usd']:.2f} | "
                f"{p['result']} | ${p['profit_loss']:+.2f} |"
            )
        lines.append("")

    if pending:
        lines += [
            "## Open Predictions",
            "",
            "| # | Date | Match | Market | Sel | Odds | Our p | Edge | Stake |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for p in pending:
            date = p["timestamp"][:10]
            lines.append(
                f"| {p['id']} | {date} | {p['match']} | {p['market']} | "
                f"{p['selection']} | {p['market_odds']} | {p['our_probability']:.2f} | "
                f"{p['edge_percent']:+.1f}% | ${p['paper_stake_usd']:.2f} |"
            )
        lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"Report written to: {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "stats":
        cmd_stats()

    elif args[0] == "settle":
        if len(args) < 3:
            print("Usage: python tracker.py settle ID WIN|LOSS")
            sys.exit(1)
        rec = settle_prediction(int(args[1]), args[2])
        predictions = load_predictions()
        bankroll = current_bankroll(predictions)
        print(f"\n  Settled #{rec['id']}: {rec['result']}  P&L: ${rec['profit_loss']:+.2f}")
        print(f"  Running bankroll: ${bankroll:.2f}")

    elif args[0] == "export":
        cmd_export()

    elif args[0] == "settle-all":
        cmd_settle_all()

    else:
        print("Usage: python tracker.py [stats|settle ID WIN|LOSS|settle-all|export]")
        sys.exit(1)
