#!/usr/bin/env python3
"""
analysis.py — BTTS and Over 2.5 goals analysis
================================================
Uses Poisson distribution to estimate BTTS and Over 2.5 probabilities
from team season-average goal rates. Returns picks with positive edge.

Usage:
    from analysis import analyse_fixture, analyse_all
    picks = analyse_all(fixtures)
"""

import math
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MIN_EDGE      = float(os.getenv("MIN_EDGE", "0.05"))
KELLY_FRAC    = 0.25
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "100.0"))


# ══════════════════════════════════════════════════════════════════════════════
#  POISSON HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for Poisson(lam)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _poisson_cdf(k_max: int, lam: float) -> float:
    """P(X <= k_max) for Poisson(lam)."""
    return sum(_poisson_pmf(k, lam) for k in range(k_max + 1))


def prob_over_2_5(home_xg: float, away_xg: float) -> float:
    """
    P(total goals >= 3) using independent Poisson for each team.
    = 1 - P(total goals <= 2)
    """
    total_lam = home_xg + away_xg
    return round(1.0 - _poisson_cdf(2, total_lam), 4)


def prob_btts(home_xg: float, away_xg: float) -> float:
    """
    P(both teams score >= 1).
    = P(home >= 1) × P(away >= 1)
    = (1 - P(home=0)) × (1 - P(away=0))
    """
    p_home_scores = 1.0 - _poisson_pmf(0, home_xg)
    p_away_scores = 1.0 - _poisson_pmf(0, away_xg)
    return round(p_home_scores * p_away_scores, 4)


# ══════════════════════════════════════════════════════════════════════════════
#  EDGE CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def _kelly_stake(our_prob: float, odds: float, bankroll: float) -> tuple[float, float]:
    """
    Returns (rec_stake_pct, paper_stake_usd) using fractional Kelly.
    """
    net_odds  = odds - 1.0
    if net_odds <= 0:
        return 0.0, 0.0
    full_kelly    = max(0.0, (net_odds * our_prob - (1 - our_prob)) / net_odds)
    rec_stake_pct = round(full_kelly * KELLY_FRAC * 100, 2)
    paper_stake   = round(bankroll * rec_stake_pct / 100, 2)
    return rec_stake_pct, paper_stake


def _edge(our_prob: float, odds: float) -> float:
    implied = 1.0 / odds
    return round((our_prob - implied) * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  FIXTURE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def analyse_fixture(
    fixture:   dict,
    home_stats: dict,
    away_stats: dict,
    bankroll:  float = STARTING_BANKROLL,
) -> list[dict]:
    """
    Given a fixture and home/away team stats, return a list of picks
    (BTTS and/or Over 2.5) with positive edge.

    Each pick dict matches the log_prediction() schema in tracker.py.
    """
    home = fixture["home"]
    away = fixture["away"]

    # Expected goals: (team's avg scored + opponent's avg conceded) / 2
    home_xg = (home_stats["goals_for_avg"] + away_stats["goals_against_avg"]) / 2
    away_xg = (away_stats["goals_for_avg"] + home_stats["goals_against_avg"]) / 2

    if home_xg <= 0 or away_xg <= 0:
        return []

    p_o25  = prob_over_2_5(home_xg, away_xg)
    p_btts = prob_btts(home_xg, away_xg)

    picks = []

    # ── Over 2.5 ────────────────────────────────────────────────────────────
    # Typical market odds for Over 2.5 when our prob > 55%
    # In a real system you'd fetch live odds from The Odds API.
    # Here we use a conservative flat estimate; update with real odds before use.
    o25_odds = 1.85  # placeholder — replace with live odds from The Odds API
    o25_edge = _edge(p_o25, o25_odds)
    if o25_edge > MIN_EDGE * 100:
        stake_pct, stake_usd = _kelly_stake(p_o25, o25_odds, bankroll)
        picks.append({
            "sport":                     "football",
            "league":                    fixture["league_name"],
            "match":                     f"{home} vs {away}",
            "market":                    "Over 2.5",
            "selection":                 "Over",
            "reasoning":                 (
                f"xG model: home_xg={home_xg:.2f} away_xg={away_xg:.2f} "
                f"→ P(O2.5)={p_o25:.2%}"
            ),
            "our_probability":           p_o25,
            "market_odds":               o25_odds,
            "implied_probability":       round(1.0 / o25_odds, 4),
            "edge_percent":              o25_edge,
            "kelly_fraction":            KELLY_FRAC,
            "recommended_stake_percent": stake_pct,
            "paper_stake_usd":           stake_usd,
            "fixture_id":                fixture.get("fixture_id"),
            "kickoff_utc":               fixture.get("kickoff_utc"),
        })

    # ── BTTS ────────────────────────────────────────────────────────────────
    btts_odds = 1.80  # placeholder
    btts_edge = _edge(p_btts, btts_odds)
    if btts_edge > MIN_EDGE * 100:
        stake_pct, stake_usd = _kelly_stake(p_btts, btts_odds, bankroll)
        picks.append({
            "sport":                     "football",
            "league":                    fixture["league_name"],
            "match":                     f"{home} vs {away}",
            "market":                    "BTTS",
            "selection":                 "Yes",
            "reasoning":                 (
                f"xG model: home_xg={home_xg:.2f} away_xg={away_xg:.2f} "
                f"→ P(BTTS)={p_btts:.2%}"
            ),
            "our_probability":           p_btts,
            "market_odds":               btts_odds,
            "implied_probability":       round(1.0 / btts_odds, 4),
            "edge_percent":              btts_edge,
            "kelly_fraction":            KELLY_FRAC,
            "recommended_stake_percent": stake_pct,
            "paper_stake_usd":           stake_usd,
            "fixture_id":                fixture.get("fixture_id"),
            "kickoff_utc":               fixture.get("kickoff_utc"),
        })

    return picks


def analyse_all(fixtures: list[dict], bankroll: float = STARTING_BANKROLL) -> list[dict]:
    """
    Analyse all fixtures. Fetches team stats for each and returns all picks.
    Imports data.py lazily to avoid circular deps.
    """
    from data import get_team_stats

    all_picks = []
    for fix in fixtures:
        home_stats = get_team_stats(fix["home_id"], fix["league_id"])
        away_stats = get_team_stats(fix["away_id"], fix["league_id"])
        picks      = analyse_fixture(fix, home_stats, away_stats, bankroll)
        all_picks.extend(picks)

    return all_picks


if __name__ == "__main__":
    # Quick smoke test with dummy data
    dummy_fix = {
        "fixture_id":  1,
        "league_name": "Test League",
        "home":        "Team A",
        "away":        "Team B",
        "home_id":     1,
        "away_id":     2,
        "league_id":   39,
        "kickoff_utc": "2026-03-14T15:00:00+00:00",
    }
    home_st = {"goals_for_avg": 1.8, "goals_against_avg": 1.1, "btts_pct": 0, "matches_played": 20}
    away_st = {"goals_for_avg": 1.5, "goals_against_avg": 1.3, "btts_pct": 0, "matches_played": 20}
    picks = analyse_fixture(dummy_fix, home_st, away_st)
    print(f"Picks found: {len(picks)}")
    for p in picks:
        print(f"  {p['match']} | {p['market']} {p['selection']} | edge={p['edge_percent']:+.2f}%")
