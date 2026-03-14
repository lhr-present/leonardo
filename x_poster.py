#!/usr/bin/env python3
"""
x_poster.py — Post picks and results to X (Twitter) via Tweepy API v2
======================================================================
Credentials from ~/leonardo/.env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET

If any credential is blank: all functions return None silently.
ONE warning is logged at import time only. Never crashes.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

_DIR        = os.path.dirname(os.path.abspath(__file__))
OUTBOX_FILE = os.path.join(_DIR, "x_outbox.json")
load_dotenv(os.path.join(_DIR, ".env"))

log = logging.getLogger("leonardo.x_poster")

# ── Load credentials ──────────────────────────────────────────────────────────
_API_KEY       = os.environ.get("X_API_KEY", "")
_API_SECRET    = os.environ.get("X_API_SECRET", "")
_ACCESS_TOKEN  = os.environ.get("X_ACCESS_TOKEN", "")
_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET", "")

_CREDENTIALS_OK = all([_API_KEY, _API_SECRET, _ACCESS_TOKEN, _ACCESS_SECRET])
_tweepy_client  = None

if not _CREDENTIALS_OK:
    log.warning(
        "X credentials not configured — X posting disabled. "
        "Set X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET in .env"
    )
else:
    try:
        import tweepy as _tweepy
        _tweepy_client = _tweepy.Client(
            consumer_key        = _API_KEY,
            consumer_secret     = _API_SECRET,
            access_token        = _ACCESS_TOKEN,
            access_token_secret = _ACCESS_SECRET,
        )
    except ImportError:
        log.warning("tweepy not installed — X posting disabled. Run: pip install tweepy")
    except Exception as exc:
        log.warning(f"Failed to initialise Tweepy client: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  CORE POST
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  OUTBOX QUEUE
# ══════════════════════════════════════════════════════════════════════════════

def _load_outbox() -> list:
    try:
        if os.path.exists(OUTBOX_FILE):
            with open(OUTBOX_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_outbox(outbox: list) -> None:
    try:
        with open(OUTBOX_FILE, "w") as f:
            json.dump(outbox, f, indent=2)
    except Exception as exc:
        log.warning(f"Failed to save outbox: {exc}")


def queue_tweet(text: str, priority: str = "normal") -> None:
    """
    Save tweet to outbox if post_to_x() fails.
    Max queue size: 50 (drops oldest if full).
    """
    if len(text) > 280:
        text = text[:279] + "…"
    outbox = _load_outbox()
    outbox.append({
        "text":      text,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "priority":  priority,
        "attempts":  0,
    })
    if len(outbox) > 50:
        outbox = outbox[-50:]   # keep newest 50
    _save_outbox(outbox)
    log.info(f"Tweet queued for retry (outbox size: {len(outbox)})")


def flush_outbox() -> int:
    """
    Try to post all queued tweets. Stops on 3 consecutive failures.
    Returns number successfully posted.
    """
    outbox = _load_outbox()
    if not outbox:
        return 0

    posted      = 0
    consecutive = 0
    remaining   = []

    for item in outbox:
        if consecutive >= 3:
            remaining.append(item)
            continue

        item["attempts"] = item.get("attempts", 0) + 1
        url = post_to_x(item["text"])
        if url:
            posted      += 1
            consecutive  = 0
            log.info(f"Flushed queued tweet: {url}")
        else:
            consecutive += 1
            remaining.append(item)
            time.sleep(1)

    _save_outbox(remaining)
    if posted:
        log.info(f"Outbox flush: {posted} posted, {len(remaining)} remaining")
    return posted


# ══════════════════════════════════════════════════════════════════════════════
#  CORE POST
# ══════════════════════════════════════════════════════════════════════════════

def post_to_x(text: str) -> Optional[str]:
    """Post tweet via Tweepy API v2. Returns tweet URL or None.
    On failure queues the tweet for later retry via flush_outbox()."""
    if _tweepy_client is None:
        return None
    if len(text) > 280:
        text = text[:279] + "…"
    try:
        response = _tweepy_client.create_tweet(text=text, user_auth=True)
        tweet_id = response.data["id"]
        return f"https://x.com/NotTrades/status/{tweet_id}"
    except Exception as exc:
        log.warning(f"X post failed: {exc}")
        queue_tweet(text)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  TWEET FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

def format_prediction_tweet(pick: dict) -> str:
    """
    Format a pre-kickoff edge pick as a tweet (≤ 280 chars).

    Example output:
      🔍 EDGE — Bundesliga
      Leverkusen vs Bayern | BTTS Yes
      Model: 67.7% | Market: 58.8%
      Edge: +8.9% | Kelly: 5.4%

      Logged before kickoff ⏱️
      Record: moltbook.com/u/edgefinderbot2

      #Polymarket #EdgeFinder #SportsBetting
    """
    league  = pick.get("league", "")
    match   = pick.get("match", "")
    market  = pick.get("market", "")
    sel     = pick.get("selection", "")
    our_p   = float(pick.get("our_probability", 0))
    implied = float(pick.get("implied_probability", 0))
    edge    = float(pick.get("edge_percent", 0))
    kelly   = float(pick.get("recommended_stake_percent", 0))

    text = (
        f"🔍 EDGE — {league}\n"
        f"{match} | {market} {sel}\n"
        f"Model: {our_p:.1%} | Market: {implied:.1%}\n"
        f"Edge: {edge:+.1f}% | Kelly: {kelly:.1f}%\n\n"
        f"Logged before kickoff ⏱️\n"
        f"Record: moltbook.com/u/edgefinderbot2\n\n"
        f"#Polymarket #EdgeFinder #SportsBetting"
    )
    if len(text) > 280:
        text = text[:279] + "…"
    return text


def format_weather_tweet(edge: dict) -> str:
    """
    Format a weather edge alert as a tweet (≤ 280 chars).

    Example output:
      🌡️ WEATHER EDGE — Munich 2026-03-14
      Market: 14.0% | Model: 27.4%
      Gap: +13.4% | Signal: BUY YES
      Consensus: 2 sources

      Timestamped before resolution ⏱️
      moltbook.com/u/edgefinderbot2

      #Polymarket #WeatherTrading #PredictionMarkets
    """
    city      = str(edge.get("city", "")).title()
    date      = edge.get("target_date", "")
    poly_p    = float(edge.get("poly_prob", 0))
    our_p     = float(edge.get("our_prob", 0))
    gap       = float(edge.get("gap", 0))
    direction = str(edge.get("direction", "")).split("(")[0].strip()
    n_src     = edge.get("sources_agreed", 0)

    text = (
        f"🌡️ WEATHER EDGE — {city} {date}\n"
        f"Market: {poly_p:.1%} | Model: {our_p:.1%}\n"
        f"Gap: {gap:+.1%} | Signal: {direction}\n"
        f"Consensus: {n_src} sources\n\n"
        f"Timestamped before resolution ⏱️\n"
        f"moltbook.com/u/edgefinderbot2\n\n"
        f"#Polymarket #WeatherTrading #PredictionMarkets"
    )
    if len(text) > 280:
        text = text[:279] + "…"
    return text


def format_result_tweet(pick: dict) -> str:
    """
    Format a settled result tweet with running record (≤ 280 chars).
    Gets win_count, loss_count, roi from tracker.compute_stats().

    Example output:
      ✅ WIN — Leverkusen vs Bayern (BTTS Yes)
      Model said: 67.7% | Market: 58.8%
      P&L: +5.48 paper

      Record: 3W/1L | ROI: +12.3%
      moltbook.com/u/edgefinderbot2

      #Verified #Polymarket #PredictionMarkets
    """
    result  = pick.get("result", "?")
    match   = pick.get("match", "")
    market  = pick.get("market", "")
    sel     = pick.get("selection", "")
    our_p   = float(pick.get("our_probability", 0))
    implied = float(pick.get("implied_probability", 0))
    pl      = float(pick.get("profit_loss", 0))

    win_count  = 0
    loss_count = 0
    roi        = 0.0
    try:
        from tracker import compute_stats, load_predictions
        s          = compute_stats(load_predictions())
        win_count  = s.get("wins", 0)
        loss_count = s.get("settled", 0) - win_count
        roi        = s.get("roi", 0.0)
    except Exception:
        pass

    emoji = "✅" if result == "WIN" else "❌"
    text  = (
        f"{emoji} {result} — {match} ({market} {sel})\n"
        f"Model said: {our_p:.1%} | Market: {implied:.1%}\n"
        f"P&L: {pl:+.2f} paper\n\n"
        f"Record: {win_count}W/{loss_count}L | ROI: {roi:+.1f}%\n"
        f"moltbook.com/u/edgefinderbot2\n\n"
        f"#Verified #Polymarket #PredictionMarkets"
    )
    if len(text) > 280:
        text = text[:279] + "…"
    return text


def format_weekly_tweet(stats: dict) -> str:
    """
    Format a weekly performance summary tweet (≤ 280 chars).

    Example output:
      📊 Leonardo Weekly — 2026-03-14
      5 picks | 60% win rate | +8.3% ROI
      Best edge: BTTS markets +12.1%
      Bankroll: $108.30 (started $100)

      All picks posted BEFORE kickoff.
      moltbook.com/u/edgefinderbot2

      #Polymarket #PredictionMarkets #EdgeFinder
    """
    from datetime import datetime
    date       = datetime.utcnow().strftime("%Y-%m-%d")
    total      = stats.get("total", 0)
    win_rate   = float(stats.get("win_rate", 0))
    roi        = float(stats.get("roi", 0))
    bankroll   = float(stats.get("bankroll", 100.0))
    avg_edge   = float(stats.get("avg_edge", 0))
    best_mkt   = stats.get("best_edge_market", "")
    best_edge  = float(stats.get("best_edge", avg_edge))

    if best_mkt:
        best_line = f"Best edge: {best_mkt[:28]} +{best_edge:.1f}%"
    else:
        best_line = f"Avg edge: +{avg_edge:.1f}%"

    text = (
        f"📊 Leonardo Weekly — {date}\n"
        f"{total} picks | {win_rate:.0f}% win rate | {roi:+.1f}% ROI\n"
        f"{best_line}\n"
        f"Bankroll: ${bankroll:.2f} (started $100)\n\n"
        f"All picks posted BEFORE kickoff.\n"
        f"moltbook.com/u/edgefinderbot2\n\n"
        f"#Polymarket #PredictionMarkets #EdgeFinder"
    )
    if len(text) > 280:
        text = text[:279] + "…"
    return text
