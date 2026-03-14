#!/usr/bin/env python3
"""
data.py — API-Football data fetcher
=====================================
Fetches today's fixtures and team statistics for configured leagues.
All output is plain dicts — no side effects, no file writes.

Usage:
    from data import get_todays_fixtures, get_team_stats
    fixtures = get_todays_fixtures()
"""

import os
import json
import requests
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

API_KEY  = os.getenv("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"

# League IDs to monitor
_raw_leagues = os.getenv("LEAGUES", "39,135,140,78,61,203,88")
LEAGUES = [int(x.strip()) for x in _raw_leagues.split(",") if x.strip()]

CURRENT_SEASON = 2025  # update each year


def _headers() -> dict:
    return {"x-apisports-key": API_KEY}


def _get(endpoint: str, params: dict) -> Optional[dict]:
    """Make a GET request. Returns parsed JSON or None on error."""
    if not API_KEY or API_KEY == "YOUR_API_FOOTBALL_KEY_HERE":
        print("[data.py] API_FOOTBALL_KEY not set — skipping API call.")
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/{endpoint}",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[data.py] API error ({endpoint}): {exc}")
        return None


def get_todays_fixtures() -> list[dict]:
    """
    Return a list of today's fixtures across all configured leagues.
    Each fixture dict contains: fixture_id, home, away, league, kickoff_utc,
    home_goals_avg, away_goals_avg, league_id.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fixtures = []

    for league_id in LEAGUES:
        data = _get("fixtures", {"league": league_id, "season": CURRENT_SEASON, "date": today})
        if not data:
            continue
        for item in data.get("response", []):
            fix    = item.get("fixture", {})
            teams  = item.get("teams", {})
            league = item.get("league", {})
            goals  = item.get("goals", {})
            fixtures.append({
                "fixture_id":  fix.get("id"),
                "kickoff_utc": fix.get("date"),
                "status":      fix.get("status", {}).get("short", "NS"),
                "league_id":   league_id,
                "league_name": league.get("name", ""),
                "home":        teams.get("home", {}).get("name", ""),
                "home_id":     teams.get("home", {}).get("id"),
                "away":        teams.get("away", {}).get("name", ""),
                "away_id":     teams.get("away", {}).get("id"),
                "home_goals":  goals.get("home"),
                "away_goals":  goals.get("away"),
            })

    return fixtures


def get_team_stats(team_id: int, league_id: int) -> dict:
    """
    Return a team's season stats dict.
    Keys: goals_for_avg, goals_against_avg, btts_pct, matches_played.
    Returns zeros if unavailable.
    """
    defaults = {
        "goals_for_avg":     0.0,
        "goals_against_avg": 0.0,
        "btts_pct":          0.0,
        "matches_played":    0,
    }

    data = _get("teams/statistics", {
        "team":   team_id,
        "league": league_id,
        "season": CURRENT_SEASON,
    })
    if not data:
        return defaults

    stats = data.get("response", {})
    goals = stats.get("goals", {})
    fix   = stats.get("fixtures", {})

    played = fix.get("played", {}).get("total", 0) or 0
    if not played:
        return defaults

    gf_total = goals.get("for", {}).get("total", {}).get("total", 0) or 0
    ga_total = goals.get("against", {}).get("total", {}).get("total", 0) or 0

    # BTTS: both teams scored in the same match.
    # API-Football doesn't expose this directly, so we approximate from
    # home/away split: if avg GA > 0 and avg GF > 0 in at least 40% of matches.
    gf_avg = round(gf_total / played, 3)
    ga_avg = round(ga_total / played, 3)

    # Simple BTTS estimate: P(home scores) × P(away scores), Poisson-based
    # (full calculation done in analysis.py)
    return {
        "goals_for_avg":     gf_avg,
        "goals_against_avg": ga_avg,
        "btts_pct":          0.0,   # computed in analysis.py
        "matches_played":    played,
    }


def fetch_final_score(fixture_id: int) -> Optional[dict]:
    """
    Fetch the final score for a completed fixture.
    Returns dict with keys: status, home_goals, away_goals, home, away
    or None if not yet finished or not found.
    """
    data = _get("fixtures", {"id": fixture_id})
    if not data:
        return None
    items = data.get("response", [])
    if not items:
        return None
    item   = items[0]
    fix    = item.get("fixture", {})
    teams  = item.get("teams", {})
    goals  = item.get("goals", {})
    status = fix.get("status", {}).get("short", "")
    if status != "FT":
        return None
    return {
        "status":     status,
        "home":       teams.get("home", {}).get("name", ""),
        "away":       teams.get("away", {}).get("name", ""),
        "home_goals": goals.get("home"),
        "away_goals": goals.get("away"),
    }


def search_fixture_by_teams(home_team: str, away_team: str, date_str: str) -> Optional[dict]:
    """
    Search for a fixture by approximate team name and date (YYYY-MM-DD).
    Tries the home team name first, scans results for a match.
    Returns same format as fetch_final_score() or None.
    """
    # Search fixtures on that date across all configured leagues
    for league_id in LEAGUES:
        data = _get("fixtures", {
            "league":  league_id,
            "season":  CURRENT_SEASON,
            "date":    date_str,
        })
        if not data:
            continue
        for item in data.get("response", []):
            fix    = item.get("fixture", {})
            teams  = item.get("teams", {})
            goals  = item.get("goals", {})
            h_name = teams.get("home", {}).get("name", "").lower()
            a_name = teams.get("away", {}).get("name", "").lower()
            # Fuzzy match: check if key words from search terms appear in names
            h_words = {w for w in home_team.lower().split() if len(w) > 3}
            a_words = {w for w in away_team.lower().split() if len(w) > 3}
            if (any(w in h_name for w in h_words) and
                    any(w in a_name for w in a_words)):
                status = fix.get("status", {}).get("short", "")
                if status != "FT":
                    return None   # found but not finished
                return {
                    "status":      status,
                    "home":        teams.get("home", {}).get("name", ""),
                    "away":        teams.get("away", {}).get("name", ""),
                    "home_goals":  goals.get("home"),
                    "away_goals":  goals.get("away"),
                    "fixture_id":  fix.get("id"),
                }
    return None


if __name__ == "__main__":
    print("Fetching today's fixtures…")
    fx = get_todays_fixtures()
    print(f"Found {len(fx)} fixtures today.")
    for f in fx[:5]:
        print(f"  [{f['league_name']}] {f['home']} vs {f['away']}  @  {f['kickoff_utc']}")
