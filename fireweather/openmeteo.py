"""7-day daily forecast from Open-Meteo (https://open-meteo.com), for the point panel's at-a-glance outlook.

Open-Meteo blends national models (GEM over Canada) and returns local-day min/max, precipitation and a WMO weather
code per day. Free for non-commercial use, no key. Responses are cached per ~1 km point for CACHE_S.
"""
import logging
import threading
import time

import requests

log = logging.getLogger("openmeteo")

API = "https://api.open-meteo.com/v1/forecast"
DAILY = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
         "precipitation_probability_max", "wind_speed_10m_max", "wind_gusts_10m_max"]
CACHE_S = 1800

_session = requests.Session()
_lock = threading.Lock()
_cache = {}


def daily(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    r = _session.get(API, params={"latitude": key[0], "longitude": key[1], "daily": ",".join(DAILY),
                                  "timezone": "auto", "forecast_days": 7}, timeout=20)
    r.raise_for_status()
    d = r.json()
    out = {"timezone": d["timezone"], "utc_offset_s": d["utc_offset_seconds"], "elevation": d.get("elevation"),
           "days": [{"date": t, **{k: d["daily"][k][i] for k in DAILY}} for i, t in enumerate(d["daily"]["time"])]}
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time(), out)
    return out
