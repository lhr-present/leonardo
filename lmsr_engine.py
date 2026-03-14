#!/usr/bin/env python3
"""
lmsr_engine.py — LMSR (Logarithmic Market Scoring Rule) mathematical core
==========================================================================
Pure functions, no side effects, no I/O.
All LMSR math used by lmsr_scanner.py for Polymarket edge analysis.

Reference: Hanson (2003) "Combinatorial Information Market Design"
  C(q)  = b * ln( Σ e^(qi/b) )
  p_k   = e^(qk/b) / Σ e^(qi/b)   ← softmax, identical to neural net output
  Cost of trade = C(q_after) - C(q_before)
"""

import math
from typing import Optional

import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  CORE LMSR FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def cost_function(q: list[float], b: float) -> float:
    """
    C(q) = b * ln( Σ e^(qi/b) )
    Numerically stable via logsumexp (subtract max before exponentiation).
    """
    arr    = np.array(q, dtype=float)
    scaled = arr / b
    m      = np.max(scaled)
    return float(b * (m + np.log(np.sum(np.exp(scaled - m)))))


def marginal_price(q: list[float], b: float, outcome: int) -> float:
    """
    p_k(q) = e^(qk/b) / Σ e^(qi/b)   [softmax]
    Invariant: Σ_k p_k(q) = 1.0 exactly.
    This is identical to the neural-network output layer.
    """
    arr    = np.array(q, dtype=float)
    scaled = arr / b
    m      = np.max(scaled)
    exps   = np.exp(scaled - m)
    return float(exps[outcome] / np.sum(exps))


def cost_of_trade(
    q_before: list[float],
    q_after:  list[float],
    b:        float,
) -> float:
    """Cost to move market from q_before to q_after = C(q_after) - C(q_before)."""
    return cost_function(q_after, b) - cost_function(q_before, b)


def max_loss(b: float, n_outcomes: int = 2) -> float:
    """
    L_max = b * ln(n)
    Maximum subsidy the market maker provides.
    For b=100_000, n=2: L_max ≈ $69,315.
    Use this to infer b from known market parameters.
    """
    return b * math.log(n_outcomes)


# ══════════════════════════════════════════════════════════════════════════════
#  b ESTIMATION
# ══════════════════════════════════════════════════════════════════════════════

def estimate_b_from_market(volume_usd: float, liquidity_depth: float) -> float:
    """
    Estimate the liquidity parameter b from observable Gamma API data.
    Polymarket does not publish b directly.

    Method: price impact of a $1 trade ≈ 1/b for mid-range probabilities.
    If a $X trade moves price by Y cents: b ≈ X / Y

    Primary estimate: b ≈ volume_usd * 0.1 (empirical — larger markets → larger b).
    Refinement: if liquidity_depth > 0, use b = max(b, liquidity_depth * 2.0).

    Returns estimated b clamped to [100, 10_000_000].
    """
    b = max(100.0, volume_usd * 0.1)
    if liquidity_depth > 0:
        b = max(b, liquidity_depth * 2.0)
    return min(b, 10_000_000.0)


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE IMPACT SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def simulate_buy(
    q_initial: list[float],
    shares:    float,
    b:         float,
    outcome:   int = 0,
    steps:     int = 50,
) -> dict:
    """
    Simulate buying `shares` of `outcome` in LMSR market.
    Uses `steps` incremental steps to model continuous price impact.

    Each step:
      1. Calculate instantaneous cost of buying shares/steps
      2. Update q vector
      3. Accumulate total cost

    Returns:
      average_fill   — actual avg cost per share paid
      final_price    — marginal price after full trade
      total_cost     — total USDC spent
      price_impact   — final_price - initial_price
      realized_edge  — 0.0  (caller updates: true_prob - average_fill)
      slippage_pct   — (average_fill - initial_price) / initial_price * 100
    """
    q           = list(q_initial)
    step_shares = shares / max(steps, 1)
    total_cost  = 0.0
    initial_p   = marginal_price(q, b, outcome)

    for _ in range(steps):
        q_after         = list(q)
        q_after[outcome] += step_shares
        total_cost      += cost_of_trade(q, q_after, b)
        q                = q_after

    final_p      = marginal_price(q, b, outcome)
    average_fill = total_cost / shares if shares > 0 else initial_p
    price_impact = final_p - initial_p
    slippage_pct = (
        (average_fill - initial_p) / initial_p * 100.0
        if initial_p > 0 else 0.0
    )

    return {
        "average_fill":  round(average_fill, 6),
        "final_price":   round(final_p,      6),
        "total_cost":    round(total_cost,   6),
        "price_impact":  round(price_impact, 6),
        "realized_edge": 0.0,   # caller: update with  true_prob - average_fill
        "slippage_pct":  round(slippage_pct, 4),
    }


