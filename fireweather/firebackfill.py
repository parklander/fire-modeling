"""Backfill the fire log for an area from the NASA FIRMS archive.

    python -m fireweather.firebackfill shilo [--from 2000-01-01] [--sources LANDSAT,VIIRS_SNPP,...]

For each sensor it uses the standard-processing (SP) archive where it exists and near-real-time (NRT) data after
that, per FIRMS's data_availability, over the area's boundary box plus firelog.LOG_BUFFER_KM. Requests cover 5 days
(the API maximum) and are throttled against the MAP_KEY's transaction limit, leaving room for the live poller.
Progress is kept in data/firelog_backfill_<area>.json, so an interrupted run resumes where it stopped.

Sensors: Landsat 8/9 (30 m, from mid-2022, no FRP), VIIRS S-NPP (2012), NOAA-20 (2018), NOAA-21 (2023), MODIS (2000).
Industrial heat sources: SP rows carry FIRMS's own type (2 = static land source); NRT rows use the live persistent
cells (hotspots.persistent.json).
"""
import argparse
import csv
import io
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from . import config, firelog

log = logging.getLogger("firebackfill")

API = "https://firms.modaps.eosdis.nasa.gov"
SENSORS = {  # family -> (SP source or None, NRT source, sensor label)
    "LANDSAT": (None, "LANDSAT_NRT", "Landsat"),
    "VIIRS_SNPP": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT", "VIIRS N"),
    "VIIRS_NOAA20": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT", "VIIRS N20"),
    "VIIRS_NOAA21": ("VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT", "VIIRS N21"),
    "MODIS": ("MODIS_SP", "MODIS_NRT", "MODIS"),
}
DAYS = 5
TX_CEILING = 3000   # pause when the key has used this many transactions in the current 10 minutes

_s = requests.Session()


def _get(url, tries=5):
    for i in range(tries):
        try:
            r = _s.get(url, timeout=180)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.RequestException as e:
            wait = 30 * (i + 1)
            log.warning("FIRMS request failed (%s), retrying in %ds", e, wait)
            time.sleep(wait)
    raise RuntimeError("FIRMS archive unreachable")


def _throttle():
    try:
        st = _get(f"{API}/mapserver/mapkey_status/?MAP_KEY={config.FIRMS_MAP_KEY}").json()
    except Exception:
        time.sleep(10)
        return
    while st.get("current_transactions", 0) > TX_CEILING:
        log.info("MAP_KEY at %s transactions this 10 min; pausing", st["current_transactions"])
        time.sleep(60)
        st = _get(f"{API}/mapserver/mapkey_status/?MAP_KEY={config.FIRMS_MAP_KEY}").json()


def availability():
    r = _get(f"{API}/api/data_availability/csv/{config.FIRMS_MAP_KEY}/ALL")
    return {row["data_id"]: (date.fromisoformat(row["min_date"]), date.fromisoformat(row["max_date"]))
            for row in csv.DictReader(io.StringIO(r.text))}


def plan(start, families):
    """[(source, first day, last day)] covering each family: SP archive, then NRT after it."""
    av, out = availability(), []
    for fam in families:
        sp, nrt, _ = SENSORS[fam]
        sp_end = None
        if sp and sp in av:
            a, b = av[sp]
            if b >= start:
                out.append((sp, max(a, start), b))
            sp_end = b
        if nrt in av:
            a, b = av[nrt]
            a = max(a, start, sp_end + timedelta(days=1) if sp_end else a)
            if a <= b:
                out.append((nrt, a, b))
    return out


