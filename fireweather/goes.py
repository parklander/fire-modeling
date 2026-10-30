"""GOES-East and GOES-West satellite imagery from ECCC GeoMet (WMS), for the weather map's Satellite layers.

1 km, every 10 minutes, about two days kept. Each product comes from both satellites (keys "east:fire",
"west:fire", ...); the page shows GOES-West when the map is centred over the west, where it sees at a better angle.
GeoMet only answers exact times inside its window (nearestValue=0), so the page asks here for each layer's
current time range and rounds to it.
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("goes")

WMS = "https://geo.weather.gc.ca/geomet"
PRODUCTS = {
    "fire": "1km_FireTemperature-SWIR",
    "smoke": "1km_SmokeABIband1-SWIR",
    "natural": "1km_NaturalColor",
    "vis": "1km_DayVis",
    "visir": "1km_DayVis-NightIR",
}
LAYERS = {f"{sat.lower()}:{k}": f"GOES-{sat}_{v}" for sat in ("East", "West") for k, v in PRODUCTS.items()}
_session = requests.Session()
_lock = threading.Lock()
_cache = {"at": 0.0, "times": {}}


def times(max_age=300):
    """{key: {"layer", "start", "end", "step_min"}} from GeoMet's per-layer capabilities (cached 5 min)."""
    with _lock:
        if time.time() - _cache["at"] < max_age and _cache["times"]:
            return _cache["times"]
    def one(layer):
        try:
            r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities",
                                          "layer": layer}, timeout=30)
            r.raise_for_status()
            m = re.search(r'<Dimension name="time"[^>]*>([^/<]+)/([^/<]+)/PT(\d+)M', r.text)
            return {"layer": layer, "start": m[1], "end": m[2], "step_min": int(m[3])} if m else None
        except Exception as e:
            log.warning("GOES %s times unavailable: %s", layer, e)
    with ThreadPoolExecutor(5) as ex:
        out = {k: v for k, v in zip(LAYERS, ex.map(one, LAYERS.values())) if v}
    with _lock:
        if out:
            _cache.update(at=time.time(), times=out)
        return _cache["times"]
