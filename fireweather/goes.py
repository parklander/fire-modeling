"""GOES-East satellite imagery from ECCC GeoMet (WMS), for the weather map's Satellite layers.

Layers are RGB composites by day and shortwave infrared at night, 1 km, every 10 minutes, about two days kept.
GeoMet only answers exact times inside its window (nearestValue=0), so the page asks here for each layer's
current time range and rounds to it.
"""
import logging
import re
import threading
import time

import requests

log = logging.getLogger("goes")

WMS = "https://geo.weather.gc.ca/geomet"
LAYERS = {
    "fire": "GOES-East_1km_FireTemperature-SWIR",
    "smoke": "GOES-East_1km_SmokeABIband1-SWIR",
}
_session = requests.Session()
_lock = threading.Lock()
_cache = {"at": 0.0, "times": {}}


def times(max_age=300):
    """{key: {"layer", "start", "end", "step_min"}} from GeoMet's per-layer capabilities (cached 5 min)."""
    with _lock:
        if time.time() - _cache["at"] < max_age and _cache["times"]:
            return _cache["times"]
    out = {}
    for key, layer in LAYERS.items():
        try:
            r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities",
                                          "layer": layer}, timeout=30)
            r.raise_for_status()
            m = re.search(r'<Dimension name="time"[^>]*>([^/<]+)/([^/<]+)/PT(\d+)M', r.text)
            if m:
                out[key] = {"layer": layer, "start": m[1], "end": m[2], "step_min": int(m[3])}
        except Exception as e:
            log.warning("GOES %s times unavailable: %s", layer, e)
    with _lock:
        if out:
            _cache.update(at=time.time(), times=out)
        return _cache["times"]
