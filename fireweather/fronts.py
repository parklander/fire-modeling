"""Surface fronts and pressure centres from the NOAA Weather Prediction Center, for the synoptic overlay.

- Analysis: WPC's coded surface bulletin (high-resolution version, positions to 0.1°), issued every 3 h:
  https://tgftp.nws.noaa.gov/data/raw/as/asus02.kwbc.cod.sus.txt
  Coordinates are LLLOOOO: latitude and west longitude in tenths of a degree (4390713 = 43.9 N, 71.3 W).
- Forecast: WPC National Forecast Chart map service, day 1-3 fronts and highs/lows. Days 2-3 are valid
  "Morning" (12 UTC); day 1 gives only a date, so it is placed at 18 UTC and labelled as approximate.

Front points are ordered so the symbols go on the left of the direction of travel (the WPC convention, as in
MetPy's bulletin plots). Fronts are drawn by forecasters; models don't produce them.
"""
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger("fronts")

BULLETIN = "https://tgftp.nws.noaa.gov/data/raw/as/asus02.kwbc.cod.sus.txt"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"  # NWS text product archive
NFC = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/natl_fcst_wx_chart/MapServer"
NFC_DAYS = {1: (1, 2), 2: (13, 14), 3: (25, 26)}  # day -> (highs/lows layer, fronts layer)
KINDS = {"COLD": "cold", "WARM": "warm", "STNRY": "stationary", "OCFNT": "occluded", "TROF": "trough",
         "DRYLINE": "dryline", "SQLN": "squall"}
FEAT_KIND = {"Cold Front": "cold", "Warm Front": "warm", "Stationary Front": "stationary",
             "Occluded Front": "occluded", "Trough": "trough", "Dry Line": "dryline", "Squall Line": "squall"}
_session = requests.Session()


def _pt(code):
    """LLLOOOO -> (lat, lon). West longitudes are negative; values over 180 W wrap to the east."""
    lat, lon = int(code[:3]) / 10, int(code[3:]) / 10
    return lat, (-lon if lon <= 180 else 360 - lon)


def parse_bulletin(text):
    """Highs, lows and fronts from a high-resolution coded surface bulletin."""
    m = re.search(r"VALID\s+(\d{2})(\d{2})(\d{2})Z", text)
    issued = datetime.now(timezone.utc)
    valid = None
    if m:
        mon, day, hh = int(m[1]), int(m[2]), int(m[3])
        year = issued.year - (1 if mon > issued.month + 6 else 0)  # December bulletin read in January
        valid = datetime(year, mon, day, hh, tzinfo=timezone.utc)
    # join continuation lines onto their keyword line
    entries, cur = [], None
    for line in text.splitlines():
        tok = line.split()
        if not tok:
            continue
        if tok[0] in ("HIGHS", "LOWS") or tok[0] in KINDS:
            cur = [tok[0], *tok[1:]]
            entries.append(cur)
        elif cur is not None and all(t.isdigit() for t in tok):
            cur.extend(tok)
        else:
            cur = None
    highs, lows, fronts = [], [], []
    for kw, *vals in entries:
        if kw in ("HIGHS", "LOWS"):
            for p, c in zip(vals[0::2], vals[1::2]):
                lat, lon = _pt(c)
                (highs if kw == "HIGHS" else lows).append({"lat": lat, "lon": lon, "hpa": int(p)})
        else:
            pts = [_pt(c) for c in vals if c.isdigit() and len(c) >= 6]
            if len(pts) >= 2:
                fronts.append({"kind": KINDS[kw], "points": pts})
    return {"valid": valid.isoformat() if valid else None, "highs": highs, "lows": lows, "fronts": fronts}


def _nfc_valid(popup):
    """'Cold Front Valid: Thu Morning Oct 29 2026' -> (datetime, approximate?)"""
    m = re.search(r"Valid:\s*\w+\s+(Morning|Afternoon|Evening|Night)?\s*(\w{3})\s+(\d+)\s+(\d{4})", popup or "")
    if not m:
        return None, True
    d = datetime.strptime(f"{m[2]} {m[3]} {m[4]}", "%b %d %Y").replace(tzinfo=timezone.utc)
    part = m[1]
    hour = {"Morning": 12, "Afternoon": 18, "Evening": 24, "Night": 30}.get(part, 18)
    return d + timedelta(hours=hour), part is None


