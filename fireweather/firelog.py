"""Permanent log of satellite fire detections at the areas of interest, for post-fire review and playback.

After every FIRMS poll, each detection (persistent and low-confidence ones too, flagged) that falls inside an area's
drawn boundary, or within LOG_BUFFER_KM of it, is stored in data/fire_log.sqlite keyed by (detection id, area). Areas
without a boundary log detections within LOG_RADIUS_KM of their point. FIRMS only returns the last couple of days, so
this log is what keeps the history.

episodes(aoi) groups the logged days into fire episodes (detections no more than EPISODE_GAP_DAYS apart);
passes(aoi, start, end) returns detections grouped into satellite passes for the progression player.
"""
import json
import logging
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config
from .firealerts import inside

log = logging.getLogger("firelog")

DB = config.DATA_DIR / "fire_log.sqlite"
LOG_BUFFER_KM = 10.0   # around a drawn boundary: catches fires approaching the area
LOG_RADIUS_KM = 25.0   # areas without a boundary
EPISODE_GAP_DAYS = 2

_lock = threading.Lock()


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS detections (
        id TEXT NOT NULL, aoi TEXT NOT NULL, time TEXT NOT NULL, lat REAL, lon REAL, frp REAL, sensor TEXT,
        source TEXT, confidence TEXT, daynight TEXT, footprint_m INTEGER, low_conf INTEGER, persistent INTEGER,
        inside INTEGER, dist_km REAL, logged_at TEXT, PRIMARY KEY (id, aoi))""")
    db.execute("CREATE INDEX IF NOT EXISTS det_aoi_time ON detections (aoi, time)")
    return db


def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lat2 - lat1) * 111.2, (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2)))


def record(detections, only=None):
    """Log detections at the areas of interest (or just the area ids in only). Returns the number of new rows."""
    areas = [a for a in json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"] if only is None or a["id"] in only]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for a in areas:
        ring = a.get("boundary")
        if ring and len(ring) >= 3:
            w, s, e, n = a.get("bbox") or (min(p[1] for p in ring), min(p[0] for p in ring),
                                          max(p[1] for p in ring), max(p[0] for p in ring))
            dlat = LOG_BUFFER_KM / 111.2
            dlon = LOG_BUFFER_KM / (111.32 * math.cos(math.radians((s + n) / 2)))
            box = (w - dlon, s - dlat, e + dlon, n + dlat)
        else:
            ring = None
            dlat = LOG_RADIUS_KM / 111.2
            dlon = LOG_RADIUS_KM / (111.32 * math.cos(math.radians(a["lat"])))
            box = (a["lon"] - dlon, a["lat"] - dlat, a["lon"] + dlon, a["lat"] + dlat)
        for d in detections:
            if not (box[1] <= d["lat"] <= box[3] and box[0] <= d["lon"] <= box[2]):
                continue
            dist = _km(a["lat"], a["lon"], d["lat"], d["lon"])
            if ring is None and dist > LOG_RADIUS_KM:
                continue
            rows.append((d["id"], a["id"], d["time"], d["lat"], d["lon"], d["frp"], d["sensor"], d.get("source"),
                         d.get("confidence"), d.get("daynight"), d["footprint_m"], int(d["low_conf"]),
                         int(d["persistent"]), int(bool(ring) and inside(d["lat"], d["lon"], ring)), round(dist, 2), now))
    if not rows:
        return 0
    with _lock:
        db = connect()
        try:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.commit()
            added = db.total_changes - before
        finally:
            db.close()
    if added:
        log.info("fire log: %d new detection(s) at areas of interest", added)
    return added


def _where(inside_only, include_persistent):
    w = ""
    if inside_only:
        w += " AND inside = 1"
    if not include_persistent:
        w += " AND persistent = 0"
    return w


def episodes(aoi, inside_only=False, include_persistent=False):
    """Fire episodes at an area, newest first: runs of days with detections, no more than EPISODE_GAP_DAYS apart."""
    db = connect()
    try:
        rows = db.execute(f"""SELECT time, frp, inside, low_conf, sensor FROM detections WHERE aoi = ?
                              {_where(inside_only, include_persistent)} ORDER BY time""", (aoi,)).fetchall()
    finally:
        db.close()
    eps = []
    for r in rows:
        t = datetime.fromisoformat(r["time"])
        if not eps or t - eps[-1]["_last"] > timedelta(days=EPISODE_GAP_DAYS):
            eps.append({"start": r["time"], "_last": t, "count": 0, "inside": 0, "low_conf": 0, "max_frp": 0.0, "sensors": set()})
        e = eps[-1]
        e["_last"], e["end"] = t, r["time"]
        e["count"] += 1
        e["inside"] += r["inside"]
        e["low_conf"] += r["low_conf"]
        e["max_frp"] = max(e["max_frp"], r["frp"] or 0)
        e["sensors"].add(r["sensor"].split()[0])
    for e in eps:
        del e["_last"]
        e["sensors"] = sorted(e["sensors"])
    return eps[::-1]


def passes(aoi, start, end, inside_only=False, include_persistent=False, include_low_conf=True):
    """Detections at an area between start and end (ISO), grouped into satellite passes."""
    from .hotspots import _group_passes
    where = _where(inside_only, include_persistent) + ("" if include_low_conf else " AND low_conf = 0")
    db = connect()
    try:
        rows = db.execute(f"""SELECT * FROM detections WHERE aoi = ? AND time >= ? AND time <= ? {where}
                              ORDER BY time""", (aoi, start, end)).fetchall()
    finally:
        db.close()
    hist = [[r["id"], r["time"], r["lat"], r["lon"], r["frp"], r["sensor"], r["footprint_m"],
             {"inside": bool(r["inside"]), "low_conf": bool(r["low_conf"]), "persistent": bool(r["persistent"])}]
            for r in rows]
    out = []
    for p in _group_passes(hist):
        frps = [d["frp"] or 0 for d in p["detections"]]  # Landsat has no FRP
        out.append({"time": p["time"], "sensor": p["sensor"], "count": len(frps), "frp_sum": round(sum(frps), 1),
                    "frp_max": round(max(frps), 1), "detections": p["detections"]})
    return out
