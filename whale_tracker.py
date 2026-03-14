#!/usr/bin/env python3
"""
whale_tracker.py — Track large Polymarket positions (whale wallets)
====================================================================
Uses the Polymarket data API to find wallets with large positions
and extract their current market bets as probability signals.

Public endpoints — no auth required:
  https://data-api.polymarket.com/positions?sizeThreshold=5000&limit=100
  https://data-api.polymarket.com/profiles/{address}

Usage:
    from whale_tracker import get_whale_signal
    prob = get_whale_signal("Will X happen?")
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Minimum position size in USD to consider a wallet a "whale"
WHALE_THRESHOLD_USD = 5_000
# Minimum number of whales on one side to count as a signal
MIN_WHALE_COUNT     = 2
# Cache lifetime for position data
_POSITIONS_TTL = 900   # 15 minutes

_positions_cache: dict = {}
_token_market_cache: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
#  FETCH LARGE POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_large_positions(size_threshold: float = WHALE_THRESHOLD_USD) -> list[dict]:
    """
    Fetch current positions above size_threshold USD from Polymarket data API.

    Returns list of position dicts:
      {asset: token_id, size: usd_value, outcome: "YES"/"NO", market_id, question}
    """
    cache_key = f"positions_{size_threshold}"
    now = time.time()
    cached = _positions_cache.get(cache_key)
    if cached and cached["ts"] + _POSITIONS_TTL > now:
        return cached["data"]

    positions = []
    offset = 0
    limit  = 100

    while len(positions) < 2_000:
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={
                    "sizeThreshold": size_threshold,
                    "limit":         limit,
                    "offset":        offset,
                },
                headers={"User-Agent": "LeonardoBot/1.0"},
                timeout=15,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            batch = data if isinstance(data, list) else data.get("positions", data.get("data", []))
        except Exception:
            break

        if not batch:
            break

        positions.extend(batch)
        offset += limit
        if len(batch) < limit:
            break
        time.sleep(0.2)

    _positions_cache[cache_key] = {"ts": now, "data": positions}
    return positions


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN → MARKET LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_token_to_market(token_id: str) -> Optional[dict]:
    """
    Look up which market a CLOB token belongs to via Gamma API.
    Returns minimal dict: {market_id, question, yes_token_id, no_token_id}
    Cached indefinitely (token→market mappings don't change).
    """
    if token_id in _token_market_cache:
        return _token_market_cache[token_id]

    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token_id, "limit": 1},
            headers={"User-Agent": "LeonardoBot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            return None
        m = markets[0]
        clob_ids = m.get("clobTokenIds") or "[]"
        try:
            ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
        except Exception:
            ids = []

        result = {
            "market_id":    m.get("id") or m.get("conditionId", ""),
            "question":     m.get("question") or m.get("title") or "",
            "yes_token_id": ids[0] if len(ids) > 0 else "",
            "no_token_id":  ids[1] if len(ids) > 1 else "",
        }
        _token_market_cache[token_id] = result
        return result
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  WHALE SIGNAL EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def get_whale_signal(question: str, jaccard_threshold: float = 0.50) -> Optional[float]:
    """
    Aggregate large-wallet positions for a given market question.

    Logic:
      1. Fetch all positions above WHALE_THRESHOLD_USD
      2. Group by market using token→market lookup
      3. Find the market whose question best matches the input
      4. If >= MIN_WHALE_COUNT whales are on YES: return weighted YES probability
         If >= MIN_WHALE_COUNT whales are on NO:  return weighted NO probability
         Otherwise: None

    The implied probability is: yes_value / (yes_value + no_value)
    clamped to [0.05, 0.95].
    """
    from lmsr_scanner import _jaccard

    positions = fetch_large_positions()
    if not positions:
        return None

    # Group positions by market
    market_data: dict[str, dict] = {}

    for pos in positions:
        token_id = pos.get("asset") or pos.get("token_id") or pos.get("tokenId") or ""
        size_usd = 0.0
        try:
            size_usd = float(pos.get("size") or pos.get("currentValue") or pos.get("value") or 0)
        except (TypeError, ValueError):
            continue

        if size_usd < WHALE_THRESHOLD_USD or not token_id:
            continue

        market = _resolve_token_to_market(token_id)
        if not market:
            continue

        mid = market["market_id"]
        if mid not in market_data:
            market_data[mid] = {
                "question": market["question"],
                "yes_usd":  0.0,
                "no_usd":   0.0,
                "yes_count": 0,
                "no_count":  0,
            }

        is_yes = (token_id == market["yes_token_id"])
        if is_yes:
            market_data[mid]["yes_usd"]   += size_usd
            market_data[mid]["yes_count"] += 1
        else:
            market_data[mid]["no_usd"]   += size_usd
            market_data[mid]["no_count"] += 1

    # Find best matching market
    best_score = 0.0
    best_data: Optional[dict] = None

    for mid, d in market_data.items():
        score = _jaccard(question, d["question"])
        if score > best_score:
            best_score = score
            best_data  = d

    if best_score < jaccard_threshold or best_data is None:
        return None

    yes_usd = best_data["yes_usd"]
    no_usd  = best_data["no_usd"]
    total   = yes_usd + no_usd

    if total <= 0:
        return None

    # Require minimum whale participation
    if (best_data["yes_count"] + best_data["no_count"]) < MIN_WHALE_COUNT:
        return None

    implied_yes = yes_usd / total
    return round(max(0.05, min(0.95, implied_yes)), 4)


# ══════════════════════════════════════════════════════════════════════════════
#  NEW WHALE POSITIONS SINCE LAST CHECK
# ══════════════════════════════════════════════════════════════════════════════

_STATE_FILE = os.path.join(_DIR, "whale_state.json")


def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"seen_positions": {}, "last_check": None}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def get_new_whale_positions(size_threshold: float = WHALE_THRESHOLD_USD) -> list[dict]:
    """
    Return positions that are NEW since the last call (based on state file).
    Each returned item has: question, side (YES/NO), size_usd, token_id, market_id.

    Call this every 15 minutes from whale_monitor.py.
    """
    state = _load_state()
    seen  = state.get("seen_positions", {})

    positions = fetch_large_positions(size_threshold)
    new_ones: list[dict] = []

    for pos in positions:
        token_id = pos.get("asset") or pos.get("token_id") or pos.get("tokenId") or ""
        size_usd = 0.0
        try:
            size_usd = float(pos.get("size") or pos.get("currentValue") or pos.get("value") or 0)
        except (TypeError, ValueError):
            continue

        if not token_id or size_usd < size_threshold:
            continue

        # Create a stable position key
        user = pos.get("proxyWallet") or pos.get("maker") or pos.get("user") or "unknown"
        pos_key = f"{user}:{token_id}"

        prev_size = seen.get(pos_key, 0.0)
        if size_usd > prev_size + 500:   # new or significantly increased
            market = _resolve_token_to_market(token_id)
            if not market:
                continue

            is_yes = (token_id == market["yes_token_id"])
            new_ones.append({
                "question":     market["question"],
                "market_id":    market["market_id"],
                "token_id":     token_id,
                "side":         "YES" if is_yes else "NO",
                "size_usd":     round(size_usd, 2),
                "prev_size_usd": round(prev_size, 2),
                "wallet":       user[:12] + "…" if len(user) > 12 else user,
                "detected_at":  datetime.now(timezone.utc).isoformat(),
            })

        seen[pos_key] = size_usd

    # Clean up stale keys (positions that have closed)
    active_keys = set()
    for pos in positions:
        token_id = pos.get("asset") or pos.get("token_id") or pos.get("tokenId") or ""
        user     = pos.get("proxyWallet") or pos.get("maker") or pos.get("user") or "unknown"
        if token_id:
            active_keys.add(f"{user}:{token_id}")
    seen = {k: v for k, v in seen.items() if k in active_keys}

    state["seen_positions"] = seen
    state["last_check"]     = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    return new_ones
