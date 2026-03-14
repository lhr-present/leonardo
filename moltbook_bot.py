#!/usr/bin/env python3
"""
moltbook_bot.py — Moltbook integration for Leonardo
=====================================================
Copied and adapted from ~/prediction_tracker/moltbook_bot.py.
Shares the same credentials file. Added:
  - post_daily_picks(picks)   — post a batch of today's picks in one post
  - post_weekly_digest()      — post a weekly performance summary

All other functions (post_prediction, post_result, post_schedule) are
preserved exactly as in the original.
"""

import os
import sys
import json
import time
import re
import requests
from datetime import datetime
from typing import Optional

try:
    from x_poster import post_to_x, format_prediction_tweet
    from x_poster import format_result_tweet, format_weather_tweet
    _X_AVAILABLE = True
except ImportError:
    _X_AVAILABLE = False

MOLTBOOK_API     = "https://moltbook.com/api/v1"
CREDENTIALS_FILE = os.path.expanduser("~/.config/moltbook/credentials.json")
AGENT_NAME       = "edgefinderbot2"
TARGET_SUBMOLT   = "sports"

# Predictions file (Leonardo's own store)
_PREDICTIONS_FILE = os.path.join(os.path.dirname(__file__), "predictions.json")


# ══════════════════════════════════════════════════════════════════════════════
#  CREDENTIALS (shared with prediction_tracker)
# ══════════════════════════════════════════════════════════════════════════════

def load_credentials() -> Optional[dict]:
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
#  HEADERS & SUBMOLT
# ══════════════════════════════════════════════════════════════════════════════

def _api_headers() -> dict:
    creds = load_credentials()
    if not creds or not creds.get("api_key") or creds["api_key"] == "PENDING_REGISTRATION":
        print("No valid API key. Run register_agent() from prediction_tracker/moltbook_bot.py first.")
        sys.exit(1)
    return {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}


