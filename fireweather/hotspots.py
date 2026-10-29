"""Satellite active-fire detections (NASA FIRMS LANCE NRT) -> incidents -> automatic growth runs.

- poll(): every FIRMS_POLL_MIN minutes, fetch VIIRS (SNPP, NOAA-20, NOAA-21) and MODIS
  detections for Canada over the last FIRMS_DAYS days.
- Persistent heat sources (refineries, flares, smelters) are learned once a day from 10
  days of VIIRS: 375 m cells seen on PERSISTENT_MIN_DAYS or more distinct days, plus a
  PERSISTENT_BUFFER_KM buffer, are excluded.
- Remaining detections (not low confidence) are clustered into incidents: detections within
  CLUSTER_KM of each other and CLUSTER_HOURS in time. Incidents keep a stable id.
- Incidents within INCIDENT_BASE_KM of an area of interest are simulated automatically with
  firemodel.simulate, starting from the detection footprints as the burned area at the
  latest pass, forecasting to AUTO_SIM_HOURS_AHEAD past now. They re-run when new
  detections join or a newer complete RDPS run is available.
"""
import csv
import hashlib
import io
import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

from . import config

log = logging.getLogger("hotspots")
_session = requests.Session()


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(a))


def _fetch(source, day_range, start=None):
    if not config.FIRMS_MAP_KEY:
        raise RuntimeError("FIRMS_MAP_KEY isn't set (add it to .env)")
    w, s, e, n = config.FIRMS_BBOX
    url = f"{config.FIRMS_API}/{config.FIRMS_MAP_KEY}/{source}/{w},{s},{e},{n}/{day_range}"
    if start:
        url += f"/{start.isoformat()}"
    r = _session.get(url, timeout=180)
    r.raise_for_status()
    if not r.text.startswith("latitude"):
        raise RuntimeError(f"FIRMS {source}: {r.text[:120]}")
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        t = row["acq_time"].zfill(4)
        when = datetime.strptime(f"{row['acq_date']} {t}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        viirs = row["instrument"] == "VIIRS"
        conf = row["confidence"]
        out.append({
            "id": f"{source}:{row['latitude']}:{row['longitude']}:{row['acq_date']}:{t}",
            "lat": float(row["latitude"]), "lon": float(row["longitude"]), "time": when.isoformat(),
            "source": source, "sensor": f"{row['instrument']} {row['satellite']}",
            "confidence": conf, "frp": float(row["frp"] or 0), "daynight": row["daynight"],
            # VIIRS 375 m pixels; MODIS 1 km (scan/track grow toward the swath edge)
            "footprint_m": round(1000 * math.sqrt(float(row["scan"]) * float(row["track"])) / 2),
            "low_conf": conf == "l" if viirs else (conf.isdigit() and int(conf) < 30),
        })
    return out


def _group_passes(history):
    """Detections [id, time, lat, lon, frp, sensor, footprint_m, (flags)] -> satellite passes (same sensor, within 10 min)."""
    passes = []
    for h in history:
        _id, t, lat, lon, frp, sensor, fp = h[:7]
        ts = datetime.fromisoformat(t)
        p = next((p for p in passes if p["sensor"] == sensor and abs((ts - p["_t"]).total_seconds()) <= 600), None)
        if not p:
            p = {"sensor": sensor, "time": t, "_t": ts, "detections": []}
            passes.append(p)
        p["detections"].append({"lat": lat, "lon": lon, "frp": frp, "footprint_m": fp} | (h[7] if len(h) > 7 else {}))
    return sorted(passes, key=lambda p: p["_t"])


class Hotspots:
    def __init__(self, store, simulate):
        self.store = store
        self.simulate = simulate          # callable(run, ignitions, start_utc, hours, center) -> result
        self.lock = threading.Lock()
        self.detections = []
        self.persistent = np.zeros((0, 2))
        self.persistent_day = None
        self.incidents = {}               # id -> incident dict
        self.status = {"last_poll": None, "error": None}
        config.HOTSPOT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            saved = json.loads((config.HOTSPOT_DIR / "incidents.json").read_text(encoding="utf-8"))
            self.incidents = {i["id"]: i for i in saved}
        except Exception:
            pass

    # ---- persistent heat sources -------------------------------------------------------
    def _refresh_persistent(self):
        today = datetime.now(timezone.utc).date()
        if self.persistent_day == today:
            return
        cells = {}
        for src in config.FIRMS_SOURCES:
            if not src.startswith("VIIRS"):
                continue
            for start in (today - timedelta(days=9), today - timedelta(days=4)):
                for d in _fetch(src, 5, start):
                    key = (round(d["lat"] / 0.0035), round(d["lon"] / 0.0055))  # ~375 m cells
                    cells.setdefault(key, set()).add(d["time"][:10])
        pts = [(k[0] * 0.0035, k[1] * 0.0055) for k, days in cells.items() if len(days) >= config.PERSISTENT_MIN_DAYS]
        self.persistent = np.array(pts) if pts else np.zeros((0, 2))
        self.persistent_day = today
        (config.HOTSPOT_DIR / "persistent.json").write_text(json.dumps(pts), encoding="utf-8")
        log.info("persistent heat sources: %d cells", len(pts))

    def _is_persistent(self, lat, lon):
        if not len(self.persistent):
            return np.zeros(np.shape(lat), dtype=bool)
        lat, lon = np.atleast_1d(lat), np.atleast_1d(lon)
        out = np.zeros(lat.shape, dtype=bool)
        for i in range(0, len(lat), 2000):
            d = _km(lat[i:i + 2000, None], lon[i:i + 2000, None], self.persistent[None, :, 0], self.persistent[None, :, 1])
            out[i:i + 2000] = d.min(axis=1) <= config.PERSISTENT_BUFFER_KM
        return out

    # ---- polling and clustering --------------------------------------------------------
    def poll(self):
        try:
            self._refresh_persistent()
        except Exception as e:
            log.warning("persistent-source refresh failed: %s", e)
        dets = []
        for src in config.FIRMS_SOURCES:
            try:
                dets += _fetch(src, config.FIRMS_DAYS)
            except Exception as e:
                log.warning("FIRMS %s failed: %s", src, e)
                self.status["error"] = str(e)
        if not dets:
            return
        pers = self._is_persistent(np.array([d["lat"] for d in dets]), np.array([d["lon"] for d in dets]))
        for d, p in zip(dets, pers):
            d["persistent"] = bool(p)
        with self.lock:
            self.detections = dets
            self._cluster()
            self.status.update(last_poll=datetime.now(timezone.utc).isoformat(), error=None,
                               count=len(dets), persistent=int(pers.sum()))
        log.info("FIRMS: %d detections (%d persistent), %d incidents", len(dets), int(pers.sum()), len(self.incidents))

    def _cluster(self):
        """Group usable detections into incidents (single-linkage within CLUSTER_KM/CLUSTER_HOURS)."""
        areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
        use = [d for d in self.detections if not d["persistent"] and not d["low_conf"]]
        if not use:
            return
        lat = np.array([d["lat"] for d in use]); lon = np.array([d["lon"] for d in use])
        t = np.array([datetime.fromisoformat(d["time"]).timestamp() for d in use]) / 3600
        parent = list(range(len(use)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        # sort by longitude so each detection only checks a short window of neighbours
        order = np.argsort(lon)
        dlon = config.CLUSTER_KM / (111.32 * math.cos(math.radians(72)))  # wide enough for the far north
        for a_i, a in enumerate(order):
            for b in order[a_i + 1:]:
                if lon[b] - lon[a] > dlon:
                    break
                if abs(lat[b] - lat[a]) * 111.32 <= config.CLUSTER_KM and abs(t[b] - t[a]) <= config.CLUSTER_HOURS \
                        and _km(lat[a], lon[a], lat[b], lon[b]) <= config.CLUSTER_KM:
                    parent[find(a)] = find(b)
        groups = {}
        for i in range(len(use)):
            groups.setdefault(find(i), []).append(use[i])
        alat = np.array([a["lat"] for a in areas]); alon = np.array([a["lon"] for a in areas])
        new = {}
        for members in groups.values():
            members.sort(key=lambda d: d["time"])
            ids = {d["id"] for d in members}
            # keep the id of an existing incident that shares detections, else a hash of the first
            prevs = [inc for inc in self.incidents.values() if ids & set(inc["detection_ids"])]
            prev = prevs[0] if prevs else None
            iid = prev["id"] if prev else hashlib.sha1(members[0]["id"].encode()).hexdigest()[:10]
            # every detection the incident has had, including passes older than the FIRMS window (for progression)
            hist = {h[0]: h for p in prevs for h in p.get("history", [])}
            for d in members:
                hist[d["id"]] = [d["id"], d["time"], d["lat"], d["lon"], round(d["frp"], 1), d["sensor"], d["footprint_m"]]
            history = sorted(hist.values(), key=lambda h: h[1])
            clat = float(np.mean([d["lat"] for d in members])); clon = float(np.mean([d["lon"] for d in members]))
            dist = _km(clat, clon, alat, alon)
            k = int(np.argmin(dist))
            inc = {
                "id": iid, "lat": round(clat, 5), "lon": round(clon, 5),
                "first_seen": history[0][1], "last_seen": members[-1]["time"],
                "detections": len(members), "detection_ids": sorted(ids),
                "max_frp": round(max(d["frp"] for d in members), 1),
                "sensors": sorted({d["sensor"] for d in members}),
                "nearest_area": areas[k]["name"], "nearest_area_id": areas[k]["id"], "distance_km": round(float(dist[k]), 1),
                "near_base": bool(dist[k] <= config.INCIDENT_BASE_KM),
                "footprints": [[d["lat"], d["lon"], d["footprint_m"]] for d in members],
                "history": history,
            }
            if prev:  # carry simulation bookkeeping forward
                for key in ("sim", "sim_run", "sim_detections", "sim_error", "sim_at"):
                    if key in prev:
                        inc[key] = prev[key]
            new[iid] = inc
        self.incidents = new
        self._save()

    def _save(self):
        tmp = config.HOTSPOT_DIR / "incidents.tmp"
        tmp.write_text(json.dumps(list(self.incidents.values())), encoding="utf-8")
        tmp.replace(config.HOTSPOT_DIR / "incidents.json")

    # ---- automatic simulation ----------------------------------------------------------
    def latest_run(self):
        runs = [r for r in self.store.summary() if len(r["hours"]) >= 49 and len(r["index_hours"]) >= 1]
        return runs[0]["run"] if runs else None

    @staticmethod
    def _fresh(inc):
        """Detected recently enough to assume it may still be burning."""
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(inc["last_seen"])).total_seconds() / 3600
        return age_h <= config.AUTO_SIM_MAX_AGE_H

    def auto_simulate(self):
        run = self.latest_run()
        if not run:
            return
        with self.lock:
            todo = [i for i in self.incidents.values() if i["near_base"] and self._fresh(i)
                    and (i.get("sim_run") != run or i.get("sim_detections") != i["detections"])]
        for inc in todo:
            self.run_incident(inc["id"], run)

    def run_incident(self, iid, run=None):
        run = run or self.latest_run()
        with self.lock:
            inc = dict(self.incidents.get(iid) or {})
        if not inc or not run:
            raise LookupError("unknown incident or no model run")
        run_t0 = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        last = datetime.fromisoformat(inc["last_seen"])
        start = max(last.replace(minute=0, second=0, microsecond=0), run_t0)
        end = datetime.now(timezone.utc) + timedelta(hours=config.AUTO_SIM_HOURS_AHEAD)
        hours = max(3, min(int(math.ceil((end - start).total_seconds() / 3600)), 36, 84 - int((start - run_t0).total_seconds() // 3600)))
        # footprints seen within 24 h of the last pass count as the current burned area
        cutoff = (last - timedelta(hours=24)).isoformat()
        with self.lock:
            dets = [d for d in self.detections if d["id"] in set(inc["detection_ids"]) and d["time"] >= cutoff]
        ignitions = [(d["lat"], d["lon"], d["footprint_m"]) for d in dets] or [tuple(f) for f in inc["footprints"]]
        t0 = time.time()
        try:
            result = self.simulate(run, ignitions, start, hours, (inc["lat"], inc["lon"]))
            result["incident"] = {k: inc[k] for k in ("id", "lat", "lon", "first_seen", "last_seen", "detections",
                                                      "max_frp", "nearest_area", "distance_km")}
            (config.HOTSPOT_DIR / f"sim_{iid}.json").write_text(json.dumps(result), encoding="utf-8")
            summary = {"area_ha": result["hourly"][-1]["area_ha"] if result["hourly"] else None,
                       "start": start.isoformat(), "hours": hours, "edge_hit": result["edge_hit"],
                       "head_ros_max": max((h["head"]["ROS"] or 0) for h in result["hourly"]) if result["hourly"] else None,
                       "hfi_max": max((h["hfi_max"] or 0) for h in result["hourly"]) if result["hourly"] else None}
            err = None
        except Exception as e:
            log.warning("incident %s simulation failed: %s", iid, e)
            summary, err = None, str(e)
        with self.lock:
            if iid in self.incidents:
                self.incidents[iid].update(sim=summary, sim_run=run, sim_detections=inc["detections"], sim_error=err,
                                           sim_at=datetime.now(timezone.utc).isoformat())
                self._save()
        log.info("incident %s simulated in %.1fs (%s)", iid, time.time() - t0, err or "ok")
        return summary, err

    def loop(self, stop, on_update=None, on_poll=None):
        while not stop.is_set():
            try:
                self.poll()
                if on_poll:  # before the (slower) growth runs
                    on_poll()
                self.auto_simulate()
                if on_update:
                    on_update()
            except Exception:
                log.exception("hotspot cycle failed")
            stop.wait(config.FIRMS_POLL_MIN * 60)

    def snapshot(self):
        with self.lock:
            return list(self.detections)

    # ---- API views -----------------------------------------------------------------------
    def detections_view(self, hours=48):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.lock:
            inc_of = {did: i["id"] for i in self.incidents.values() for did in i["detection_ids"]}
            return [{k: d[k] for k in ("lat", "lon", "time", "sensor", "frp", "confidence", "persistent", "low_conf", "footprint_m")}
                    | {"incident": inc_of.get(d["id"])} for d in self.detections if d["time"] >= cutoff]

    def progression(self, iid):
        """An incident's detections grouped into satellite passes (one satellite, detections within 10 min)."""
        with self.lock:
            inc = self.incidents.get(iid)
            hist = list(inc.get("history", [])) if inc else None
        if inc is None:
            raise LookupError("unknown incident")
        out = []
        for p in _group_passes(hist):
            frps = [d["frp"] for d in p["detections"]]
            out.append({"time": p["time"], "sensor": p["sensor"], "count": len(frps),
                        "frp_sum": round(sum(frps), 1), "frp_max": round(max(frps), 1), "detections": p["detections"]})
        return {"id": iid, "lat": inc["lat"], "lon": inc["lon"], "nearest_area": inc["nearest_area"],
                "distance_km": inc["distance_km"], "passes": out}

    def incidents_view(self):
        with self.lock:
            out = [{k: v for k, v in i.items() if k not in ("detection_ids", "footprints", "history")}
                   | {"fresh": self._fresh(i), "passes": len(_group_passes(i.get("history", [])))}
                   for i in self.incidents.values()]
        # near-base and recently detected first
        return sorted(out, key=lambda i: (not i["near_base"], not i["fresh"], i["distance_km"]))