def _query(layer):
    r = _session.get(f"{NFC}/{layer}/query", params={"where": "1=1", "outFields": "feat,popupconte",
                                                    "returnGeometry": "true", "outSR": "4326", "f": "json"}, timeout=40)
    r.raise_for_status()
    return r.json().get("features", [])


def forecast_day(day):
    hl_layer, fr_layer = NFC_DAYS[day]
    highs, lows, fronts, valid, approx = [], [], [], None, True
    for f in _query(hl_layer):
        g, a = f.get("geometry") or {}, f["attributes"]
        if "x" not in g:
            continue
        valid, approx = _nfc_valid(a.get("popupconte"))
        (highs if a.get("feat", "").startswith("High") else lows).append(
            {"lat": round(g["y"], 2), "lon": round(g["x"], 2), "hpa": None})
    for f in _query(fr_layer):
        g, a = f.get("geometry") or {}, f["attributes"]
        kind = next((k for name, k in FEAT_KIND.items() if a.get("feat", "").startswith(name)), "trough")
        if valid is None:
            valid, approx = _nfc_valid(a.get("popupconte"))
        for path in g.get("paths", []):
            if len(path) >= 2:
                fronts.append({"kind": kind, "points": [(round(y, 2), round(x, 2)) for x, y in path]})
    return {"day": day, "valid": valid.isoformat() if valid else None, "approx_time": approx,
            "highs": highs, "lows": lows, "fronts": fronts}


def recent_analyses(limit=24):
    """Past high-resolution bulletins (ASUS02) from the Iowa Environmental Mesonet text archive:
    {valid ISO: analysis}. WPC's own server keeps only the latest one."""
    r = _session.get(IEM, params={"pil": "CODSUS", "limit": limit, "fmt": "text"}, timeout=40)
    r.raise_for_status()
    out = {}
    for block in re.split(r"(?m)^(?=ASUS0\d KWBC)", r.text):
        if block.startswith("ASUS02"):
            a = parse_bulletin(block)
            if a["valid"]:
                out[a["valid"]] = a | {"source": "WPC surface analysis"}
    return out


def history_view(fr):
    with fr.lock:
        return list(fr.history.values())


class Fronts:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"analysis": None, "forecast": [], "checked": None, "error": None}
        self.history = {}  # valid time (ISO) -> analysis, oldest first

    def poll(self):
        out, errs = {"analysis": None, "forecast": []}, []
        try:
            r = _session.get(BULLETIN, timeout=30)
            r.raise_for_status()
            out["analysis"] = parse_bulletin(r.text) | {"source": "WPC surface analysis"}
        except Exception as e:
            errs.append(f"analysis: {e}")
        try:  # earlier analyses (to measure how fronts are moving)
            hist = recent_analyses()
            if out["analysis"]:
                hist[out["analysis"]["valid"]] = out["analysis"]
            with self.lock:
                self.history = dict(sorted(hist.items())[-config.FRONTS_HISTORY:])
        except Exception as e:
            errs.append(f"history: {e}")
        for day in NFC_DAYS:
            try:
                out["forecast"].append(forecast_day(day) | {"source": f"WPC day {day} forecast chart"})
            except Exception as e:
                errs.append(f"day {day}: {e}")
        with self.lock:
            prev = self.data
            self.data = {"analysis": out["analysis"] or prev["analysis"],
                         "forecast": out["forecast"] or prev["forecast"],
                         "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "error": "; ".join(errs) or None}
        if errs:
            log.warning("WPC fronts: %s", "; ".join(errs))
        a = self.data["analysis"]
        log.info("WPC fronts: analysis %s (%d fronts), %d forecast days", a and a["valid"],
                 len(a["fronts"]) if a else 0, len(self.data["forecast"]))

    def view(self):
        with self.lock:
            return self.data

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("WPC fronts poll failed")
            stop.wait(config.FRONTS_POLL_MIN * 60)
