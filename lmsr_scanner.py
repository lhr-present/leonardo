#!/usr/bin/env python3
"""
lmsr_scanner.py — LMSR-aware Polymarket market scanner
=======================================================
Integrates the LMSR engine with external probability sources
(FedWatch, weather models, Metaculus) to find genuine edges
with proper price-impact analysis.

PAPER_MODE = True everywhere — no orders placed.

Usage:
    from lmsr_scanner import scan_market_lmsr
    result = scan_market_lmsr(market_dict)
"""

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, ".env"))

sys.path.insert(0, _DIR)

from lmsr_engine import (
    estimate_b_from_market,
    infer_q_from_price,
    marginal_price,
    simulate_buy,
    optimal_entry_size,
    expected_value,
    convergence_profit,
)
from polymarket import fetch_external_probability

# Weather helpers — imported lazily to avoid slow startup when not needed
_weather_imported = False
_consensus_probability  = None
_extract_city_fn        = None
_extract_date_fn        = None
_geocode_fn             = None
_hours_to_resolution_fn = None


def _ensure_weather_imports() -> bool:
    global _weather_imported, _consensus_probability
    global _extract_city_fn, _extract_date_fn, _geocode_fn, _hours_to_resolution_fn
    if _weather_imported:
        return True
    try:
        from weather_edge import (
            consensus_probability,
            _extract_city,
            _extract_date,
            _geocode,
            _hours_to_resolution,
        )
        _consensus_probability  = consensus_probability
        _extract_city_fn        = _extract_city
        _extract_date_fn        = _extract_date
        _geocode_fn             = _geocode
        _hours_to_resolution_fn = _hours_to_resolution
        _weather_imported       = True
        return True
    except ImportError:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  EXTERNAL PROBABILITY SOURCES
# ══════════════════════════════════════════════════════════════════════════════

_fedwatch_cache: dict    = {}
_FEDWATCH_TTL  : int     = 1800   # 30 minutes

def source_fedwatch() -> Optional[dict]:
    """
    Fetch CME FedWatch implied probabilities for the next FOMC meeting.
    Returns {"cut_prob": X, "hold_prob": X, "hike_prob": X} or None on failure.
    Cached for 30 minutes.
    """
    now = time.time()
    if _fedwatch_cache.get("ts", 0) + _FEDWATCH_TTL > now:
        return _fedwatch_cache.get("data")

    # Try CME FedWatch JSON endpoint
    urls = [
        "https://www.cmegroup.com/CmeWS/mvc/MktData/getFedWatch.do?isArchived=false",
        "https://www.cmegroup.com/CmeWS/mvc/MktData/getFedWatch.do",
    ]
    for url in urls:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LeonardoBot/1.0)",
                    "Accept":     "application/json",
                    "Referer":    "https://www.cmegroup.com/",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            # Parse CME FedWatch structure
            # Typical fields: meetings → list of {meetingDate, probabilities}
            meetings = data if isinstance(data, list) else data.get("meetings", [])
            if not meetings:
                # Try alternate key names
                meetings = data.get("data", [])
            if not meetings:
                continue

            # Use the nearest upcoming meeting
            first = meetings[0] if meetings else {}
            probs = (
                first.get("probabilities")
                or first.get("targetProbabilities")
                or first.get("prob", {})
            )
            if not probs or not isinstance(probs, dict):
                continue

            # Normalise key names (CME uses various field names)
            cut_p  = _find_prob(probs, ["cut25", "cut", "decrease", "down25", "lower"])
            hold_p = _find_prob(probs, ["hold", "unchanged", "noChange", "flat"])
            hike_p = _find_prob(probs, ["hike25", "hike", "increase", "up25", "higher"])

            # Fill missing with complement
            known = sum(p for p in [cut_p, hold_p, hike_p] if p is not None)
            if cut_p  is None: cut_p  = max(0.0, 1.0 - known)
            if hold_p is None: hold_p = max(0.0, 1.0 - known)
            if hike_p is None: hike_p = 0.0

            result = {
                "cut_prob":  round(float(cut_p),  4),
                "hold_prob": round(float(hold_p), 4),
                "hike_prob": round(float(hike_p), 4),
            }
            _fedwatch_cache["ts"]   = now
            _fedwatch_cache["data"] = result
            return result

        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue

    # Silently fail — FedWatch is often blocked for programmatic access
    _fedwatch_cache["ts"]   = now
    _fedwatch_cache["data"] = None
    return None


