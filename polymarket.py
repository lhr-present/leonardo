#!/usr/bin/env python3
"""
polymarket.py — Polymarket CLOB scanner for Leonardo
======================================================
Extracted from ~/polymarket_bot/bot.py.
PAPER_MODE = True is hardcoded — this module never places real orders.

Usage:
    from polymarket import scan_and_report
    opportunities = scan_and_report()   # returns list of dicts
"""

import os
import sys
import json
import math
import urllib.parse
import requests
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  (PAPER_MODE locked True)
# ══════════════════════════════════════════════════════════════════════════════

PAPER_MODE      = True   # LOCKED — this module never sends real orders
MAX_TRADE_USDT  = 10.0
KELLY_FRACTION  = 0.25
MIN_EDGE        = float(os.getenv("MIN_EDGE", "0.05"))
BANKROLL        = float(os.getenv("STARTING_BANKROLL", "100.0"))
MAX_MARKETS     = 50

CLOB_HOST       = "https://clob.polymarket.com"
METACULUS_HOST  = "https://www.metaculus.com/api2"

PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY", "")
API_KEY        = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET     = os.getenv("POLYMARKET_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
CHAIN_ID       = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

_metaculus_cache: dict = {}

_STOPWORDS = {
    "a","an","the","is","are","was","were","will","be","been","being","have",
    "has","had","do","does","did","but","and","or","if","in","on","at","to",
    "for","of","with","by","from","as","into","about","than","that","this",
    "it","its","not","no","any","all","would","could","should","may","might",
    "can","between","during","before","after","above","below","over","under",
    "again","then","once","there","when","where","who","which","what","how",
    "why","more","most","other","some",
}


# ══════════════════════════════════════════════════════════════════════════════
#  CLIENT SETUP
# ══════════════════════════════════════════════════════════════════════════════

def build_client():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        print("[polymarket.py] py-clob-client not installed. Run: pip install py-clob-client")
        return None

    if not PRIVATE_KEY:
        print("[polymarket.py] POLYMARKET_PRIVATE_KEY not set in .env — skipping Polymarket.")
        return None

    try:
        client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY.removeprefix("0x"), chain_id=CHAIN_ID)
        if API_KEY and API_SECRET and API_PASSPHRASE:
            from py_clob_client.clob_types import ApiCreds
            client.set_api_creds(ApiCreds(
                api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE,
            ))
        return client
    except Exception as exc:
        print(f"[polymarket.py] Could not build Polymarket client: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  METACULUS
# ══════════════════════════════════════════════════════════════════════════════

def _keyword_similarity(a: str, b: str) -> float:
    def tokens(text: str) -> set:
        words = text.lower().split()
        return {w.strip("?.!,;:\"'()") for w in words} - _STOPWORDS
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def fetch_external_probability(question: str) -> Optional[float]:
    if question in _metaculus_cache:
        return _metaculus_cache[question]

    search_query = urllib.parse.quote(question[:80])
    url = f"{METACULUS_HOST}/questions/?search={search_query}&limit=5"

    try:
        resp = requests.get(url, timeout=8, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _metaculus_cache[question] = None
        return None

    results    = data.get("results", [])
    best_score = 0.0
    best_prob  = None

    for item in results:
        title      = item.get("title", "")
        similarity = _keyword_similarity(question, title)
        if similarity <= best_score:
            continue
        cp   = item.get("community_prediction") or {}
        full = cp.get("full") or {}
        q2   = full.get("q2") or cp.get("q2") or cp.get("prediction")
        if q2 is not None:
            best_score = similarity
            best_prob  = float(q2)

    if best_score >= 0.70 and best_prob is not None:
        _metaculus_cache[question] = best_prob
        return best_prob

    _metaculus_cache[question] = None
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER BOOK MODEL
# ══════════════════════════════════════════════════════════════════════════════

def _ob_model(order_book: dict, client=None, yes_token_id: str = "") -> Optional[float]:
    bids      = order_book.get("bids", [])
    asks_list = order_book.get("asks", [])
    if not bids or not asks_list:
        return None

    try:
        bid = float(bids[0]["price"])
        ask = float(asks_list[0]["price"])
    except (KeyError, ValueError, IndexError):
        return None

    spread     = ask - bid
    simple_mid = (bid + ask) / 2.0

    def depth_usd(levels: list) -> float:
        return sum(float(lv.get("price", 0)) * float(lv.get("size", 0)) for lv in levels[:5])

    bid_depth   = depth_usd(bids)
    ask_depth   = depth_usd(asks_list)
    total_depth = bid_depth + ask_depth

    if total_depth > 0:
        weighted_mid = (bid_depth * bid + ask_depth * ask) / total_depth
        obi          = (bid_depth - ask_depth) / total_depth
    else:
        weighted_mid = simple_mid
        obi          = 0.0

    obi_adj      = obi * (spread / 2.0)
    momentum_adj = 0.0

    if client and yes_token_id:
        try:
            last_str = client.get_last_trade_price(yes_token_id)
            if last_str is not None:
                momentum_adj = (float(last_str) - weighted_mid) * 0.30
        except Exception:
            pass

    estimate = (
        weighted_mid                    * 0.35
        + (weighted_mid + obi_adj)      * 0.30
        + (weighted_mid + momentum_adj) * 0.20
        + simple_mid                    * 0.15
    )

    uncertainty_weight = min(spread / 0.05, 1.0) * 0.08
    estimate           = estimate + (0.5 - estimate) * uncertainty_weight

    return max(0.01, min(0.99, estimate))


def mid_price(order_book: dict) -> Optional[float]:
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if not bids or not asks:
        return None
    try:
        return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2.0
    except (KeyError, ValueError, IndexError):
        return None


def kelly_size(edge: float, entry_price: float) -> float:
    if entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0
    full_kelly = max(0.0, edge / (1.0 - entry_price))
    dollars    = full_kelly * KELLY_FRACTION * BANKROLL
    return min(dollars, MAX_TRADE_USDT)


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN
# ══════════════════════════════════════════════════════════════════════════════

def scan_and_report() -> list[dict]:
    """
    Scan active Polymarket markets and return a list of opportunity dicts.
    Never places orders (PAPER_MODE = True hardcoded).
    Each dict: question, yes_mid, our_estimate, edge, trade_side, cost_usd, signal_source.
    """
    client = build_client()
    if client is None:
        return []

    opportunities = []

    try:
        resp    = client.get_markets()
        markets = resp.get("data", []) if isinstance(resp, dict) else (resp or [])
    except Exception as exc:
        print(f"[polymarket.py] Failed to fetch markets: {exc}")
        return []

    for market in markets[:MAX_MARKETS]:
        if not market.get("active", False) or market.get("closed", True):
            continue

        question  = market.get("question", "")
        tokens    = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome") == "YES"), None)
        no_token  = next((t for t in tokens if t.get("outcome") == "NO"),  None)

        if not yes_token or not no_token:
            continue

        yes_token_id = yes_token.get("token_id", "")
        no_token_id  = no_token.get("token_id", "")
        if not yes_token_id:
            continue

        try:
            ob = client.get_order_book(yes_token_id)
        except Exception:
            continue

        yes_mid = mid_price(ob)
        if yes_mid is None or yes_mid < 0.02 or yes_mid > 0.98:
            continue

        meta_p = fetch_external_probability(question)
        ob_p   = _ob_model(ob, client, yes_token_id)

        if meta_p is not None and ob_p is not None:
            p_estimate    = meta_p * 0.60 + ob_p * 0.40
            signal_source = "HYBRID"
        elif meta_p is not None:
            p_estimate    = meta_p
            signal_source = "METACULUS"
        elif ob_p is not None:
            p_estimate    = ob_p
            signal_source = "OB_ONLY"
        else:
            continue

        p_estimate = max(0.01, min(0.99, p_estimate))

        yes_edge = p_estimate - yes_mid
        no_edge  = yes_mid - p_estimate
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
            "question":      question,
            "yes_mid":       yes_mid,
            "our_estimate":  round(p_estimate, 4),
            "edge":          round(edge, 4),
            "trade_side":    trade_side,
            "limit_price":   limit,
            "cost_usd":      round(cost_usd, 2),
            "signal_source": signal_source,
            "scanned_at":    datetime.utcnow().isoformat() + "Z",
        })

    return opportunities


if __name__ == "__main__":
    print("Scanning Polymarket…")
    opps = scan_and_report()
    print(f"Found {len(opps)} opportunities.")
    for o in opps[:10]:
        print(f"  [{o['signal_source']}] edge={o['edge']:.1%} {o['trade_side']} ${o['cost_usd']:.2f} — {o['question'][:60]}")
