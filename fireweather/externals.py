"""Observations and products from outside the model, for burn reports.

- Surface observations: MSC SWOB real-time (api.weather.gc.ca, collection swob-realtime)
- AQHI observations / forecasts and stations (aqhi-*-realtime, aqhi-stations)
- Active alerts incl. Special Air Quality Statements (weather-alerts)
- CWFIS daily station noon observations + FWI System codes (cwfis.cfs.nrcan.gc.ca)

Every call is best-effort: failures return None / [] and the report says "unavailable".
"""
import csv
import io
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("externals")

API = "https://api.weather.gc.ca/collections"
CWFIS = "https://cwfis.cfs.nrcan.gc.ca/downloads/fwi_obs"
_session = requests.Session()
_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as e:
        log.warning("%s unavailable: %s", key, e)
        val = None
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _get_json(url, params):
    r = _session.get(url, params={"f": "json", **params}, timeout=30)
    r.raise_for_status()
    return r.json()


def km_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


# ---- SWOB observations -----------------------------------------------------------
def latest_obs(lat, lon, radius_deg=1.0, max_km=100):
    """Most recent observation from the nearest reporting SWOB station (within max_km)."""
    def fetch():
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = _get_json(f"{API}/swob-realtime/items", {
            "bbox": f"{lon - radius_deg},{lat - radius_deg * 0.7},{lon + radius_deg},{lat + radius_deg * 0.7}",
            "datetime": f"{since}/..", "sortby": "-date_tm-value", "limit": 500})
        best = {}
        for f in d.get("features", []):
            p = f["properties"]
            stn = p.get("msc_id-value") or p.get("stn_nam-value")
            if stn not in best:  # sorted newest first
                x, y = f["geometry"]["coordinates"][:2]
                best[stn] = (km_between(lat, lon, y, x), p)
        if not best:
            return None
        dist, p = min(best.values(), key=lambda t: t[0])
        if dist > max_km:
            return None
        g = lambda *keys: next((p[k] for k in keys if p.get(k) is not None), None)
        return {
            "station": (p.get("stn_nam-value") or "").replace("+", " "), "distance_km": round(dist, 1),
            "time": p.get("date_tm-value"),
            "temp": g("air_temp"), "rh": g("rel_hum"),
            "wspd": g("avg_wnd_spd_10m_pst10mts", "avg_wnd_spd_10m_pst2mts", "avg_wnd_spd_10m_pst1mt"),
            "wdir": g("avg_wnd_dir_10m_pst10mts", "avg_wnd_dir_10m_pst2mts", "avg_wnd_dir_10m_pst1mt"),
            "gust": g("max_wnd_spd_10m_pst10mts", "max_wnd_spd_10m_pst1hr", "max_wnd_spd_10m_pst1mt"),
            "precip_1h": g("pcpn_amt_pst1hr"),
        }
    return _cached(f"obs:{lat:.2f},{lon:.2f}", 300, fetch)


# ---- AQHI ------------------------------------------------------------------------
def aqhi(lat, lon, max_km=150):
    stations = _cached("aqhi-stations", 86400, lambda: _get_json(f"{API}/aqhi-stations/items", {"limit": 2000}))
    if not stations:
        return None
    near = min(((km_between(lat, lon, f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]), f["properties"])
                for f in stations["features"]), key=lambda t: t[0], default=None)
    if not near or near[0] > max_km:
        return None
    dist, st = near
    loc = st["location_id"]

    def fetch():
        obs = _get_json(f"{API}/aqhi-observations-realtime/items",
                        {"location_id": loc, "sortby": "-observation_datetime", "limit": 1})["features"]
        fc = _get_json(f"{API}/aqhi-forecasts-realtime/items",
                       {"location_id": loc, "sortby": "-publication_datetime", "limit": 72})["features"]
        latest_pub = fc[0]["properties"]["publication_datetime"] if fc else None
        # skip period-summary records, which carry no forecast_datetime / aqhi
        fc = sorted((f["properties"] for f in fc
                     if f["properties"]["publication_datetime"] == latest_pub and "forecast_datetime" in f["properties"]),
                    key=lambda p: p["forecast_datetime"])
        return {
            "location": st["location_name_en"], "distance_km": round(dist, 1),
            "obs": obs[0]["properties"]["aqhi"] if obs else None,
            "obs_time": obs[0]["properties"]["observation_datetime"] if obs else None,
            "forecast": [{"time": p["forecast_datetime"], "aqhi": p["aqhi"]} for p in fc],
            "forecast_issued": latest_pub,
        }
    return _cached(f"aqhi:{loc}", 600, fetch)


def aqhi_risk(v):
    if v is None:
        return None
    return "Low" if v <= 3 else "Moderate" if v <= 6 else "High" if v <= 10 else "Very high"


# ---- alerts ----------------------------------------------------------------------
AIR_QUALITY_WORDS = ("air quality", "smoke", "qualité de l'air")


