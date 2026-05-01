"""Stadium weather via the National Weather Service public API (no key).

Returns wind speed/direction + temp for each park's first-pitch hour.
A simple HR factor: wind blowing OUT (within ±60° of CF heading) at >10 mph
is a tailwind for HRs; wind blowing IN at >10 mph suppresses.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

import requests

# Park lat/lon and CF compass heading (the direction the wind is "blowing OUT"
# toward — i.e., from home plate to dead-CF). Approximate orientations from
# Baseball Almanac. Heading: 0=N, 90=E, 180=S, 270=W.
PARKS = {
    "AZ":  (33.4453, -112.0667, 5),    "ATL": (33.8908, -84.4678, 27),
    "BAL": (39.2839, -76.6217, 33),    "BOS": (42.3467, -71.0972, 45),
    "CHC": (41.9484, -87.6553, 36),    "CWS": (41.8300, -87.6339, 40),
    "CIN": (39.0972, -84.5078, 65),    "CLE": (41.4962, -81.6852, 30),
    "COL": (39.7559, -104.9942, 0),    "DET": (42.3390, -83.0485, 30),
    "HOU": (29.7572, -95.3554, 19),    "KC":  (39.0517, -94.4803, 45),
    "LAA": (33.8003, -117.8827, 60),   "LAD": (34.0739, -118.2400, 22),
    "MIA": (25.7781, -80.2196, 75),    "MIL": (43.0280, -87.9712, 60),
    "MIN": (44.9817, -93.2776, 0),     "NYM": (40.7571, -73.8458, 30),
    "NYY": (40.8296, -73.9262, 30),    "ATH": (37.7516, -122.2005, 65),
    "PHI": (39.9061, -75.1665, 25),    "PIT": (40.4469, -80.0058, 60),
    "SD":  (32.7076, -117.1570, 55),   "SF":  (37.7786, -122.3893, 90),
    "SEA": (47.5914, -122.3325, 0),    "STL": (38.6226, -90.1928, 35),
    "TB":  (27.7682, -82.6534, 30),    "TEX": (32.7472, -97.0832, 12),
    "TOR": (43.6414, -79.3894, 0),     "WSH": (38.8729, -77.0074, 30),
}

# Domes / retractable that almost always close → weather doesn't matter.
DOMED = {"ARI", "AZ", "HOU", "MIA", "MIL", "TB", "TEX", "TOR", "SEA", "MIN"}

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 1800  # 30 min

UA = {"User-Agent": "mlb_dfs/0.1 (contact@example.com)"}


def _nws(lat: float, lon: float):
    key = f"{lat:.3f},{lon:.3f}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        meta = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=UA, timeout=8).json()
        url = meta["properties"]["forecastHourly"]
        data = requests.get(url, headers=UA, timeout=8).json()
        _CACHE[key] = (now, data)
        return data
    except Exception:
        return None


def _compass_to_deg(c: str) -> int | None:
    pts = {"N":0,"NNE":22,"NE":45,"ENE":67,"E":90,"ESE":112,"SE":135,"SSE":157,
           "S":180,"SSW":202,"SW":225,"WSW":247,"W":270,"WNW":292,"NW":315,"NNW":337}
    return pts.get((c or "").upper())


def hr_factor(wind_mph: float, wind_from_deg: float, cf_heading_deg: float) -> float:
    """Wind FROM south at park facing N-CF means tailwind = blow-out.
    Convert wind_from to wind_to (180 + from)."""
    if wind_mph is None or wind_mph < 5:
        return 1.0
    wind_to = (wind_from_deg + 180) % 360
    diff = abs((wind_to - cf_heading_deg + 180) % 360 - 180)
    if diff <= 60:    # blowing out
        return 1.0 + min(wind_mph, 25) * 0.012   # +1.2% per mph, max ~+30%
    if diff >= 120:   # blowing in
        return 1.0 - min(wind_mph, 25) * 0.010   # max ~-25%
    return 1.0


def park_forecast(team_abbr: str, when_iso: str) -> dict | None:
    """team_abbr: home team. when_iso: ISO 8601 game start time.
    Returns {wind_mph, wind_dir, temp_f, hr_factor, dome}."""
    park = PARKS.get(team_abbr)
    if not park:
        return None
    lat, lon, cf_heading = park
    if team_abbr in DOMED:
        return {"wind_mph": 0, "wind_dir": "—", "temp_f": 72, "hr_factor": 1.0, "dome": True}
    data = _nws(lat, lon)
    if not data:
        return None
    try:
        target = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    best = None
    for p in data.get("properties", {}).get("periods", []):
        try:
            t = datetime.fromisoformat(p["startTime"])
        except Exception:
            continue
        if best is None or abs((t - target).total_seconds()) < abs((best["t"] - target).total_seconds()):
            best = {"t": t, "p": p}
    if not best:
        return None
    p = best["p"]
    wind_mph = 0
    try:
        wind_mph = int(p.get("windSpeed", "0").split()[0])
    except Exception:
        pass
    wind_from = _compass_to_deg(p.get("windDirection") or "")
    factor = hr_factor(wind_mph, wind_from, cf_heading) if wind_from is not None else 1.0
    return {
        "wind_mph": wind_mph,
        "wind_dir": p.get("windDirection") or "—",
        "temp_f": p.get("temperature"),
        "hr_factor": round(factor, 3),
        "dome": False,
    }
