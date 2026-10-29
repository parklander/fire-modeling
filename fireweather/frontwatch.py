"""Front watch: is a front coming to a base, how fast, and what will the wind do when it arrives?

Two independent views, shown side by side:

1. WPC analysed fronts (fronts.py) and how they are moving. Each front in the latest analysis is compared with the
   analyses 3 and 6 h earlier: points along it are matched to the nearest point of an earlier front, which measures
   motion across the front (the part that brings it to a base). The median displacement over the time gap gives
   speed; the mean displacement direction gives heading. For each base: distance to the nearest front, whether that
   front is closing on it, and an arrival estimate (distance / closing speed), all straight-line extrapolation.

2. The RDPS hourly forecast at the base. A frontal passage leaves a signature: the wind veers or backs at least
   45 deg (3 h mean before vs after), together with a pressure trough (lowest pressure of the +-3 h window, then
   rising) or a temperature drop of 3 C or more or an RH jump of 15 % or more. Reported: when, the wind before and
   after, the peak gust in the 6 h after, and the temperature / RH change.
"""
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

from . import config, fronts, grid, ingest, report

log = logging.getLogger("frontwatch")

FRONT_KINDS = ("cold", "warm", "stationary", "occluded", "trough")
SERIES_VARS = ("wspd", "wdir", "gust", "temp", "rh", "mslp")
MAX_MATCH_KM = 450      # an earlier front further than this from a point isn't the same front
MAX_SPEED_KMH = 90      # faster than this is a mismatch, not a front
WATCH_KM = 800          # report fronts within this distance of a base
ETA_MAX_H = 36          # straight-line arrival estimates beyond this aren't given
COMPATIBLE = {"cold": {"cold", "stationary", "occluded"}, "warm": {"warm", "stationary", "occluded"},
              "stationary": {"stationary", "cold", "warm"}, "occluded": {"occluded", "cold", "warm"},
              "trough": {"trough"}}
MIN_AFTER_KMH, MIN_GUST_KMH = 15, 30      # a shift counts only with this much wind after it (or gusts)
STRONG_KMH, STRONG_GUST_KMH = 30, 50      # ... and is "strong" above these
_pool = ThreadPoolExecutor(8, thread_name_prefix="frontwatch")


# ---- geometry on a local flat projection (fine over a few hundred km) --------------------------------------------
def _xy(lat, lon, lat0):
    return lon * 111.32 * math.cos(math.radians(lat0)), lat * 110.57