def _find_prob(probs: dict, keys: list[str]) -> Optional[float]:
    for k in keys:
        if k in probs:
            v = probs[k]
            try:
                f = float(v)
                return f / 100.0 if f > 1.0 else f
            except (TypeError, ValueError):
                pass
    return None


def source_weather(city: str, lat: float, lon: float, question: str) -> Optional[dict]:
    """
    Get weather forecast probability using consensus model from weather_edge.py.
    Returns {"prob": float, "confidence": str, "sources_agreed": int, "details": dict}
    or None if weather_edge unavailable or no applicable metric.
    """
    if not _ensure_weather_imports():
        return None
    target_date = _extract_date_fn(question)
    prob, conf, n_src, details = _consensus_probability(
        lat, lon, city, target_date, question,
    )
    if prob is None:
        return None
    return {"prob": prob, "confidence": conf, "sources_agreed": n_src, "details": details}


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "will", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "to", "of", "in",
    "on", "at", "by", "for", "with", "and", "or", "but", "if", "as",
    "it", "this", "that", "from", "above", "below", "more", "than",
}

def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of keyword token sets (stopwords removed)."""
    def tokens(text: str) -> set:
        words = re.sub(r"[?.!,;:\"'()°%$]", " ", text.lower()).split()
        return {w for w in words if w and w not in _STOPWORDS}
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_metaculus_cache: dict = {}
_METACULUS_HOST = "https://www.metaculus.com/api2"

def source_metaculus(question: str) -> Optional[float]:
    """
    Fetch Metaculus community median for question.
    Uses Jaccard >= 0.45 (lower than polymarket.py's 0.70) to catch more matches.
    Cached per question for the session.
    """
    if question in _metaculus_cache:
        return _metaculus_cache[question]

    try:
        search_query = urllib.parse.quote(question[:80])
        url = f"{_METACULUS_HOST}/questions/?search={search_query}&limit=10"
        resp = requests.get(
            url,
            headers={"User-Agent": "LeonardoBot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data if isinstance(data, list) else data.get("results", data.get("data", []))
    except Exception:
        return None

    best_score = 0.0
    best_prob: Optional[float] = None

    for item in results:
        title = item.get("title", "")
        score = _jaccard(question, title)
        if score <= best_score:
            continue
        cp   = item.get("community_prediction") or {}
        full = cp.get("full") or {}
        q2   = full.get("q2") or cp.get("q2") or cp.get("prediction")
        if q2 is not None:
            best_score = score
            best_prob  = float(q2)

    if best_score >= 0.45 and best_prob is not None:
        _metaculus_cache[question] = best_prob
        return best_prob

    _metaculus_cache[question] = None
    return None


# ── Kalshi ────────────────────────────────────────────────────────────────────

_kalshi_cache: dict = {}
_KALSHI_TTL = 600   # 10 minutes
_KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

def source_kalshi(question: str) -> Optional[float]:
    """
    Search Kalshi public API for a matching market and return its YES probability.
    Kalshi covers elections, fed rates, weather, sports, crypto — nearly everything
    Polymarket covers, making it the best cross-reference source.

    Endpoint: GET /markets?limit=100&status=open
    Jaccard >= 0.35 (lower threshold — different phrasing for same events).
    Cached per question for 10 minutes.
    """
    now = time.time()
    cache_entry = _kalshi_cache.get(question)
    if cache_entry and cache_entry["ts"] + _KALSHI_TTL > now:
        return cache_entry["prob"]

    # Build search keywords from question
    keywords = re.sub(r"[?.!,;:\"'()°%$]", " ", question.lower()).split()
    keywords = [w for w in keywords if w and w not in _STOPWORDS and len(w) > 2]
    search_term = " ".join(keywords[:6]) if keywords else question[:60]

    try:
        resp = requests.get(
            f"{_KALSHI_API}/markets",
            params={
                "limit":  100,
                "search": search_term[:60],
            },
            headers={"User-Agent": "LeonardoBot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            _kalshi_cache[question] = {"ts": now, "prob": None}
            return None
        data = resp.json()
        markets = data.get("markets", data if isinstance(data, list) else [])
    except Exception:
        _kalshi_cache[question] = {"ts": now, "prob": None}
        return None

    best_score = 0.0
    best_prob: Optional[float] = None

    for m in markets:
        title = m.get("title") or m.get("question") or m.get("subtitle") or ""
        score = _jaccard(question, title)
        if score <= best_score:
            continue

        # Kalshi yes_bid/yes_ask or last_price — convert cents → decimal
        yes_price = None
        for field in ["yes_bid", "yes_ask", "last_price", "yes_sub_title"]:
            raw = m.get(field)
            if raw is not None:
                try:
                    p = float(raw)
                    yes_price = p / 100.0 if p > 1.0 else p
                    break
                except (TypeError, ValueError):
                    continue

        if yes_price is not None and 0.01 <= yes_price <= 0.99:
            best_score = score
            best_prob  = yes_price

    if best_score >= 0.35 and best_prob is not None:
        _kalshi_cache[question] = {"ts": now, "prob": best_prob}
        return best_prob

    _kalshi_cache[question] = {"ts": now, "prob": None}
    return None


# ── Manifold Markets ──────────────────────────────────────────────────────────

_manifold_cache: dict = {}
_MANIFOLD_TTL = 900   # 15 minutes
_MANIFOLD_API = "https://api.manifold.markets/v0"

def source_manifold(question: str) -> Optional[float]:
    """
    Search Manifold Markets for a matching binary market.
    Manifold is a play-money prediction market with diverse community coverage.

    Endpoint: GET /search-markets?term=...&filter=open&contractType=BINARY&limit=10
    Jaccard >= 0.40.
    Cached per question for 15 minutes.
    """
    now = time.time()
    cache_entry = _manifold_cache.get(question)
    if cache_entry and cache_entry["ts"] + _MANIFOLD_TTL > now:
        return cache_entry["prob"]

    try:
        resp = requests.get(
            f"{_MANIFOLD_API}/search-markets",
            params={
                "term":         question[:80],
                "contractType": "BINARY",
                "limit":        10,
            },
            headers={"User-Agent": "LeonardoBot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            _manifold_cache[question] = {"ts": now, "prob": None}
            return None
        markets = resp.json()
        if not isinstance(markets, list):
            markets = markets.get("items", markets.get("data", []))
    except Exception:
        _manifold_cache[question] = {"ts": now, "prob": None}
        return None

    best_score = 0.0
    best_prob: Optional[float] = None

    for m in markets:
        title = m.get("question") or m.get("title") or ""
        score = _jaccard(question, title)
        if score <= best_score:
            continue

        # Skip resolved markets — we'd be using outcome, not signal
        if m.get("isResolved") or m.get("resolution"):
            continue
        # Manifold probability field: probability (0-1)
        p = m.get("probability")
        if p is not None:
            try:
                prob = float(p)
                if 0.01 <= prob <= 0.99:
                    best_score = score
                    best_prob  = prob
            except (TypeError, ValueError):
                continue

    if best_score >= 0.40 and best_prob is not None:
        _manifold_cache[question] = {"ts": now, "prob": best_prob}
        return best_prob

    _manifold_cache[question] = {"ts": now, "prob": None}
    return None


# ── Volume Momentum ───────────────────────────────────────────────────────────

_CYCLE_DATA_FILE = os.path.join(_DIR, "polymarket_24h.json")

def source_volume_momentum(market: dict) -> Optional[float]:
    """
    Detect unusual volume momentum from prior cycle snapshots.

    Reads polymarket_24h.json (written by polymarket_monitor.py every cycle).
    Looks for markets where:
      - volume has spiked > 3× the prior 24h baseline
      - smart money accumulating one side (best_ask dropping while volume rises)

    Returns a probability nudge:
      - high YES accumulation → nudge toward yes_mid + 0.05
      - high NO accumulation  → nudge toward yes_mid - 0.05
      - no signal             → None
    """
    question = market.get("question") or market.get("title") or ""
    if not question:
        return None

    try:
        if not os.path.exists(_CYCLE_DATA_FILE):
            return None
        with open(_CYCLE_DATA_FILE) as f:
            history = json.load(f)
    except Exception:
        return None

    # polymarket_24h.json format: list of cycle snapshots, each with "markets"
    if not isinstance(history, list) or len(history) < 2:
        # Also try dict format: {"cycles": [...]}
        if isinstance(history, dict):
            history = history.get("cycles", [])
        if len(history) < 2:
            return None

    # Find this question across the last 6 cycles
    matches: list[dict] = []
    for cycle in history[-6:]:
        cycle_markets = cycle.get("markets") or cycle.get("opportunities") or []
        for cm in cycle_markets:
            q = cm.get("question") or cm.get("title") or ""
            if _jaccard(question, q) >= 0.80:
                matches.append(cm)
                break

    if len(matches) < 2:
        return None

    # Compare volume from oldest to newest snapshot
    try:
        v_old = float(matches[0].get("volume24hr") or matches[0].get("volume_usd") or 0)
        v_new = float(matches[-1].get("volume24hr") or matches[-1].get("volume_usd") or 0)
        p_old = float(matches[0].get("market_price") or matches[0].get("yes_mid") or 0.5)
        p_new = float(matches[-1].get("market_price") or matches[-1].get("yes_mid") or 0.5)
    except (TypeError, ValueError):
        return None

    if v_old <= 0:
        return None

    vol_ratio = v_new / v_old
    price_drift = p_new - p_old

    # Only act on significant volume spikes with directional price movement
    if vol_ratio >= 3.0 and abs(price_drift) >= 0.03:
        # Strong momentum signal: price is moving WITH high volume
        # Extrapolate slightly beyond current price
        nudge_factor = min(vol_ratio / 10.0, 0.10)
        if price_drift > 0:
            return min(p_new + nudge_factor, 0.95)
        else:
            return max(p_new - nudge_factor, 0.05)

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  PROBABILITY AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

_DEFAULT_WEIGHTS: dict[str, float] = {
    "fedwatch":        0.50,
    "weather":         0.45,
    "kalshi":          0.45,   # primary cross-reference for all categories
    "metaculus":       0.30,
    "manifold":        0.25,
    "volume_momentum": 0.20,
    "orderbook":       0.10,   # reduced — only weak signal
}


def aggregate_probability(
    sources:  dict[str, float],
    weights:  Optional[dict[str, float]] = None,
) -> dict:
    """
    Combine multiple probability estimates using weighted average.

    Default weights: fedwatch=0.50, weather=0.45, kalshi=0.45, metaculus=0.30, manifold=0.25, volume_momentum=0.20, orderbook=0.10.
    Normalises weights to sum to 1.0 over available sources only.

    Confidence:
      HIGH   — 3+ sources agree within 10%
      MEDIUM — 2 sources available, agree within 10%, OR 3+ with wider spread
      LOW    — single source or all disagree > 20%
    """
    if not sources:
        return {"combined_prob": None, "sources_used": [], "source_probs": {}, "confidence": "NONE"}

    w = weights or _DEFAULT_WEIGHTS
    total_w = sum(w.get(src, 0.10) for src in sources)
    if total_w == 0:
        total_w = 1.0

    combined = sum(
        sources[src] * w.get(src, 0.10) / total_w
        for src in sources
    )
    combined = round(max(0.01, min(0.99, combined)), 4)

    vals = sorted(sources.values())
    n    = len(vals)
    spread = vals[-1] - vals[0] if n > 1 else 0.0

    if n >= 3 and spread <= 0.10:
        conf = "HIGH"
    elif n >= 2 and spread <= 0.10:
        conf = "MEDIUM"
    elif n >= 2 and spread <= 0.20:
        conf = "MEDIUM"
    elif n == 1:
        conf = "LOW"
    else:
        conf = "LOW"

    return {
        "combined_prob": combined,
        "sources_used":  list(sources.keys()),
        "source_probs":  {k: round(v, 4) for k, v in sources.items()},
        "confidence":    conf,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

_CLASS_KEYWORDS: dict[str, list[str]] = {
    "fed_rate": [
        "fed", "fomc", "federal reserve", "rate cut", "rate hike",
        "basis points", "interest rate", "funds rate", "bps",
    ],
    "weather": [
        "temperature", "rain", "snow", "storm", "degrees",
        "celsius", "fahrenheit", "precipitation", "humidity",
        "wind", "forecast", "weather", "max temp", "min temp",
    ],
    "election": [
        "election", "president", "senator", "governor", "congress",
        "vote", "ballot", "primary", "candidate", "poll",
    ],
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
        "defi", "nft", "solana", "sol", "coinbase",
    ],
    "sports": [
        "nfl", "nba", "nhl", "mlb", "premier league", "bundesliga",
        "la liga", "serie a", "ligue 1", "world cup", "champions league",
        "win the", "qualify", "championship", "super bowl", "stanley cup",
    ],
}


def classify_market(question: str) -> str:
    """
    Classify Polymarket question into a category to route probability sources.

    Categories:
      fed_rate  → fedwatch (primary), metaculus (secondary)
      weather   → weather models (primary)
      election  → metaculus (primary)
      crypto    → metaculus (secondary)
      sports    → metaculus (secondary)
      other     → metaculus only
    """
    q = question.lower()
    for category, keywords in _CLASS_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return "other"


# ══════════════════════════════════════════════════════════════════════════════
#  RISK FLAGS
# ══════════════════════════════════════════════════════════════════════════════

def risk_flags(market: dict, analysis: dict) -> list[str]:
    """
    Check for known risks. Returns list of warning strings for any that apply.

    RESOLVES_TODAY   — resolves within 12 hours (price impact may not correct)
    LOW_VOLUME       — volume < $5,000 (tiny b, severe price impact)
    WIDE_SPREAD      — bid-ask spread > 8% (market maker uncertain)
    NEAR_CERTAINTY   — market price > 0.92 or < 0.08 (tail risk)
    SINGLE_SOURCE    — only one probability source available
    LARGE_IMPACT     — price impact consumes > 15% of edge
    """
    flags = []

    hours = analysis.get("hours_to_resolution")
    if hours is not None and 0 < hours < 12:
        flags.append("RESOLVES_TODAY")

    volume = float(market.get("volume24hr") or market.get("volume") or 0)
    if volume < 5_000:
        flags.append("LOW_VOLUME")

    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    if bid is not None and ask is not None:
        try:
            spread = float(ask) - float(bid)
            if spread > 0.08:
                flags.append("WIDE_SPREAD")
        except (TypeError, ValueError):
            pass

    mp = analysis.get("market_price", 0.5)
    if mp > 0.92 or mp < 0.08:
        flags.append("NEAR_CERTAINTY")

    if len(analysis.get("sources", {}).get("sources_used", [])) <= 1:
        flags.append("SINGLE_SOURCE")

    edge         = abs(analysis.get("true_prob", 0.5) - analysis.get("market_price", 0.5))
    price_impact = abs(analysis.get("price_impact_pct", 0.0)) / 100.0
    if edge > 0 and price_impact / edge > 0.15:
        flags.append("LARGE_IMPACT")

    return flags


# ══════════════════════════════════════════════════════════════════════════════
#  FULL LMSR MARKET ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def scan_market_lmsr(market: dict) -> Optional[dict]:
    """
    Full LMSR analysis of a single Polymarket market.

    Steps:
      1.  Extract question, yes_mid, volume, bestBid, bestAsk
      2.  Estimate b using estimate_b_from_market(volume, spread*volume)
      3.  Infer q vector from current price and b
      4.  Classify market → determine probability sources
      5.  Gather external probability estimates
      6.  Aggregate with aggregate_probability()
      7.  Skip if confidence == "LOW"
      8.  Calculate spot EV
      9.  Skip if spot EV < 5%
      10. Simulate optimal entry with price impact
      11. Calculate realized EV after price impact
      12. Skip if realized EV < 3%
      13. Return full analysis dict

    Returns None if any threshold fails.
    """
    question = market.get("question") or market.get("title") or ""

    # ── Step 1: Extract prices ─────────────────────────────────────────────
    prices_raw = market.get("outcomePrices")
    best_bid   = market.get("bestBid")
    best_ask   = market.get("bestAsk")

    yes_price = None
    if prices_raw:
        try:
            prices    = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0])
        except Exception:
            pass

    bid_f = ask_f = None
    try:
        bid_f = float(best_bid) if best_bid is not None else None
        ask_f = float(best_ask) if best_ask is not None else None
    except (TypeError, ValueError):
        pass

    if bid_f is not None and ask_f is not None and ask_f > bid_f:
        yes_mid = (bid_f + ask_f) / 2.0
        spread  = ask_f - bid_f
    elif yes_price is not None:
        yes_mid = yes_price
        spread  = 0.02   # assume typical spread
    else:
        return None

    yes_mid = max(0.02, min(0.98, yes_mid))

    volume = float(market.get("volume24hr") or market.get("volume") or 0)

    # ── Step 2: Estimate b ──────────────────────────────────────────────────
    liquidity_depth = spread * volume
    b = estimate_b_from_market(volume, liquidity_depth)

    # ── Step 3: Infer q ─────────────────────────────────────────────────────
    q_vector = infer_q_from_price(yes_mid, b)

    # ── Step 4: Classify ────────────────────────────────────────────────────
    category = classify_market(question)

    # ── Step 5: Gather probabilities ─────────────────────────────────────────
    source_probs: dict[str, float] = {}

    if category == "fed_rate":
        fw = source_fedwatch()
        if fw:
            q_lower = question.lower()
            if "cut" in q_lower and "hike" not in q_lower:
                source_probs["fedwatch"] = fw["cut_prob"]
            elif "hike" in q_lower or "raise" in q_lower:
                source_probs["fedwatch"] = fw["hike_prob"]
            elif "hold" in q_lower or "unchanged" in q_lower or "no change" in q_lower:
                source_probs["fedwatch"] = fw["hold_prob"]
            else:
                source_probs["fedwatch"] = fw["hold_prob"] + fw["hike_prob"]

    elif category == "weather":
        if _ensure_weather_imports():
            city = _extract_city_fn(question)
            if city:
                coords = _geocode_fn(city)
                if coords:
                    lat, lon = coords
                    weather_result = source_weather(city, lat, lon, question)
                    if weather_result:
                        source_probs["weather"] = weather_result["prob"]

    # Kalshi: primary cross-reference for ALL categories
    kalshi_p = source_kalshi(question)
    if kalshi_p is not None:
        source_probs["kalshi"] = kalshi_p

    # Metaculus: secondary for all categories
    meta = source_metaculus(question)
    if meta is not None:
        source_probs["metaculus"] = meta

    # Manifold: tertiary for all categories
    manifold_p = source_manifold(question)
    if manifold_p is not None:
        source_probs["manifold"] = manifold_p

    # Volume momentum: only if cycle data available
    vol_mom_p = source_volume_momentum(market)
    if vol_mom_p is not None:
        source_probs["volume_momentum"] = vol_mom_p

    # Always add orderbook as weak signal
    source_probs["orderbook"] = yes_mid

    # Require at least one external source (not just orderbook)
    external = {k: v for k, v in source_probs.items() if k != "orderbook"}
    if not external:
        return None  # No external signal — skip

    # If single external source, require it disagrees with orderbook by >= 8%
    # to avoid acting on noise
    if len(external) == 1:
        ext_val = list(external.values())[0]
        if abs(ext_val - yes_mid) < 0.08:
            return None

    # ── Step 6: Aggregate ────────────────────────────────────────────────────
    agg = aggregate_probability(source_probs)
    true_prob  = agg["combined_prob"]
    confidence = agg["confidence"]

    # ── Step 7: Skip only if no real probability estimate
    if true_prob is None:
        return None
    # With LOW confidence + single source, require stronger EV to compensate
    external_count = len([k for k in agg["sources_used"] if k != "orderbook"])
    _min_spot_ev = 0.05 if (confidence != "LOW" or external_count >= 2) else 0.10

    # ── Step 8: Spot EV ──────────────────────────────────────────────────────
    # Determine trade direction
    if true_prob > yes_mid:
        trade_side  = "YES"
        market_p    = yes_mid
        true_p      = true_prob
    else:
        trade_side  = "NO"
        market_p    = 1.0 - yes_mid   # NO price
        true_p      = 1.0 - true_prob  # true probability of NO

    spot_ev = expected_value(market_p, true_p)

    # ── Step 9: Spot EV threshold ────────────────────────────────────────────
    if spot_ev < _min_spot_ev:
        return None

    # ── Step 10: Price-impact simulation ────────────────────────────────────
    # For YES trades: use q_vector directly
    # For NO trades: infer q from NO perspective
    if trade_side == "YES":
        q_for_sim = q_vector
        sim_outcome = 0
    else:
        q_for_sim   = infer_q_from_price(market_p, b)
        sim_outcome = 0  # outcome 0 = the side we're buying

    bankroll = float(os.environ.get("STARTING_BANKROLL", "100.0"))
    sizing   = optimal_entry_size(
        market_price   = market_p,
        true_prob      = true_p,
        b              = b,
        bankroll       = bankroll,
        kelly_fraction = 0.25,
        fill_threshold = 0.80,
    )

    # Simulate the recommended size
    rec_shares = sizing["recommended_shares"]
    if rec_shares > 0:
        sim = simulate_buy(q_for_sim, rec_shares, b, outcome=sim_outcome, steps=50)
        avg_fill     = sim["average_fill"]
        price_impact = sim["price_impact"]
    else:
        avg_fill     = market_p
        price_impact = 0.0

    # ── Step 11: Realized EV ─────────────────────────────────────────────────
    realized_ev = expected_value(market_p, true_p, cost_per_share=avg_fill)

    # ── Step 12: Realized EV threshold ──────────────────────────────────────
    if realized_ev < 0.03:
        return None

    expected_profit = realized_ev * sizing["recommended_shares"]
    price_impact_pct = (price_impact / market_p * 100.0) if market_p > 0 else 0.0

    # Convergence exit target (50% of the way to true_prob)
    convergence_target = market_p + (true_p - market_p) * 0.5

    # Hours to resolution (best effort from question text)
    hours_to_res = None
    if _ensure_weather_imports():
        target_date = _extract_date_fn(question)
        hours_to_res = _hours_to_resolution_fn(target_date)

    analysis = {
        "question":         question,
        "market_price":     round(market_p,       4),
        "true_prob":        round(true_p,          4),
        "confidence":       confidence,
        "sources":          agg,
        "spot_ev":          round(spot_ev,         4),
        "estimated_b":      round(b,               2),
        "q_vector":         [round(x, 4) for x in q_vector],
        "optimal_size_usd": round(sizing["recommended_size_usd"], 4),
        "expected_avg_fill":round(avg_fill,         4),
        "realized_ev":      round(realized_ev,      4),
        "expected_profit":  round(expected_profit,  4),
        "trade_side":       trade_side,
        "price_impact_pct": round(price_impact_pct, 4),
        "constraint_binding": sizing["constraint_binding"],
        "convergence_target": round(convergence_target, 4),
        "hours_to_resolution": round(hours_to_res, 1) if hours_to_res is not None else None,
        "category":         category,
        "risk_flags":       [],   # filled below
        "scanned_at":       datetime.now(timezone.utc).isoformat(),
    }

    analysis["risk_flags"] = risk_flags(market, analysis)

    return analysis