def alert_page(lat, lon):
    """weather.gc.ca location page for a point: its forecast with any alerts in effect at the top."""
    return f"https://weather.gc.ca/en/location/index.html?coords={lat:.3f},{lon:.3f}"


def alerts(lat, lon, strict=False):
    """Active alerts whose area covers the point (tiny bbox intersect). With strict, a failed request raises
    instead of looking like "no alerts" (the alert poller then keeps what it last saw)."""
    def fetch():
        d = _get_json(f"{API}/weather-alerts/items",
                      {"bbox": f"{lon - 0.01},{lat - 0.01},{lon + 0.01},{lat + 0.01}", "limit": 100})
        now = datetime.now(timezone.utc).isoformat()
        out, seen = [], set()
        for f in d.get("features", []):
            p = f["properties"]
            if (p.get("expiration_datetime") or "9") < now:
                continue
            key = (p.get("alert_code"), p.get("alert_name_en"))
            if key in seen:
                continue
            seen.add(key)
            name = p.get("alert_name_en") or ""
            out.append({
                "name": name, "type": p.get("alert_type"), "issued": p.get("publication_datetime"),
                "expires": p.get("expiration_datetime"),
                "air_quality": any(w in name.lower() for w in AIR_QUALITY_WORDS)
                or p.get("alert_code") in ("SAQS", "AQ"),
                "text": (p.get("alert_text_en") or "").strip(),
                "area": p.get("feature_name_en"),
                # GeoMet gives no link; ECCC's location page for the point shows the alerts in effect there
                "link": alert_page(lat, lon),
            })
        return out
    v = _cached(f"alerts:{lat:.2f},{lon:.2f}", 300, fetch)
    if v is None and strict:
        raise RuntimeError("GeoMet weather-alerts unavailable")
    return v or []


# ---- CWFIS station FWI ------------------------------------------------------------
def _cwfis_stations():
    year = datetime.now(timezone.utc).year
    r = _session.get(f"{CWFIS}/cwfis_allstn{year}.csv", timeout=60)
    r.raise_for_status()
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            out[row["aes"].strip()] = (float(row["lat"]), float(row["lon"]))
        except (KeyError, ValueError):
            pass
    return out


def _cwfis_daily():
    """Latest substantially complete daily station file -> (date, rows).

    Today's file is published early and fills in through the day, so a file only
    counts once it has at least 70% as many stations as the fullest recent one.
    """
    today = datetime.now(timezone.utc).date()
    files = []
    for back in range(0, 4):
        d = today - timedelta(days=back)
        r = _session.get(f"{CWFIS}/current/cwfis_fwi_{d:%Y%m%d}.csv", timeout=60)
        if r.status_code != 200:
            continue
        rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k}
                for row in csv.DictReader(io.StringIO(r.text))]
        files.append((d, rows))
    if not files:
        return None
    most = max(len(rows) for _, rows in files)
    for d, rows in files:  # newest first
        if rows and len(rows) >= 0.7 * most:
            return d.isoformat(), rows
    return None


def cwfis_station_codes():
    """All CWFIS stations in the latest daily file with coordinates and FFMC/DMC/DC.

    -> {"file_date": str, "stations": [{"name", "lat", "lon", "ffmc", "dmc", "dc"}]} or None.
    """
    stations = _cached("cwfis-stations", 86400, _cwfis_stations)
    daily = _cached("cwfis-daily", 1800, _cwfis_daily)
    if not stations or not daily:
        return None
    num = lambda v: None if v in ("", "NULL") else float(v)
    out = []
    for row in daily[1]:
        ll = stations.get(row.get("AES", ""))
        if ll:
            out.append({"name": row["NAME"].replace("+", " "), "lat": ll[0], "lon": ll[1],
                        **{k.lower(): num(row.get(k, "")) for k in ("FFMC", "DMC", "DC")}})
    return {"file_date": daily[0], "stations": out}


def cwfis_stations_near(lat, lon, n=3, max_km=120):
    stations = _cached("cwfis-stations", 86400, _cwfis_stations)
    daily = _cached("cwfis-daily", 1800, _cwfis_daily)
    if not stations or not daily:
        return None
    date, rows = daily
    num = lambda v: None if v in ("", "NULL") else float(v)
    found = []
    for row in rows:
        ll = stations.get(row.get("AES", ""))
        if not ll:
            continue
        dist = km_between(lat, lon, *ll)
        if dist <= max_km:
            found.append({
                "station": row["NAME"].replace("+", " "), "agency": row.get("AGENCY"), "distance_km": round(dist, 1),
                "date": row.get("REPDATE"),
                **{k.lower(): num(row.get(k, "")) for k in
                   ("TEMP", "RH", "WS", "WDIR", "PRECIP", "FFMC", "DMC", "DC", "ISI", "BUI", "FWI", "DSR")},
            })
    return {"file_date": date, "stations": sorted(found, key=lambda s: s["distance_km"])[:n]}