def _seg_nearest(p, a, b):
    """Nearest point to p on segment a-b (all (x, y) km)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return ax + t * dx, ay + t * dy


def _nearest_on(p, lines):
    """(distance km, nearest point) from p to any polyline in `lines` (lists of (x, y))."""
    best = (math.inf, None)
    for pts in lines:
        for a, b in zip(pts, pts[1:]):
            q = _seg_nearest(p, a, b)
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            if d < best[0]:
                best = (d, q)
    return best


def _resample(pts, step=40.0):
    """Points every `step` km along a polyline (x, y)."""
    out, carry = [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            d += step
        carry = (carry + seg) % step
    return out


def _bearing(dx, dy):
    """Compass bearing (deg) of a displacement (x east, y north)."""
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


# ---- WPC front motion --------------------------------------------------------------------------------------------
def front_motion(front, earlier, hours, lat0):
    """Speed (km/h) and heading (deg) of `front` from earlier fronts `hours` before, or None if no match."""
    pts = [_xy(la, lo, lat0) for la, lo in front["points"]]
    ok = COMPATIBLE.get(front["kind"], {front["kind"]})  # a cold front may be redrawn stationary, not as a warm one
    prev = [[_xy(la, lo, lat0) for la, lo in f["points"]] for f in earlier
            if f["kind"] in ok and len(f["points"]) >= 2]
    if len(pts) < 2 or not prev:
        return None
    disp = []
    for p in _resample(pts):
        d, q = _nearest_on(p, prev)
        if d <= MAX_MATCH_KM:
            disp.append((p[0] - q[0], p[1] - q[1], d))
    if len(disp) < max(3, len(_resample(pts)) // 3):  # most of the front must match
        return None
    dist = float(np.median([d for *_, d in disp]))
    speed = dist / hours
    if speed > MAX_SPEED_KMH:
        return None
    mx, my = float(np.mean([x for x, _, _ in disp])), float(np.mean([y for _, y, _ in disp]))
    heading = _bearing(mx, my) if math.hypot(mx, my) > 1e-6 else None
    # velocity: speed along the mean displacement direction
    norm = math.hypot(mx, my) or 1.0
    return {"speed_kmh": round(speed, 1), "heading": None if heading is None else round(heading),
            "vx": speed * mx / norm, "vy": speed * my / norm, "hours": hours}


def wpc_watch(lat, lon, analyses):
    """Nearest WPC front to a point and its motion: dict or None."""
    if not analyses:
        return None
    cur = analyses[-1]
    t_cur = datetime.fromisoformat(cur["valid"])
    lat0 = lat
    base = _xy(lat, lon, lat0)
    best = None
    for f in cur["fronts"]:
        if f["kind"] not in FRONT_KINDS or len(f["points"]) < 2:
            continue
        d, q = _nearest_on(base, [[_xy(la, lo, lat0) for la, lo in f["points"]]])
        if d <= WATCH_KM and (best is None or d < best[0]):
            best = (d, q, f)
    if best is None:
        return None
    d, q, f = best
    out = {"kind": f["kind"], "distance_km": round(d), "lies": report.compass(_bearing(q[0] - base[0], q[1] - base[1])),
           "analysis_valid": cur["valid"], "motion": None, "eta": None, "closing_kmh": None}
    # motion: compare with the analysis 3 h back, else 6 h back (the 00/06/12/18Z ones carry more fronts)
    for back in (3, 6):
        prev = next((a for a in analyses[:-1] if datetime.fromisoformat(a["valid"]) == t_cur - timedelta(hours=back)), None)
        m = prev and front_motion(f, prev["fronts"], back, lat0)
        if m:
            out["motion"] = {"speed_kmh": m["speed_kmh"], "heading": m["heading"],
                             "toward": report.compass(m["heading"]) if m["heading"] is not None else None,
                             "from_analysis": prev["valid"]}
            # closing speed: velocity component along the front-to-base direction
            ux, uy = base[0] - q[0], base[1] - q[1]
            n = math.hypot(ux, uy) or 1.0
            closing = (m["vx"] * ux + m["vy"] * uy) / n
            out["closing_kmh"] = round(closing, 1)
            if closing >= 3 and d > 0:
                hrs = d / closing
                out["eta_hours"] = round(hrs, 1)
                if hrs <= ETA_MAX_H:
                    out["eta"] = (t_cur + timedelta(hours=hrs)).isoformat()
            break
    return out


# ---- RDPS frontal signature at a point ---------------------------------------------------------------------------
def _vmean(spd, dirs):
    u = sum(-s * math.sin(math.radians(d)) for s, d in zip(spd, dirs))
    v = sum(-s * math.cos(math.radians(d)) for s, d in zip(spd, dirs))
    n = len(spd) or 1
    return math.hypot(u, v) / n, (math.degrees(math.atan2(-u, -v)) + 360) % 360


def rdps_passages(series, t0, min_veer=45.0):
    """Frontal passages in an hourly point series {var: [values by forecast hour]}."""
    ws, wd, T, rh, p, g = (series[k] for k in ("wspd", "wdir", "temp", "rh", "mslp", "gust"))
    n, out, skip = len(ws), [], -1
    for h in range(3, n - 3):
        if h <= skip:
            continue
        b, a = range(h - 3, h), range(h + 1, h + 4)
        if any(ws[i] is None or wd[i] is None for i in (*b, *a)):
            continue
        sb, db = _vmean([ws[i] for i in b], [wd[i] for i in b])
        sa, da = _vmean([ws[i] for i in a], [wd[i] for i in a])
        veer = abs((da - db + 180) % 360 - 180)
        if veer < min_veer or sb < 8 or sa < 8:
            continue
        win = [p[i] for i in range(h - 3, h + 4) if p[i] is not None]
        trough = bool(win) and p[h] is not None and p[h] <= min(win) + 0.6 and p[h + 3] is not None and p[h + 3] - p[h] >= 0.8
        tb = [T[i] for i in b if T[i] is not None]
        ta = [T[i] for i in a if T[i] is not None]
        dT = (sum(ta) / len(ta) - sum(tb) / len(tb)) if ta and tb else 0.0
        rb = [rh[i] for i in b if rh[i] is not None]
        ra = [rh[i] for i in a if rh[i] is not None]
        dRH = (sum(ra) / len(ra) - sum(rb) / len(rb)) if ra and rb else 0.0
        if not (trough or dT <= -3 or dRH >= 15):
            continue
        # strongest hour of the shift: look 2 h ahead for a sharper veer, then skip past this event
        gusts = [g[i] for i in range(h, min(n, h + 7)) if g[i] is not None]
        after_max = max((ws[i] for i in range(h, min(n, h + 7)) if ws[i] is not None), default=sa)
        gmax = max(gusts) if gusts else 0
        if after_max < MIN_AFTER_KMH and gmax < MIN_GUST_KMH:
            continue  # a turn in light winds: not a fire concern
        out.append({
            "time": (t0 + timedelta(hours=h)).isoformat(), "fh": h,
            "from_dir": round(db), "from_card": report.compass(db), "from_speed": round(sb),
            "to_dir": round(da), "to_card": report.compass(da), "to_speed": round(sa),
            "veer": round(veer), "max_wind": round(after_max), "max_gust": round(max(gusts)) if gusts else None,
            "temp_change": round(dT, 1), "rh_change": round(dRH), "pressure_trough": trough,
            "strong": after_max >= STRONG_KMH or gmax >= STRONG_GUST_KMH,
        })
        skip = h + 6
    return out


def _latest_run(store):
    for r in store.summary():
        if r["complete"] and len(r.get("mslp_hours", [])) >= 80:
            return r["run"]
    return None


def point_series(run, idx_list):
    """Hourly values of SERIES_VARS at display pixels: {idx: {var: [..85]}} (full decode, for many points)."""
    out = {i: {v: [None] * len(config.FORECAST_HOURS) for v in SERIES_VARS} for i in idx_list}

    def one(var_fh):
        var, fh = var_fh
        p = ingest.frame_path(run, var, fh)
        if not p.exists():
            return
        a = grid.decode(p.read_bytes(), var)
        for i in idx_list:
            v = a[i]
            out[i][var][fh] = None if np.isnan(v) else float(v)
    list(_pool.map(one, [(v, fh) for v in SERIES_VARS for fh in config.FORECAST_HOURS]))
    return out


def single_series(run, idx):
    """Same for one pixel, decompressing each frame only as far as needed."""
    out = {v: [None] * len(config.FORECAST_HOURS) for v in SERIES_VARS}

    def one(var_fh):
        var, fh = var_fh
        p = ingest.frame_path(run, var, fh)
        if p.exists():
            out[var][fh] = grid.decode_pixel(p.read_bytes(), var, idx)
    list(_pool.map(one, [(v, fh) for v in SERIES_VARS for fh in config.FORECAST_HOURS]))
    return out


class FrontWatch:
    def __init__(self, store, fr):
        self.store, self.fr = store, fr
        self.lock = threading.Lock()
        self.cache = {"key": None, "bases": []}

    def _assess(self, lat, lon, run, series, analyses):
        t0 = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        events = [e for e in rdps_passages(series, t0) if datetime.fromisoformat(e["time"]) >= now - timedelta(hours=1)]
        wpc = wpc_watch(lat, lon, analyses)
        soon = [e for e in events if datetime.fromisoformat(e["time"]) <= now + timedelta(hours=48)]
        level = "warning" if any(e["strong"] for e in soon) else "watch" if soon else None
        return {"run": run, "rdps": events, "wpc": wpc, "level": level, "tz": str(report.local_zone(lat, lon))}

    def bases(self):
        """Front watch for every area of interest, recomputed when a new run or analysis arrives."""
        import json
        run = _latest_run(self.store)
        analyses = fronts.history_view(self.fr)
        key = (run, analyses[-1]["valid"] if analyses else None, config.AOI_FILE.stat().st_mtime)
        with self.lock:
            if self.cache["key"] == key:
                return self.cache["bases"]
            if not run:
                return []
            areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
            pix = {a["id"]: grid.point_pixel(a["lat"], a["lon"]) for a in areas}
            series = point_series(run, [p[0] for p in pix.values() if p])
            out = []
            for a in areas:
                if not pix[a["id"]]:
                    continue
                out.append({"id": a["id"], "name": a["name"], "prov": a.get("prov"), "lat": a["lat"], "lon": a["lon"],
                            **self._assess(a["lat"], a["lon"], run, series[pix[a["id"]][0]], analyses)})
            self.cache = {"key": key, "bases": out}
            n = sum(1 for b in out if b["level"])
            log.info("front watch %s: %d of %d bases with a frontal passage in the next 48 h", run, n, len(out))
            return out

    def point(self, lat, lon):
        with self.lock:  # at a base: reuse its cached result (same run and analysis)
            hit = next((b for b in self.cache["bases"] if abs(b["lat"] - lat) < 0.02 and abs(b["lon"] - lon) < 0.03), None)
        if hit and self.cache["key"] == (_latest_run(self.store), (fronts.history_view(self.fr) or [{}])[-1].get("valid"),
                                         config.AOI_FILE.stat().st_mtime):
            return hit
        run = _latest_run(self.store)
        found = grid.point_pixel(lat, lon)
        if not run or not found:
            return None
        return self._assess(lat, lon, run, single_series(run, found[0]), fronts.history_view(self.fr))

    def loop(self, stop, on_bases=None):
        """Recompute every 5 min (cached unless the run or analysis changed); on_bases(bases) runs the alerts."""
        while not stop.is_set():
            try:
                b = self.bases()
                if on_bases and b:
                    on_bases(b)
            except Exception:
                log.exception("front watch failed")
            stop.wait(300)
