#!/usr/bin/env python3
"""
weather_edge.py — Polymarket weather market edge finder (v2)
=============================================================
Multi-source forecast consensus (Open-Meteo + Tomorrow.io + Met.no + NOAA),
timing filter (48h max), history tracking, and monitor mode.

Usage:
    python3 weather_edge.py                  # one-shot scan
    python3 weather_edge.py --settle         # settle resolved markets
    python3 weather_edge.py --monitor        # continuous 30-min monitor

Output:
    ~/leonardo/weather_edges.json
    ~/leonardo/weather_history.json
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

try:
    from x_poster import post_to_x, format_weather_tweet
    _X_AVAILABLE = True
except ImportError:
    _X_AVAILABLE = False

# ── Load environment ──────────────────────────────────────────────────────────
_DOTENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_DOTENV)

# ── API endpoints ─────────────────────────────────────────────────────────────
GAMMA_API      = "https://gamma-api.polymarket.com"
NOAA_API       = "https://api.weather.gov"
OPENMETEO_API  = "https://api.open-meteo.com/v1/forecast"
GEOCODING_API  = "https://geocoding-api.open-meteo.com/v1/search"
TOMORROWIO_API = "https://api.tomorrow.io/v4/weather/forecast"
METNO_API      = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

# ── File paths ────────────────────────────────────────────────────────────────
_DIR         = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE  = os.path.join(_DIR, "weather_edges.json")
HISTORY_FILE = os.path.join(_DIR, "weather_history.json")
LOG_FILE     = os.path.join(_DIR, "leonardo.log")

# ── Config ────────────────────────────────────────────────────────────────────
EDGE_THRESHOLD   = 0.08   # flag if gap > 8%
MAX_HOURS_AHEAD  = 48     # skip markets resolving more than 48h away
MONITOR_INTERVAL = 1800   # 30 minutes in seconds

TOMORROW_KEY = os.environ.get("TOMORROW_API_KEY", "")

# ── Headers ───────────────────────────────────────────────────────────────────
NOAA_HEADERS  = {
    "User-Agent": "LeonardoWeatherEdge/1.0 (research; contact@example.com)",
    "Accept":     "application/geo+json",
}
METNO_HEADERS = {"User-Agent": "LeonardoBot/1.0"}

# ── US city → (lat, lon) ──────────────────────────────────────────────────────
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york":        (40.7128, -74.0060),
    "nyc":             (40.7128, -74.0060),
    "los angeles":     (34.0522, -118.2437),
    "la":              (34.0522, -118.2437),
    "chicago":         (41.8781, -87.6298),
    "houston":         (29.7604, -95.3698),
    "phoenix":         (33.4484, -112.0740),
    "philadelphia":    (39.9526, -75.1652),
    "san antonio":     (29.4241, -98.4936),
    "san diego":       (32.7157, -117.1611),
    "dallas":          (32.7767, -96.7970),
    "san jose":        (37.3382, -121.8863),
    "austin":          (30.2672, -97.7431),
    "jacksonville":    (30.3322, -81.6557),
    "san francisco":   (37.7749, -122.4194),
    "columbus":        (39.9612, -82.9988),
    "charlotte":       (35.2271, -80.8431),
    "indianapolis":    (39.7684, -86.1581),
    "seattle":         (47.6062, -122.3321),
    "denver":          (39.7392, -104.9903),
    "washington":      (38.9072, -77.0369),
    "washington dc":   (38.9072, -77.0369),
    "nashville":       (36.1627, -86.7816),
    "oklahoma city":   (35.4676, -97.5164),
    "el paso":         (31.7619, -106.4850),
    "boston":          (42.3601, -71.0589),
    "portland":        (45.5051, -122.6750),
    "las vegas":       (36.1699, -115.1398),
    "miami":           (25.7617, -80.1918),
    "atlanta":         (33.7490, -84.3880),
    "minneapolis":     (44.9778, -93.2650),
    "new orleans":     (29.9511, -90.0715),
    "tampa":           (27.9506, -82.4572),
    "orlando":         (28.5383, -81.3792),
    "detroit":         (42.3314, -83.0458),
    "memphis":         (35.1495, -90.0490),
    "louisville":      (38.2527, -85.7585),
    "baltimore":       (39.2904, -76.6122),
    "raleigh":         (35.7796, -78.6382),
    "kansas city":     (39.0997, -94.5786),
    "virginia beach":  (36.8529, -75.9780),
    "omaha":           (41.2565, -95.9345),
    "colorado springs": (38.8339, -104.8214),
    "tulsa":           (36.1540, -95.9928),
    "st. louis":       (38.6270, -90.1994),
    "st louis":        (38.6270, -90.1994),
    "pittsburgh":      (40.4406, -79.9959),
    "anchorage":       (61.2181, -149.9003),
    "honolulu":        (21.3069, -157.8583),
    "buffalo":         (42.8864, -78.8784),
    "cincinnati":      (39.1031, -84.5120),
    "salt lake city":  (40.7608, -111.8910),
    "richmond":        (37.5407, -77.4360),
    "baton rouge":     (30.4515, -91.1871),
    "tacoma":          (47.2529, -122.4443),
    "albuquerque":     (35.0844, -106.6504),
    "fresno":          (36.7378, -119.7871),
    "sacramento":      (38.5816, -121.4944),
    "long beach":      (33.7701, -118.1937),
    "mesa":            (33.4152, -111.8315),
    "norfolk":         (36.8508, -76.2859),
    "madison":         (43.0731, -89.4012),
    "wilmington":      (34.2257, -77.9447),
    "fort worth":      (32.7555, -97.3308),
}

WEATHER_KEYWORDS = [
    "temperature", "rain", "snow", "storm", "hurricane",
    "degrees", "fahrenheit", "celsius", "precipitation",
    "tornado", "flood", "drought", "blizzard", "frost", "hail",
    "wind", "inches", "rainfall", "snowfall", "humidity",
    "forecast", "weather",
]


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — FETCH WEATHER MARKETS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_weather_markets() -> list[dict]:
    found   = {}
    session = requests.Session()

    # Fetch 1: broad volume-sorted fetch, filter client-side
    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true", "closed": "false",
                "limit":  500, "order": "volume24hr", "ascending": "false",
            },
            timeout=20,
        )
        resp.raise_for_status()
        all_markets = resp.json()
        if not isinstance(all_markets, list):
            all_markets = all_markets.get("markets", all_markets.get("data", []))
        for m in all_markets:
            q = (m.get("question") or m.get("title") or "").lower()
            if any(kw in q for kw in WEATHER_KEYWORDS):
                mid = m.get("id") or m.get("conditionId", "")
                found[str(mid)] = m
    except Exception as exc:
        print(f"[weather_edge] Gamma broad fetch error: {exc}")

    # Fetch 2: tag=weather endpoint
    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={"tag": "weather", "active": "true", "closed": "false", "limit": 200},
            timeout=20,
        )
        if resp.status_code == 200:
            tagged = resp.json()
            if not isinstance(tagged, list):
                tagged = tagged.get("markets", tagged.get("data", []))
            for m in tagged:
                mid = m.get("id") or m.get("conditionId", "")
                found[str(mid)] = m
    except Exception as exc:
        print(f"[weather_edge] Gamma tag=weather fetch error: {exc}")

    print(f"[weather_edge] Found {len(found)} weather markets from Gamma API.")
    return list(found.values())


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — PARSE CITY / DATE / THRESHOLD
# ══════════════════════════════════════════════════════════════════════════════

_geocode_cache: dict[str, Optional[tuple[float, float]]] = {}


def _geocode(city_name: str) -> Optional[tuple[float, float]]:
    key = city_name.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]
    if key in CITY_COORDS:
        _geocode_cache[key] = CITY_COORDS[key]
        return CITY_COORDS[key]
    try:
        resp = requests.get(
            GEOCODING_API,
            params={"name": city_name, "count": 1, "language": "en"},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            r      = results[0]
            coords = (float(r["latitude"]), float(r["longitude"]))
            _geocode_cache[key] = coords
            time.sleep(0.2)
            return coords
    except Exception:
        pass
    _geocode_cache[key] = None
    return None


def _extract_city(question: str) -> Optional[str]:
    q_lower = question.lower()
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city in q_lower:
            return city
    m = re.search(
        r"(?:temperature|rain|snow|storm|weather|high|low)\s+in\s+([A-Z][a-zA-Z\s]{2,20}?)"
        r"(?:\s+(?:be|on|by|above|below|exceed|reach|or|\d)|\?|$)",
        question,
    )
    if m:
        candidate = m.group(1).strip()
        if len(candidate) >= 3:
            return candidate.lower()
    return None


def _extract_date(question: str) -> Optional[datetime]:
    today = datetime.now(timezone.utc)
    q = question.strip()
    if "tomorrow" in q.lower():
        return today + timedelta(days=1)
    if "today" in q.lower():
        return today

    MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
        r"\s+(\d{1,2})(?:,?\s*(\d{4}))?",
        q, re.IGNORECASE,
    )
    if m:
        month = MONTHS[m.group(1).lower()]
        day   = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else today.year
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", q)
    if m:
        month = int(m.group(1))
        day   = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else today.year
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _extract_temp_threshold(question: str) -> Optional[tuple[str, float, str]]:
    # Pattern 1: "above/below N°F"
    m = re.search(
        r"\b(above|below|exceed|reach|over|under|at\s+least|more\s+than|less\s+than|hit)\s+"
        r"(\d+)\s*(?:degrees?\s*)?(fahrenheit|celsius|°f|°c|\bf\b|\bc\b)?",
        question, re.IGNORECASE,
    )
    if m:
        direction = m.group(1).lower().replace(" ", "_")
        value     = float(m.group(2))
        raw_unit  = (m.group(3) or "").upper()
        unit      = "C" if "C" in raw_unit else "F"
        above     = direction in ("above", "exceed", "over", "reach", "at_least", "more_than", "hit")
        return ("above" if above else "below", value, unit)

    # Pattern 2: "N°F or above/below/higher/lower" (Polymarket reversed format)
    m = re.search(
        r"(\d+)\s*(?:degrees?\s*)?(fahrenheit|celsius|°f|°c|\bf\b|\bc\b)?\s+"
        r"(?:or\s+)?(above|below|higher|lower|more|less|exceed)",
        question, re.IGNORECASE,
    )
    if m:
        value    = float(m.group(1))
        raw_unit = (m.group(2) or "").upper()
        if not raw_unit:
            raw_unit = "C" if "°c" in question.lower() or "celsius" in question.lower() else "F"
        unit      = "C" if "C" in raw_unit else "F"
        direction = m.group(3).lower()
        above     = direction in ("above", "higher", "more", "exceed")
        return ("above" if above else "below", value, unit)

    # Pattern 3: "be N°C on" — exact match, interpreted as "reach N°C or above"
    m = re.search(
        r"\bbe\s+(\d+)\s*(?:degrees?\s*)?(fahrenheit|celsius|°f|°c|\bf\b|\bc\b)?(?:\s+(?:on|by|or))",
        question, re.IGNORECASE,
    )
    if m:
        value    = float(m.group(1))
        raw_unit = (m.group(2) or "").upper()
        if not raw_unit:
            raw_unit = "C" if "°c" in question.lower() or "celsius" in question.lower() else "F"
        unit = "C" if "C" in raw_unit else "F"
        return ("above", value, unit)

    return None


def _is_precip_market(question: str) -> bool:
    q = question.lower()
    return any(w in q for w in ["rain", "snow", "precipitation", "inches", "rainfall",
                                  "snowfall", "blizzard", "flood", "storm", "tornado",
                                  "hurricane", "hail", "frost"])


# ══════════════════════════════════════════════════════════════════════════════
#  TIMING FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _hours_to_resolution(target_date: Optional[datetime]) -> Optional[float]:
    """Hours from now until end of target_date (23:59:59 UTC)."""
    if target_date is None:
        return None
    end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
    now  = datetime.now(timezone.utc)
    diff = (end_of_day - now).total_seconds() / 3600
    return max(0.0, diff)


def _forecast_confidence_decay(hours: Optional[float]) -> str:
    if hours is None:
        return "MEDIUM"   # unknown date — don't skip, but mark uncertain
    if hours <= 0:
        return "SKIP"     # already resolved
    if hours <= 12:
        return "HIGH"
    if hours <= 48:
        return "MEDIUM"
    return "SKIP"


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — WEATHER SOURCES
# ══════════════════════════════════════════════════════════════════════════════

_noaa_grid_cache: dict[str, dict] = {}


def _get_noaa_grid(lat: float, lon: float) -> Optional[dict]:
    key = f"{lat:.4f},{lon:.4f}"
    if key in _noaa_grid_cache:
        return _noaa_grid_cache[key]
    try:
        resp = requests.get(
            f"{NOAA_API}/points/{lat:.4f},{lon:.4f}",
            headers=NOAA_HEADERS, timeout=10,
        )
        if resp.status_code != 200:
            return None
        props = resp.json().get("properties", {})
        grid  = {
            "forecast":       props.get("forecast"),
            "forecastHourly": props.get("forecastHourly"),
            "city":           props.get("relativeLocation", {}).get("properties", {}).get("city", ""),
        }
        _noaa_grid_cache[key] = grid
        time.sleep(0.3)
        return grid
    except Exception:
        return None


def _get_noaa_forecast(forecast_url: str) -> Optional[list[dict]]:
    try:
        resp = requests.get(forecast_url, headers=NOAA_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        time.sleep(0.3)
        return data.get("properties", {}).get("periods", [])
    except Exception:
        return None


def _find_period_for_date(periods: list[dict], target_date: datetime) -> Optional[dict]:
    target_str = target_date.strftime("%Y-%m-%d")
    daytime = any_match = None
    for p in periods:
        if p.get("startTime", "").startswith(target_str):
            any_match = p
            if p.get("isDaytime", True):
                daytime = p
                break
    return daytime or any_match


def get_noaa_probability(
    city: str,
    target_date: Optional[datetime],
    question: str,
) -> tuple[Optional[float], str]:
    coords = CITY_COORDS.get(city)
    if not coords:
        return None, f"city '{city}' not in US lookup"
    lat, lon = coords
    grid = _get_noaa_grid(lat, lon)
    if not grid or not grid.get("forecast"):
        return None, "NOAA grid lookup failed"
    periods = _get_noaa_forecast(grid["forecast"])
    if not periods:
        return None, "NOAA forecast fetch failed"

    period = (_find_period_for_date(periods, target_date)
              if target_date else periods[0])
    if not period:
        period = periods[0]

    period_name = period.get("name", "")
    short_fc    = period.get("shortForecast", "")
    detail_fc   = period.get("detailedForecast", "")
    temp        = period.get("temperature")
    temp_unit   = period.get("temperatureUnit", "F")
    pop_raw     = period.get("probabilityOfPrecipitation", {})
    pop_val     = pop_raw.get("value") if isinstance(pop_raw, dict) else None

    thresh = _extract_temp_threshold(question)
    if thresh and temp is not None:
        direction, threshold, unit = thresh
        fc_temp = float(temp)
        if unit == "C" and temp_unit == "F":
            fc_temp = (fc_temp - 32) * 5 / 9
        elif unit == "F" and temp_unit == "C":
            fc_temp = fc_temp * 9 / 5 + 32
        sigma = 5.0 if unit == "F" else 2.8
        z     = (threshold - fc_temp) / sigma
        cdf   = 0.5 * (1.0 + math.erf(z / 1.4142))
        prob  = max(0.01, min(0.99, (1.0 - cdf) if direction == "above" else cdf))
        return round(prob, 3), (
            f"NOAA {period_name}: {temp}°{temp_unit} | "
            f"threshold={threshold}°{unit} {direction} | σ=5°{unit} → P={prob:.1%}"
        )

    if _is_precip_market(question):
        if pop_val is not None:
            prob = max(0.01, min(0.99, float(pop_val) / 100.0))
            return round(prob, 3), f"NOAA {period_name}: PoP={pop_val}% | '{short_fc}'"
        fc_lower = (short_fc + " " + detail_fc).lower()
        if any(w in fc_lower for w in ["snow", "blizzard"]):
            prob = 0.80 if "heavy" in fc_lower else (0.60 if "likely" in fc_lower else 0.85)
        elif any(w in fc_lower for w in ["rain", "shower", "storm"]):
            prob = 0.70 if "likely" in fc_lower else (0.40 if "chance" in fc_lower else 0.75)
        elif any(w in fc_lower for w in ["sunny", "clear"]):
            prob = 0.05
        else:
            prob = 0.20
        return round(prob, 3), f"NOAA {period_name}: '{short_fc}' (keyword)"

    return None, f"NOAA: market type unclear: '{short_fc}'"


def get_openmeteo_probability(
    lat: float,
    lon: float,
    target_date: Optional[datetime],
    question: str,
) -> tuple[Optional[float], str]:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end_date  = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            OPENMETEO_API,
            params={
                "latitude":   lat, "longitude": lon,
                "daily":      "temperature_2m_max,temperature_2m_min,"
                              "precipitation_probability_max,precipitation_sum",
                "timezone":   "auto",
                "start_date": today_str, "end_date": end_date,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        time.sleep(0.2)
    except Exception as exc:
        return None, f"Open-Meteo error: {exc}"

    daily       = data.get("daily", {})
    dates       = daily.get("time", [])
    max_temps   = daily.get("temperature_2m_max", [])
    min_temps   = daily.get("temperature_2m_min", [])
    precip_prob = daily.get("precipitation_probability_max", [])
    precip_sum  = daily.get("precipitation_sum", [])
    units       = data.get("daily_units", {})
    t_unit      = units.get("temperature_2m_max", "°C")

    target_str = target_date.strftime("%Y-%m-%d") if target_date else today_str
    idx = dates.index(target_str) if target_str in dates else 0

    if idx >= len(max_temps):
        return None, "Open-Meteo: date beyond forecast window"

    t_max  = max_temps[idx] if idx < len(max_temps) else None
    t_min  = min_temps[idx] if idx < len(min_temps) else None
    pop    = precip_prob[idx] if idx < len(precip_prob) else None
    p_sum  = precip_sum[idx]  if idx < len(precip_sum)  else None

    thresh = _extract_temp_threshold(question)
    if thresh and t_max is not None:
        direction, threshold, q_unit = thresh
        fc_temp = float(t_max)
        if q_unit == "F" and "°C" in t_unit:
            fc_temp = fc_temp * 9 / 5 + 32
            sigma = 5.0
        elif q_unit == "C" and "°F" in t_unit:
            fc_temp = (fc_temp - 32) * 5 / 9
            sigma = 2.8
        else:
            sigma = 2.8 if "°C" in t_unit else 5.0
        z    = (threshold - fc_temp) / sigma
        cdf  = 0.5 * (1.0 + math.erf(z / 1.4142))
        prob = max(0.01, min(0.99, (1.0 - cdf) if direction == "above" else cdf))
        return round(prob, 3), (
            f"Open-Meteo: max={t_max}{t_unit} min={t_min}{t_unit} | "
            f"threshold={threshold}°{q_unit} {direction} | σ→P={prob:.1%}"
        )

    if _is_precip_market(question) and pop is not None:
        prob = max(0.01, min(0.99, float(pop) / 100.0))
        return round(prob, 3), f"Open-Meteo: precip_prob={pop}% sum={p_sum}mm"

    return None, "Open-Meteo: no matching metric"


def get_tomorrowio_probability(
    lat: float,
    lon: float,
    target_date: Optional[datetime],
    question: str,
) -> tuple[Optional[float], str]:
    if not TOMORROW_KEY:
        return None, "Tomorrow.io: no API key configured"
    try:
        resp = requests.get(
            TOMORROWIO_API,
            params={
                "location":  f"{lat},{lon}",
                "timesteps": "1d",
                "fields":    "temperatureMax,temperatureMin,precipitationProbabilityAvg",
                "apikey":    TOMORROW_KEY,
                "units":     "metric",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        time.sleep(0.2)
    except Exception as exc:
        return None, f"Tomorrow.io error: {exc}"

    daily      = data.get("timelines", {}).get("daily", [])
    target_str = target_date.strftime("%Y-%m-%d") if target_date else \
                 datetime.now(timezone.utc).strftime("%Y-%m-%d")

    day_data = next(
        (e.get("values", {}) for e in daily if e.get("time", "")[:10] == target_str),
        daily[0].get("values", {}) if daily else None,
    )
    if not day_data:
        return None, "Tomorrow.io: no daily data"

    t_max      = day_data.get("temperatureMax")
    t_min      = day_data.get("temperatureMin")
    precip_pct = day_data.get("precipitationProbabilityAvg")

    thresh = _extract_temp_threshold(question)
    if thresh and t_max is not None:
        direction, threshold, q_unit = thresh
        fc_temp = float(t_max)  # metric = °C
        if q_unit == "F":
            fc_temp = fc_temp * 9 / 5 + 32
            sigma = 5.0
        else:
            sigma = 2.8
        z    = (threshold - fc_temp) / sigma
        cdf  = 0.5 * (1.0 + math.erf(z / 1.4142))
        prob = max(0.01, min(0.99, (1.0 - cdf) if direction == "above" else cdf))
        return round(prob, 3), (
            f"Tomorrow.io: max={t_max}°C min={t_min}°C | "
            f"threshold={threshold}°{q_unit} {direction} | σ→P={prob:.1%}"
        )

    if _is_precip_market(question) and precip_pct is not None:
        prob = max(0.01, min(0.99, float(precip_pct) / 100.0))
        return round(prob, 3), f"Tomorrow.io: precipProbAvg={precip_pct}%"

    return None, "Tomorrow.io: no matching metric"


def get_metno_probability(
    lat: float,
    lon: float,
    target_date: Optional[datetime],
    question: str,
) -> tuple[Optional[float], str]:
    try:
        resp = requests.get(
            METNO_API,
            params={"lat": round(lat, 4), "lon": round(lon, 4)},
            headers=METNO_HEADERS,
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        time.sleep(0.2)
    except Exception as exc:
        return None, f"Met.no error: {exc}"

    timeseries = data.get("properties", {}).get("timeseries", [])
    target_str = (target_date.strftime("%Y-%m-%d") if target_date
                  else datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    daily_max_temps: list[float] = []
    daily_precip:    list[float] = []

    for entry in timeseries:
        if entry.get("time", "")[:10] != target_str:
            continue
        n6 = entry.get("data", {}).get("next_6_hours", {}).get("details", {})
        if n6.get("air_temperature_max") is not None:
            daily_max_temps.append(float(n6["air_temperature_max"]))
        if n6.get("precipitation_amount") is not None:
            daily_precip.append(float(n6["precipitation_amount"]))

    # Fallback to instant temps if no 6h blocks
    if not daily_max_temps:
        for entry in timeseries:
            if entry.get("time", "")[:10] != target_str:
                continue
            inst = entry.get("data", {}).get("instant", {}).get("details", {})
            if inst.get("air_temperature") is not None:
                daily_max_temps.append(float(inst["air_temperature"]))

    thresh = _extract_temp_threshold(question)
    if thresh and daily_max_temps:
        t_max     = max(daily_max_temps)
        direction, threshold, q_unit = thresh
        fc_temp = t_max  # Met.no is Celsius
        if q_unit == "F":
            fc_temp = t_max * 9 / 5 + 32
            sigma = 5.0
        else:
            sigma = 2.8
        z    = (threshold - fc_temp) / sigma
        cdf  = 0.5 * (1.0 + math.erf(z / 1.4142))
        prob = max(0.01, min(0.99, (1.0 - cdf) if direction == "above" else cdf))
        return round(prob, 3), (
            f"Met.no: max={t_max:.1f}°C | "
            f"threshold={threshold}°{q_unit} {direction} | σ→P={prob:.1%}"
        )

    if _is_precip_market(question) and daily_precip:
        total_mm = sum(daily_precip)
        prob = max(0.01, min(0.99, min(1.0, total_mm / 5.0)))
        return round(prob, 3), f"Met.no: precip_sum={total_mm:.1f}mm (proxy)"

    return None, "Met.no: no matching data"


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — CONSENSUS PROBABILITY
# ══════════════════════════════════════════════════════════════════════════════

def consensus_probability(
    lat: float,
    lon: float,
    city: str,
    target_date: Optional[datetime],
    question: str,
) -> tuple[Optional[float], str, int, dict]:
    """
    Fetch from all available sources, compute consensus.
    Returns (prob, confidence, sources_agreed, details_dict).
    confidence: "HIGH" (≥2 sources agree ≤10%), "MEDIUM" (2 sources agree ≤10%),
                "LOW" (all disagree or only 1 source)
    Only MEDIUM and HIGH are used; LOW is skipped by caller.
    """
    probs:   dict[str, float] = {}
    details: dict[str, str]   = {}

    # Open-Meteo (worldwide)
    p, d = get_openmeteo_probability(lat, lon, target_date, question)
    if p is not None:
        probs["openmeteo"] = p
        details["openmeteo"] = d

    # Tomorrow.io (if key configured)
    if TOMORROW_KEY:
        p, d = get_tomorrowio_probability(lat, lon, target_date, question)
        if p is not None:
            probs["tomorrowio"] = p
            details["tomorrowio"] = d

    # Met.no / Norwegian Met (worldwide)
    p, d = get_metno_probability(lat, lon, target_date, question)
    if p is not None:
        probs["metno"] = p
        details["metno"] = d

    # NOAA (US cities only)
    if city in CITY_COORDS:
        p, d = get_noaa_probability(city, target_date, question)
        if p is not None:
            probs["noaa"] = p
            details["noaa"] = d

    if not probs:
        return None, "NONE", 0, {}

    values = sorted(probs.values())
    n      = len(values)

    if n == 1:
        return values[0], "LOW", 1, details

    if n == 2:
        if abs(values[1] - values[0]) <= 0.10:
            return round((values[0] + values[1]) / 2, 3), "MEDIUM", 2, details
        return None, "LOW", 0, details

    # n >= 3: check spread
    spread = values[-1] - values[0]
    if spread <= 0.10:
        return round(sum(values) / n, 3), "HIGH", n, details

    # Find any pair that agrees within 10%
    for i in range(n):
        for j in range(i + 1, n):
            if abs(values[i] - values[j]) <= 0.10:
                median = sorted(values)[n // 2]
                return round(median, 3), "MEDIUM", 2, details

    # All disagree
    return None, "LOW", 0, details


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 5 — ANALYSE MARKET
# ══════════════════════════════════════════════════════════════════════════════

def analyse_market(market: dict) -> Optional[dict]:
    question   = market.get("question") or market.get("title") or ""
    prices_raw = market.get("outcomePrices")
    best_ask   = market.get("bestAsk")

    poly_prob = None
    if prices_raw:
        try:
            prices    = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            poly_prob = float(prices[0])
        except Exception:
            pass
    if poly_prob is None and best_ask:
        try:
            poly_prob = float(best_ask)
        except Exception:
            pass

    if poly_prob is None or poly_prob < 0.001 or poly_prob > 0.999:
        return None

    volume      = float(market.get("volume24hr") or market.get("volume") or 0)
    city        = _extract_city(question)
    target_date = _extract_date(question)

    if not city:
        return None

    coords = _geocode(city)
    if not coords:
        return None
    lat, lon = coords

    hours = _hours_to_resolution(target_date)
    decay = _forecast_confidence_decay(hours)
    if decay == "SKIP":
        return None

    our_prob, confidence, sources_agreed, source_details = consensus_probability(
        lat, lon, city, target_date, question,
    )

    if our_prob is None or confidence == "LOW":
        return None

    gap     = our_prob - poly_prob
    abs_gap = abs(gap)
    flagged = abs_gap >= EDGE_THRESHOLD

    direction = ""
    if flagged:
        direction = ("BUY YES (our prob higher than market)"
                     if gap > 0 else "BUY NO  (our prob lower than market)")

    return {
        "question":                  question,
        "market_id":                 str(market.get("id") or market.get("conditionId", "")),
        "city":                      city,
        "target_date":               target_date.strftime("%Y-%m-%d") if target_date else "unknown",
        "our_prob":                  our_prob,
        "poly_prob":                 round(poly_prob, 4),
        "gap":                       round(gap, 4),
        "abs_gap":                   round(abs_gap, 4),
        "flagged":                   flagged,
        "direction":                 direction,
        "confidence":                confidence,
        "sources_agreed":            sources_agreed,
        "source_details":            source_details,
        "hours_to_resolution":       round(hours, 1) if hours is not None else None,
        "forecast_confidence_decay": decay,
        "volume_24h":                round(volume, 2),
        "scanned_at":                datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HISTORY TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def _load_history() -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE) as f:
        return json.load(f)


def _save_history(history: list[dict]) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _append_to_history(result: dict) -> None:
    history   = _load_history()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    already   = any(
        h.get("market_id") == result["market_id"]
        and h.get("timestamp", "")[:10] == today_str
        for h in history
    )
    if already:
        return
    history.append({
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "market_id":       result.get("market_id", ""),
        "market_question": result["question"],
        "polymarket_prob": result["poly_prob"],
        "our_prob":        result["our_prob"],
        "gap":             result["gap"],
        "confidence":      result["confidence"],
        "sources_agreed":  result["sources_agreed"],
        "resolution":      None,
    })
    _save_history(history)


# ══════════════════════════════════════════════════════════════════════════════
#  --settle MODE
# ══════════════════════════════════════════════════════════════════════════════

def settle_mode() -> None:
    history   = _load_history()
    unsettled = [h for h in history if h.get("resolution") is None]
    if not unsettled:
        print("No unsettled entries in weather_history.json.")
        return

    print(f"Checking {len(unsettled)} unsettled market(s) against Gamma API…")
    updated = 0

    for entry in unsettled:
        mid = entry.get("market_id")
        if not mid:
            continue
        try:
            resp = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=10)
            if resp.status_code != 200:
                continue
            m = resp.json()
        except Exception:
            continue

        if not m.get("closed"):
            continue

        prices_raw = m.get("outcomePrices")
        if prices_raw:
            try:
                prices    = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                yes_price = float(prices[0])
                if yes_price > 0.99:
                    entry["resolution"] = "YES"
                elif yes_price < 0.01:
                    entry["resolution"] = "NO"
                if entry["resolution"]:
                    updated += 1
            except Exception:
                pass

    _save_history(history)

    settled = [h for h in history if h.get("resolution") is not None]
    if not settled:
        print(f"Updated {updated} resolution(s). No resolved markets to compute stats.")
        return

    def _outcome(h: dict) -> float:
        return 1.0 if h["resolution"] == "YES" else 0.0

    our_correct = sum(
        1 for h in settled
        if abs(h["our_prob"] - _outcome(h)) < abs(h["polymarket_prob"] - _outcome(h))
    )
    n = len(settled)

    correct_gaps   = [abs(h["gap"]) for h in settled
                      if abs(h["our_prob"] - _outcome(h)) < abs(h["polymarket_prob"] - _outcome(h))]
    incorrect_gaps = [abs(h["gap"]) for h in settled
                      if abs(h["our_prob"] - _outcome(h)) >= abs(h["polymarket_prob"] - _outcome(h))]

    print(f"\n{'═'*60}")
    print(f"  WEATHER EDGE ACCURACY  ({n} resolved)")
    print(f"{'═'*60}")
    print(f"  Our prob closer to outcome : {our_correct}/{n} ({our_correct/n:.1%})")
    if correct_gaps:
        print(f"  Mean gap when right        : {sum(correct_gaps)/len(correct_gaps):.1%}")
    if incorrect_gaps:
        print(f"  Mean gap when wrong        : {sum(incorrect_gaps)/len(incorrect_gaps):.1%}")
    print(f"{'═'*60}\n")
    print(f"Updated {updated} new resolution(s).")


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-SETTLE WEATHER HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def auto_settle_weather_history() -> dict:
    """
    Silently check unsettled weather_history.json entries against Gamma API.
    Update resolutions. Compute and return accuracy stats.
    If accuracy > 60% after 10+ samples, post Moltbook accuracy update.

    Returns dict: {settled_now, total_settled, our_correct, accuracy, n}
    """
    history   = _load_history()
    unsettled = [h for h in history if h.get("resolution") is None]
    settled_now = 0

    for entry in unsettled:
        mid = entry.get("market_id")
        if not mid:
            continue
        try:
            resp = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=10)
            if resp.status_code != 200:
                continue
            m = resp.json()
        except Exception:
            continue

        if not m.get("closed"):
            continue

        prices_raw = m.get("outcomePrices")
        if not prices_raw:
            continue
        try:
            prices    = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0])
            if yes_price > 0.99:
                entry["resolution"] = "YES"
                settled_now += 1
            elif yes_price < 0.01:
                entry["resolution"] = "NO"
                settled_now += 1
        except Exception:
            pass

    if settled_now:
        _save_history(history)

    # Compute accuracy stats
    resolved = [h for h in history if h.get("resolution") is not None]
    n = len(resolved)
    if n == 0:
        return {"settled_now": settled_now, "total_settled": 0, "our_correct": 0,
                "accuracy": 0.0, "n": 0}

    def _outcome(h: dict) -> float:
        return 1.0 if h["resolution"] == "YES" else 0.0

    our_correct = sum(
        1 for h in resolved
        if abs(h["our_prob"] - _outcome(h)) < abs(h["polymarket_prob"] - _outcome(h))
    )
    accuracy = our_correct / n

    stats = {
        "settled_now":   settled_now,
        "total_settled": n,
        "our_correct":   our_correct,
        "accuracy":      round(accuracy, 4),
        "n":             n,
    }

    # Post Moltbook accuracy milestone if 10+ samples and accuracy > 60%
    if n >= 10 and accuracy > 0.60:
        try:
            creds_file = os.path.expanduser("~/.config/moltbook/credentials.json")
            if os.path.exists(creds_file):
                with open(creds_file) as f:
                    creds = json.load(f)
                api_key = creds.get("api_key", "")
                if api_key and api_key != "PENDING_REGISTRATION":
                    # Only post if not already posted for this threshold
                    _milestone_file = os.path.join(_DIR, "weather_milestone_state.json")
                    _ms = {}
                    if os.path.exists(_milestone_file):
                        try:
                            with open(_milestone_file) as f:
                                _ms = json.load(f)
                        except Exception:
                            pass
                    threshold_key = f"accuracy_{int(accuracy * 100)}_at_{n}"
                    if threshold_key not in _ms:
                        headers = {
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type":  "application/json",
                        }
                        MOLTBOOK_API_URL = "https://moltbook.com/api/v1"
                        title = (
                            f"🌦️ Weather model accuracy: {accuracy:.1%} "
                            f"({our_correct}/{n} closer than market)"
                        )
                        content = (
                            f"# Weather Model Accuracy Update\n\n"
                            f"After **{n} resolved weather markets**, the multi-source "
                            f"consensus model (Open-Meteo + Met.no) outperformed the "
                            f"Polymarket implied probability in **{our_correct}/{n} cases** "
                            f"({accuracy:.1%} accuracy).\n\n"
                            f"Settled this run: {settled_now} new resolution(s).\n\n"
                            f"This is the core edge source — when forecasters agree and the "
                            f"market disagrees, the market tends to be wrong.\n\n"
                            f"*Observational data only. Paper trades. Not financial advice.*"
                        )
                        try:
                            requests.post(
                                f"{MOLTBOOK_API_URL}/posts",
                                headers=headers,
                                json={
                                    "submolt_name": "polymarket",
                                    "submolt":      "polymarket",
                                    "title":        title,
                                    "content":      content,
                                },
                                timeout=15,
                            )
                            _ms[threshold_key] = datetime.now(timezone.utc).isoformat()
                            with open(_milestone_file, "w") as f:
                                json.dump(_ms, f, indent=2)
                        except Exception:
                            pass
        except Exception:
            pass

    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  --monitor MODE
# ══════════════════════════════════════════════════════════════════════════════

def _log(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [WEATHER-EDGE] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _post_weather_alert(result: dict) -> Optional[str]:
    creds_file = os.path.expanduser("~/.config/moltbook/credentials.json")
    if not os.path.exists(creds_file):
        return None
    with open(creds_file) as f:
        creds = json.load(f)
    api_key = creds.get("api_key")
    if not api_key or api_key == "PENDING_REGISTRATION":
        return None

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    action  = result["direction"].split("(")[0].strip()
    title   = (f"[WEATHER-EDGE] {result['city'].title()} {result['target_date']} "
               f"— {action}  gap={result['gap']:+.1%}  [{result['confidence']}]")

    src_lines = "\n".join(
        f"  - **{src.capitalize()}**: {det}"
        for src, det in result.get("source_details", {}).items()
    )
    body = "\n".join([
        f"**Market**: {result['question']}",
        f"**City**: {result['city'].title()}  |  **Date**: {result['target_date']}",
        f"**Our probability**: {result['our_prob']:.1%}  "
        f"(confidence: {result['confidence']}, {result['sources_agreed']} sources agreed)",
        f"**Polymarket**: {result['poly_prob']:.1%}",
        f"**Gap**: {result['gap']:+.1%}  →  {result['direction']}",
        f"**Time to resolution**: {result['hours_to_resolution']:.1f}h  "
        f"({result['forecast_confidence_decay']} decay)",
        f"**Volume**: ${result['volume_24h']:,.0f} / 24h",
        "",
        "**Forecast sources:**",
        src_lines,
        "",
        "---",
        "*Paper observation only — no real money placed.*",
    ])

    post_url = None
    try:
        resp = requests.post(
            "https://moltbook.com/api/v1/posts",
            headers=headers,
            json={"submolt_name": "sports", "submolt": "sports",
                  "title": title, "content": body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            post = resp.json().get("post", {})
            pid  = post.get("id")
            post_url = f"https://www.moltbook.com/m/sports/{pid}" if pid else None
    except Exception:
        pass

    if _X_AVAILABLE and result.get("flagged"):
        post_to_x(format_weather_tweet(result))

    return post_url


def monitor_mode() -> None:
    _log(f"Monitor started — scanning every {MONITOR_INTERVAL // 60} minutes.")
    posted_today: dict[str, str] = {}   # market_id → posted_at ISO
    last_seen:    dict[str, dict] = {}  # market_id → last result
    last_settle_check: Optional[datetime] = None

    while True:
        now       = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        # Auto-settle every 6 hours
        if last_settle_check is None or (now - last_settle_check).total_seconds() >= 21600:
            try:
                stats = auto_settle_weather_history()
                if stats["settled_now"]:
                    _log(
                        f"Auto-settled {stats['settled_now']} weather market(s). "
                        f"Accuracy: {stats['our_correct']}/{stats['total_settled']} "
                        f"({stats['accuracy']:.1%})"
                    )
            except Exception as exc:
                _log(f"Auto-settle error: {exc}")
            last_settle_check = now

        # Reset at midnight
        if not any(v.startswith(today_str) for v in posted_today.values()):
            posted_today.clear()

        _log("Starting scan cycle…")
        markets       = fetch_weather_markets()
        current_edges: dict[str, dict] = {}

        for market in markets:
            result = analyse_market(market)
            if result and result["flagged"]:
                current_edges[result["market_id"]] = result

        _log(f"Scanned {len(markets)} markets — {len(current_edges)} active edge(s).")

        for mid, result in current_edges.items():
            if mid not in posted_today:
                _log(
                    f"NEW EDGE: {result['city'].title()} {result['target_date']} "
                    f"gap={result['gap']:+.1%} conf={result['confidence']} "
                    f"vol=${result['volume_24h']:,.0f}"
                )
                url = _post_weather_alert(result)
                if url:
                    _log(f"Posted to Moltbook: {url}")
                _append_to_history(result)
                posted_today[mid] = now.isoformat()

        for mid, prev in last_seen.items():
            if prev.get("flagged") and mid not in current_edges:
                _log(f"EDGE CLOSED: {prev['city'].title()} — market moved toward our estimate.")

        last_seen = dict(current_edges)

        output = {
            "generated_at":  now.isoformat(),
            "mode":          "monitor",
            "active_edges":  len(current_edges),
            "results":       sorted(current_edges.values(),
                                    key=lambda x: x["abs_gap"], reverse=True),
        }
        with open(OUTPUT_FILE, "w") as f:
            json.dump(output, f, indent=2)

        _log(f"Sleeping {MONITOR_INTERVAL // 60} minutes…")
        time.sleep(MONITOR_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket weather edge finder v2")
    parser.add_argument("--settle",  action="store_true",
                        help="Settle resolved markets in weather_history.json")
    parser.add_argument("--monitor", action="store_true",
                        help="Continuous 30-min monitor mode")
    args = parser.parse_args()

    if args.settle:
        settle_mode()
        return

    if args.monitor:
        monitor_mode()
        return

    # ── One-shot scan ─────────────────────────────────────────────────────────
    sources_active = ["Open-Meteo", "Met.no"]
    if TOMORROW_KEY:
        sources_active.append("Tomorrow.io")

    print("=" * 70)
    print("  Polymarket Weather Edge Finder  (v2 — Multi-Source Consensus)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Sources: {', '.join(sources_active)}")
    print("  OBSERVE ONLY — no orders placed")
    print("=" * 70)

    markets = fetch_weather_markets()
    if not markets:
        print("No weather markets found.")
        return

    print(f"\nAnalysing {len(markets)} weather markets (≤{MAX_HOURS_AHEAD}h to resolution only)…\n")

    results:          list[dict] = []
    flagged:          list[dict] = []
    skipped_timing   = 0
    skipped_no_city  = 0
    skipped_low_conf = 0

    for i, market in enumerate(markets, 1):
        question    = (market.get("question") or market.get("title") or "")
        city        = _extract_city(question)
        target_date = _extract_date(question)

        short_q = question[:68]
        print(f"  [{i:>3}/{len(markets)}] {short_q}", end="  ", flush=True)

        if not city:
            print("— skip (no city)")
            skipped_no_city += 1
            continue

        hours = _hours_to_resolution(target_date)
        decay = _forecast_confidence_decay(hours)
        if decay == "SKIP":
            hrs_str = f"{hours:.0f}h" if hours is not None else "?"
            print(f"— skip (>{MAX_HOURS_AHEAD}h away: {hrs_str})")
            skipped_timing += 1
            continue

        result = analyse_market(market)
        if result is None:
            print("— skip (low conf / no data)")
            skipped_low_conf += 1
            continue

        results.append(result)
        flag = " ◄ EDGE" if result["flagged"] else ""
        print(
            f"P={result['our_prob']:.1%} Poly={result['poly_prob']:.1%} "
            f"gap={result['gap']:+.1%} [{result['confidence']}  "
            f"{result['sources_agreed']}src]{flag}"
        )
        if result["flagged"]:
            flagged.append(result)
            _append_to_history(result)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(
        f"  RESULTS: {len(results)} analysed | {len(flagged)} edges >{EDGE_THRESHOLD:.0%} | "
        f"{skipped_timing} timing | {skipped_no_city} no-city | {skipped_low_conf} low-conf"
    )
    print(f"{'═'*70}")

    if results:
        results.sort(key=lambda x: x["abs_gap"], reverse=True)
        print(f"\n{'Market':<35} {'P':>6} {'Poly':>6} {'Gap':>7} {'Conf':>6} {'Vol$':>10} {'ETA':>5}")
        print("─" * 70)
        for r in results:
            q    = r["question"][:34]
            flag = " ◄" if r["flagged"] else ""
            eta  = f"{r['hours_to_resolution']:.0f}h" if r["hours_to_resolution"] is not None else "?"
            print(
                f"{q:<35} {r['our_prob']:>5.1%} {r['poly_prob']:>6.1%} "
                f"{r['gap']:>+6.1%} {r['confidence']:>6} "
                f"{r['volume_24h']:>10,.0f} {eta:>5}{flag}"
            )

    if flagged:
        print(f"\n{'─'*70}")
        print(f"  FLAGGED EDGES (gap >{EDGE_THRESHOLD:.0%}, confidence ≥ MEDIUM):")
        print(f"{'─'*70}")
        for r in flagged:
            print(f"\n  Market     : {r['question']}")
            print(f"  City       : {r['city'].title()}  |  Date: {r['target_date']}")
            print(f"  Confidence : {r['confidence']}  ({r['sources_agreed']} sources agreed)")
            for src, det in r.get("source_details", {}).items():
                print(f"  {src.capitalize():<11}: {det}")
            print(f"  Polymarket : {r['poly_prob']:.1%}")
            print(f"  Our prob   : {r['our_prob']:.1%}")
            print(f"  Gap        : {r['gap']:+.1%}  →  {r['direction']}")
            eta = f"{r['hours_to_resolution']:.1f}h" if r["hours_to_resolution"] is not None else "?"
            print(f"  Time to res: {eta}  ({r['forecast_confidence_decay']} decay)")
            print(f"  Volume     : ${r['volume_24h']:,.2f} / 24h")
    else:
        print(f"\n  No edges >{EDGE_THRESHOLD:.0%} with MEDIUM+ confidence found.")

    print(f"\n{'═'*70}\n")

    output = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "edge_threshold":  EDGE_THRESHOLD,
        "sources_active":  sources_active,
        "markets_scanned": len(markets),
        "markets_matched": len(results),
        "edges_flagged":   len(flagged),
        "results":         results,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
