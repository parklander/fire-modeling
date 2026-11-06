"""Official CWFIS fire weather maps (NRCan GeoServer WMS), for the weather map's "CWFIS (official)" layers.

Daily FWI System and FBP grids at noon local standard time: interpolated weather-station observations for past
days, CWFIS's own forecast for today, and the NAEFS ensemble forecast after that (about 12 days ahead). Each layer
is time-enabled (one image per day); the page asks here for each layer's date range and legend classes.
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("cwfis")

WMS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wms"
LAYERS = ["fwi", "isi", "ffmc", "bui", "dmc", "dc", "fdr", "hfi"]
_session = requests.Session()
_lock = threading.Lock()
_cache = {"at": 0.0, "info": {}}


def _legend(layer):
    """[{"label", "color"}] from GeoServer's JSON legend; English half of bilingual labels."""
    r = _session.get(WMS.replace("/wms", "/ows"), params={"service": "WMS", "version": "1.3.0", "request": "GetLegendGraphic",
                                                          "format": "application/json", "layer": layer}, timeout=30)
    r.raise_for_status()
    entries = r.json()["Legend"][0]["rules"][0]["symbolizers"][0]["Raster"]["colormap"]["entries"]
    return [{"label": e["label"].split(" / ")[0].strip(), "color": e["color"]} for e in entries]


def info(max_age=3600):
    """{layer: {"layer", "title", "start", "end", "legend"}} (cached an hour; CWFIS updates daily)."""
    with _lock:
        if time.time() - _cache["at"] < max_age and _cache["info"]:
            return _cache["info"]
    out = {}
    try:
        r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}, timeout=60)
        r.raise_for_status()
        caps = r.text
        with ThreadPoolExecutor(4) as ex:
            legends = dict(zip(LAYERS, ex.map(lambda l: _safe(_legend, l), LAYERS)))
        for layer in LAYERS:
            m = re.search(rf"<Name>{layer}</Name>\s*<Title>([^<]*)</Title>.*?<Dimension name=\"time\"[^>]*>([^/<]+)/([^/<]+)/P1D",
                          caps, re.S)
            if m:
                out[layer] = {"layer": layer, "title": m[1], "start": m[2][:10], "end": m[3][:10], "legend": legends[layer] or []}
    except Exception as e:
        log.warning("CWFIS layers unavailable: %s", e)
    with _lock:
        if out:
            _cache.update(at=time.time(), info=out)
        return _cache["info"]


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.warning("CWFIS legend %s unavailable: %s", a, e)
        return None