def ensure_submolt(name: str) -> Optional[str]:
    headers = _api_headers()
    try:
        resp = requests.get(f"{MOLTBOOK_API}/submolts/{name}", headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("id") or name
    except Exception:
        pass

    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/submolts",
            headers=headers,
            json={
                "name":         name,
                "display_name": name.capitalize(),
                "description":  "Sports prediction tracking — picks logged before kickoff.",
            },
            timeout=10,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return data.get("id") or data.get("slug") or name
        print(f"Could not create submolt '{name}': {resp.status_code} — {resp.text[:200]}")
    except Exception as exc:
        print(f"Could not create submolt '{name}': {exc}")

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  STATS HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _get_stats() -> dict:
    if not os.path.exists(_PREDICTIONS_FILE):
        return {"total": 0, "win_rate": 0.0, "roi": 0.0, "bankroll": 100.0, "settled": 0}

    with open(_PREDICTIONS_FILE) as f:
        predictions = json.load(f)

    settled      = [p for p in predictions if p.get("settled")]
    wins         = [p for p in settled if p.get("result") == "WIN"]
    n            = len(settled)
    win_rate     = (len(wins) / n * 100) if n else 0.0
    total_staked = sum(p.get("paper_stake_usd", 0) for p in settled)
    total_pl     = sum(p.get("profit_loss", 0) for p in settled)
    roi          = (total_pl / total_staked * 100) if total_staked else 0.0
    bankroll     = 100.0 + total_pl

    return {
        "total":    len(predictions),
        "settled":  n,
        "win_rate": round(win_rate, 1),
        "roi":      round(roi, 2),
        "bankroll": round(bankroll, 2),
    }


def _save_post_id(pred_id: int, post_id, post_url) -> None:
    if not os.path.exists(_PREDICTIONS_FILE):
        return
    with open(_PREDICTIONS_FILE) as f:
        predictions = json.load(f)
    for p in predictions:
        if p["id"] == pred_id:
            p["moltbook_post_id"]  = post_id
            p["moltbook_post_url"] = post_url
    with open(_PREDICTIONS_FILE, "w") as f:
        json.dump(predictions, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  POST SINGLE PREDICTION
# ══════════════════════════════════════════════════════════════════════════════

def post_prediction(prediction: dict) -> Optional[str]:
    """Post one prediction. Returns URL or None."""
    stats = _get_stats()

    title = (
        f"📊 [{prediction['league']}] {prediction['match']} — "
        f"{prediction['market']} {prediction['selection']} @ {prediction['market_odds']}"
    )

    body_lines = [
        f"**Match:** {prediction['match']}",
        f"**League:** {prediction['league']}",
        f"**Market:** {prediction['market']}  |  **Selection:** {prediction['selection']}",
        f"**Odds:** {prediction['market_odds']} (implied {prediction['implied_probability']:.1%})",
        "",
        f"**Our probability:** {prediction['our_probability']:.1%}",
        f"**Edge:** {prediction['edge_percent']:+.2f}%",
        f"**Kelly stake:** {prediction['recommended_stake_percent']:.2f}% of bankroll "
        f"= ${prediction['paper_stake_usd']:.2f}",
        "",
        f"**Reasoning:** {prediction.get('reasoning', '—')}",
        "",
        "---",
        f"*Paper trade only. Track record: {stats['total']} picks, "
        f"{stats['win_rate']:.1f}% win rate, {stats['roi']:+.1f}% ROI*",
    ]

    headers = _api_headers()
    ensure_submolt(TARGET_SUBMOLT)

    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/posts",
            headers=headers,
            json={
                "submolt_name": TARGET_SUBMOLT,
                "submolt":      TARGET_SUBMOLT,
                "title":        title,
                "content":      "\n".join(body_lines),
            },
            timeout=15,
        )
    except requests.exceptions.ConnectionError as exc:
        print(f"Moltbook unreachable: {exc}")
        return None

    if resp.status_code in (200, 201):
        data     = resp.json()
        post     = data.get("post", data)
        post_id  = post.get("id")
        post_url = f"https://www.moltbook.com/m/{TARGET_SUBMOLT}/{post_id}" if post_id else None
        print(f"Posted prediction #{prediction['id']}. URL: {post_url}")
        _save_post_id(prediction["id"], post_id, post_url)
        if _X_AVAILABLE:
            post_to_x(format_prediction_tweet(prediction))
        return post_url

    print(f"Post failed: {resp.status_code} — {resp.text[:300]}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  POST RESULT
# ══════════════════════════════════════════════════════════════════════════════

def post_result(prediction: dict) -> Optional[str]:
    """Post the settled result as a comment on the original prediction post."""
    if os.path.exists(_PREDICTIONS_FILE):
        with open(_PREDICTIONS_FILE) as f:
            predictions = json.load(f)
        record = next((p for p in predictions if p["id"] == prediction["id"]), prediction)
    else:
        record = prediction

    post_id = record.get("moltbook_post_id")
    if not post_id:
        print(f"No Moltbook post ID for prediction #{prediction['id']} — cannot comment.")
        return None

    stats         = _get_stats()
    result_emoji  = "✅" if record["result"] == "WIN" else "❌"

    comment_lines = [
        f"{result_emoji} **Result: {record['result']}**",
        "",
        f"P&L: ${record['profit_loss']:+.2f}",
        f"Running bankroll: ${stats['bankroll']:.2f}",
        "",
        f"Updated stats: {stats['settled']} settled | "
        f"{stats['win_rate']:.1f}% win rate | {stats['roi']:+.1f}% ROI",
    ]

    headers = _api_headers()
    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/posts/{post_id}/comments",
            headers=headers,
            json={"content": "\n".join(comment_lines)},
            timeout=15,
        )
    except requests.exceptions.ConnectionError as exc:
        print(f"Moltbook unreachable: {exc}")
        return None

    if resp.status_code in (200, 201):
        data = resp.json()
        url  = data.get("url") or data.get("comment_url")
        print(f"Result posted for #{prediction['id']}: {record['result']}")
        if _X_AVAILABLE:
            post_to_x(format_result_tweet(record))
        return url

    print(f"Comment failed: {resp.status_code} — {resp.text[:300]}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  POST DAILY PICKS  (new — batch post for Leonardo's automated picks)
# ══════════════════════════════════════════════════════════════════════════════

def post_daily_picks(picks: list[dict]) -> Optional[str]:
    """
    Post a batch of today's picks in a single Moltbook post.
    `picks` is a list of prediction records (already saved to predictions.json).
    Returns the post URL or None.
    """
    if not picks:
        return None

    stats = _get_stats()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    title = f"📅 Daily Picks — {today} ({len(picks)} pick{'s' if len(picks) != 1 else ''})"

    lines = [
        f"# Daily Picks — {today}",
        "",
        f"Auto-generated by Leonardo. All picks logged before kickoff.",
        "",
        "| # | Match | Market | Sel | Odds | Edge | Stake |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in picks:
        lines.append(
            f"| {p['id']} | {p['match']} | {p['market']} | {p['selection']} | "
            f"{p['market_odds']} | {p['edge_percent']:+.1f}% | ${p['paper_stake_usd']:.2f} |"
        )

    lines += [
        "",
        "---",
        f"*Track record: {stats['total']} picks, {stats['win_rate']:.1f}% win rate, "
        f"{stats['roi']:+.1f}% ROI | Bankroll: ${stats['bankroll']:.2f}*",
    ]

    headers = _api_headers()
    ensure_submolt(TARGET_SUBMOLT)

    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/posts",
            headers=headers,
            json={
                "submolt_name": TARGET_SUBMOLT,
                "submolt":      TARGET_SUBMOLT,
                "title":        title,
                "content":      "\n".join(lines),
            },
            timeout=15,
        )
    except requests.exceptions.ConnectionError as exc:
        print(f"Moltbook unreachable: {exc}")
        return None

    if resp.status_code in (200, 201):
        post = resp.json().get("post", {})
        pid  = post.get("id")
        url  = f"https://www.moltbook.com/m/{TARGET_SUBMOLT}/{pid}" if pid else None
        print(f"Daily picks posted. URL: {url}")
        return url

    print(f"Daily picks post failed: {resp.status_code} — {resp.text[:300]}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  POST WEEKLY DIGEST  (new)
# ══════════════════════════════════════════════════════════════════════════════

def post_weekly_digest() -> Optional[str]:
    """Post a weekly performance digest to Moltbook."""
    stats   = _get_stats()
    week_of = datetime.utcnow().strftime("%Y-%m-%d")
    title   = f"📈 Weekly Digest — {week_of}"

    lines = [
        f"# Weekly Performance Digest",
        f"**Period ending:** {week_of}",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Total picks | {stats['total']} |",
        f"| Settled | {stats['settled']} |",
        f"| Win rate | {stats['win_rate']:.1f}% |",
        f"| ROI | {stats['roi']:+.2f}% |",
        f"| Bankroll | ${stats['bankroll']:.2f} (started $100.00) |",
        "",
        "*All picks logged before kickoff. Paper money only. "
        "This is a calibration experiment, not financial advice.*",
    ]

    headers = _api_headers()
    ensure_submolt(TARGET_SUBMOLT)

    try:
        resp = requests.post(
            f"{MOLTBOOK_API}/posts",
            headers=headers,
            json={
                "submolt_name": TARGET_SUBMOLT,
                "submolt":      TARGET_SUBMOLT,
                "title":        title,
                "content":      "\n".join(lines),
            },
            timeout=15,
        )
    except requests.exceptions.ConnectionError as exc:
        print(f"Moltbook unreachable: {exc}")
        return None

    if resp.status_code in (200, 201):
        post = resp.json().get("post", {})
        pid  = post.get("id")
        url  = f"https://www.moltbook.com/m/{TARGET_SUBMOLT}/{pid}" if pid else None
        print(f"Weekly digest posted. URL: {url}")
        return url

    print(f"Weekly digest post failed: {resp.status_code} — {resp.text[:300]}")
    return None