def _persistent_cells():
    try:
        return json.loads((config.HOTSPOT_DIR / "persistent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def parse(source, text, persistent):
    fam = next(f for f, v in SENSORS.items() if source in v[:2])
    label = SENSORS[fam][2]
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        t = row["acq_time"].zfill(4)
        when = datetime.strptime(f"{row['acq_date']} {t}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        lat, lon, conf = float(row["latitude"]), float(row["longitude"]), row.get("confidence", "")
        if fam == "LANDSAT":
            sensor, frp, fp = f"Landsat {row.get('satellite', '').lstrip('L')}".strip(), None, 15
            low = conf.upper() == "L"
        else:
            sat = row.get("satellite", "")
            sensor = f"MODIS {'Terra' if sat in ('T', 'Terra') else 'Aqua' if sat in ('A', 'Aqua') else sat}" if fam == "MODIS" else label
            frp = float(row["frp"] or 0)
            fp = round(1000 * math.sqrt(float(row["scan"]) * float(row["track"])) / 2)
            low = conf == "l" if fam.startswith("VIIRS") else (conf.isdigit() and int(conf) < 30)
        if "type" in row and row["type"] != "":
            pers = row["type"] == "2"
        else:
            pers = any(math.hypot((lat - p[0]) * 111.2, (lon - p[1]) * 111.32 * math.cos(math.radians(lat)))
                       <= config.PERSISTENT_BUFFER_KM for p in persistent)
        out.append({"id": f"{source}:{row['latitude']}:{row['longitude']}:{row['acq_date']}:{t}", "lat": lat, "lon": lon,
                    "time": when.isoformat(), "source": source, "sensor": sensor, "confidence": conf, "frp": frp,
                    "daynight": row.get("daynight"), "footprint_m": fp, "low_conf": bool(low), "persistent": bool(pers)})
    return out


def backfill(aoi_id, start, families):
    areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
    a = next((x for x in areas if x["id"] == aoi_id), None)
    if not a:
        sys.exit(f"unknown area {aoi_id!r}")
    ring = a.get("boundary")
    if ring:
        w, s, e, n = a.get("bbox") or (min(p[1] for p in ring), min(p[0] for p in ring),
                                      max(p[1] for p in ring), max(p[0] for p in ring))
        buf = firelog.LOG_BUFFER_KM
    else:
        w = e = a["lon"]; s = n = a["lat"]
        buf = firelog.LOG_RADIUS_KM
    dlat, dlon = buf / 111.2, buf / (111.32 * math.cos(math.radians((s + n) / 2)))
    box = f"{w - dlon:.4f},{s - dlat:.4f},{e + dlon:.4f},{n + dlat:.4f}"
    state_file = config.DATA_DIR / f"firelog_backfill_{aoi_id}.json"
    try:
        done = set(json.loads(state_file.read_text(encoding="utf-8"))["done"])
    except (OSError, ValueError, KeyError):
        done = set()
    persistent = _persistent_cells()
    jobs = plan(start, families)
    total = sum((b - a0).days // DAYS + 1 for _, a0, b in jobs)
    log.info("backfill %s over %s: %d requests planned (%s)", a["name"], box, total,
             ", ".join(f"{src} {a0}..{b}" for src, a0, b in jobs))
    k = added = found = 0
    for src, a0, b in jobs:
        d = a0
        while d <= b:
            key = f"{src}:{d}"
            k += 1
            if key not in done:
                if k % 25 == 1:
                    _throttle()
                r = _get(f"{API}/api/area/csv/{config.FIRMS_MAP_KEY}/{src}/{box}/{DAYS}/{d}")
                if r.status_code != 200 or not r.text.startswith("latitude"):
                    log.warning("%s %s: %s", src, d, r.text[:120].replace(config.FIRMS_MAP_KEY, "KEY"))
                else:
                    dets = parse(src, r.text, persistent)
                    found += len(dets)
                    added += firelog.record(dets, only=[aoi_id]) if dets else 0
                    done.add(key)
                    if k % 20 == 0 or dets:
                        state_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
                if k % 100 == 0:
                    log.info("%d/%d requests · %d detections in the box, %d new in the log", k, total, found, added)
            d += timedelta(days=DAYS)
    state_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
    log.info("backfill %s done: %d requests, %d detections in the box, %d new in the log", a["name"], k, found, added)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("aoi")
    ap.add_argument("--from", dest="start", default="2000-01-01")
    ap.add_argument("--sources", default=",".join(SENSORS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(config.ROOT / "logs" / "firelog-backfill.log")])
    backfill(args.aoi, date.fromisoformat(args.start), [s.strip().upper() for s in args.sources.split(",")])


if __name__ == "__main__":
    main()