def optimal_entry_size(
    market_price:   float,
    true_prob:      float,
    b:              float,
    bankroll:       float,
    kelly_fraction: float = 0.25,
    fill_threshold: float = 0.80,
) -> dict:
    """
    Determine optimal entry size combining Kelly criterion and price-impact limit.

    1. Kelly criterion
       edge       = true_prob - market_price
       odds       = (1 - market_price) / market_price
       f*         = edge / odds
       kelly_size = f* * kelly_fraction * bankroll

    2. Price-impact limit
       Stop accumulating when average_fill > true_prob * fill_threshold.
       Binary search over share count to find the binding point.

    3. Recommended = min(kelly_size, impact_limited_size).
    """
    edge = true_prob - market_price

    _zero = {
        "kelly_size_usd":           0.0,
        "impact_limited_size_usd":  0.0,
        "recommended_size_usd":     0.0,
        "recommended_shares":       0.0,
        "expected_avg_fill":        market_price,
        "expected_edge_per_share":  0.0,
        "expected_total_edge":      0.0,
        "constraint_binding":       "kelly",
    }

    if edge <= 0 or market_price <= 0 or market_price >= 1:
        return _zero

    odds      = (1.0 - market_price) / market_price
    full_k    = edge / odds
    kelly_usd = full_k * kelly_fraction * bankroll

    q_init      = infer_q_from_price(market_price, b)
    fill_limit  = true_prob * fill_threshold

    # Binary search: find max shares such that average_fill ≤ fill_limit
    lo = 0.0
    hi = max(kelly_usd / max(market_price, 1e-6) * 20.0, 1.0)

    for _ in range(40):
        mid = (lo + hi) / 2.0
        if mid <= 0:
            break
        sim = simulate_buy(q_init, mid, b, outcome=0, steps=30)
        if sim["average_fill"] <= fill_limit:
            lo = mid
        else:
            hi = mid

    impact_shares = lo
    if impact_shares > 1e-9:
        impact_sim = simulate_buy(q_init, impact_shares, b, outcome=0, steps=50)
        impact_usd = impact_sim["total_cost"]
        avg_fill   = impact_sim["average_fill"]
    else:
        impact_usd = 0.0
        avg_fill   = market_price

    recommended_usd    = min(kelly_usd, impact_usd)
    constraint_binding = "kelly" if kelly_usd <= impact_usd else "impact"

    rec_shares     = recommended_usd / avg_fill if avg_fill > 0 else 0.0
    edge_per_share = true_prob - avg_fill
    total_edge     = edge_per_share * rec_shares

    return {
        "kelly_size_usd":           round(kelly_usd,        4),
        "impact_limited_size_usd":  round(impact_usd,       4),
        "recommended_size_usd":     round(recommended_usd,  4),
        "recommended_shares":       round(rec_shares,       4),
        "expected_avg_fill":        round(avg_fill,         6),
        "expected_edge_per_share":  round(edge_per_share,   6),
        "expected_total_edge":      round(total_edge,       4),
        "constraint_binding":       constraint_binding,
    }


def expected_value(
    market_price:   float,
    true_prob:      float,
    cost_per_share: Optional[float] = None,
) -> float:
    """
    EV of buying YES when true probability = true_prob.

    Spot EV (no price impact):
      EV = true_prob * (1 - market_price) - (1 - true_prob) * market_price
         = true_prob - market_price

    Realized EV (with price impact):
      EV = true_prob - cost_per_share

    Returns realized EV if cost_per_share provided, else spot EV.
    """
    if cost_per_share is not None:
        return true_prob - cost_per_share
    return true_prob - market_price


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET STATE INFERENCE
# ══════════════════════════════════════════════════════════════════════════════

def infer_q_from_price(price: float, b: float) -> list[float]:
    """
    Given observed YES price p and liquidity b, infer q = [q_yes, q_no].

    From p = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))
    Set q_no = 0 (reference — only relative differences matter).
    Solve: q_yes = b * ln(p / (1 - p))   ← log-odds times b

    Verification: marginal_price([q_yes, 0.0], b, 0) ≈ price.
    """
    p     = max(1e-6, min(1.0 - 1e-6, price))
    q_yes = b * math.log(p / (1.0 - p))
    return [q_yes, 0.0]


def convergence_profit(
    entry_price: float,
    exit_price:  float,
    shares:      float,
) -> dict:
    """
    Profit from market converging toward true probability WITHOUT waiting for resolution.

    Example: bought YES at 0.38, market moves to 0.49 — sell and lock in profit.

    annualized_if_days assumes a 1-day holding period.
    Caller: divide annualized by actual holding days if known.

    Returns:
      profit_usd        — (exit - entry) * shares
      return_pct        — return as % of cost basis
      annualized_if_days — annualized return assuming 1-day hold
    """
    profit_usd = (exit_price - entry_price) * shares
    return_pct = (
        (exit_price - entry_price) / entry_price * 100.0
        if entry_price > 0 else 0.0
    )
    annualized = return_pct * 365.0   # scale by days; default 1-day assumption

    return {
        "profit_usd":         round(profit_usd,  4),
        "return_pct":         round(return_pct,  4),
        "annualized_if_days": round(annualized,  2),
    }
