"""Hourly FWI2025 over the RDPS forecast, chained from run to run.

The FWI system is a bookkeeping of fuel moisture, so each hour needs the previous
hour's state. For run R we start from the state valid at R that an earlier run's
forecast produced (normally R-6h at T+6, saved to data/fwi_state), then step through
T+1..T+84 as the weather frames arrive. With no earlier state (first start, or a gap)
we cold-start from CWFIS's current daily FFMC/DMC/DC grids where they have data and
the FWI2025 startup defaults elsewhere.

Time of day: the grid uses local solar time (UTC offset = lon/15), which keeps the
sunrise/sunset day-night switch of the DMC/DC drying consistent everywhere.
"""
import logging
import re
import threading
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

from . import cffdrs, config, grid, ingest

log = logging.getLogger("fwi")

INPUTS = ("temp", "rh", "wspd", "precip")
STATE_RE = re.compile(r"(\d{10})_(\d{10})\.npz$")  # <valid time>_<source run>


def _run_time(run):
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def _days_since_greenup(d):
    start = date(d.year, 3, 12)
    if d < start:
        start = date(d.year - 1, 3, 12)
    return (d - start).days


class FWIEngine:
    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.chain = {}  # run -> (last hour computed, state dict)
        self._static = None
        # numpy releases the GIL, so stepping chunks of the grid in threads scales with cores
        self.pool = ThreadPoolExecutor(max(1, min(8, (os.cpu_count() or 2) - 1)), thread_name_prefix="fwi")
        config.FWI_STATE_DIR.mkdir(parents=True, exist_ok=True)
        store.listeners.append(self._on_frame)

    def _on_frame(self, run, var, fh):
        if var in INPUTS:
            self.wake.set()

    # ---- main loop --------------------------------------------------------------
    def loop(self, stop):
        while not stop.is_set():
            self.wake.wait(60)
            self.wake.clear()
            try:
                for run in sorted(self.store.runs):  # oldest first so states chain forward
                    if stop.is_set():
                        break
                    self._advance(run)
            except Exception:
                log.exception("FWI computation failed")

    def _static_fields(self):
        if self._static is None:
            lat, lon = grid.pixel_latlon()
            self._static = {"lat": lat, "lon": lon, "tz": lon / 15.0}
        return self._static

    def _have_inputs(self, run, fh):
        return all(self.store.has(run, v, fh) for v in INPUTS)

    def _advance(self, run):
        done = self.store.runs.get(run, {}).get("fwi", set())
        if len(done) == len(config.FORECAST_HOURS) and run not in self.chain:
            return
        if run not in self.chain:
            if not self._have_inputs(run, 0):
                return
            state = self._initial_state(run)
            if state is None:
                return
            wx = self._load(run, 0)
            self._write(run, 0, self._parallel(
                lambda sl: cffdrs.indices(_sub(state, sl), wx["wspd"][sl], *self._grass(run, 0, sl))))
            self.chain[run] = (0, state)
        hour, state = self.chain[run]
        t0 = time.time()
        start = hour
        while hour + 1 <= config.FORECAST_HOURS[-1] and self._have_inputs(run, hour + 1):
            hour += 1
            wx = self._load(run, hour)
            wx["prec"] = np.maximum(wx["precip"] - self._load_one(run, "precip", hour - 1), 0.0)
            self._write(run, hour, self._parallel(lambda sl: self._step_chunk(run, hour, state, wx, sl)))
            if hour in config.FWI_STATE_HOURS:
                self._save_state(run, hour, state)
        self.chain[run] = (hour, state)
        if hour > start:
            log.info("FWI %s T+%02d..%02d  %.1fs", run, start + 1, hour, time.time() - t0)
        if hour == config.FORECAST_HOURS[-1]:
            del self.chain[run]

    # ---- inputs -----------------------------------------------------------------
    def _load_one(self, run, var, fh):
        return grid.decode(ingest.frame_path(run, var, fh).read_bytes(), var).astype(np.float64)

    def _load(self, run, fh):
        wx = {v: self._load_one(run, v, fh) for v in INPUTS}
        wx["rh"] = np.clip(wx["rh"], 0, 100)
        wx["wspd"] = np.maximum(wx["wspd"], 0)
        return wx

    def _local(self, run, fh, sl):
        """Local solar hour and a picker for per-pixel local-date properties."""
        st = self._static_fields()
        valid = _run_time(run) + timedelta(hours=fh)
        local = valid.hour + valid.minute / 60 + st["tz"][sl]
        day_shift = np.floor(local / 24).astype(int)  # -1, 0 or +1 relative to the UTC date
        dates = [valid.date() + timedelta(days=k) for k in (-1, 0, 1)]
        pick = lambda f: np.array([f(d) for d in dates], dtype=float)[day_shift + 1]
        return local - 24 * day_shift, pick

    def _grass(self, run, fh, sl):
        """Percent cured and standing/matted grass for each pixel's local date."""
        _, pick = self._local(run, fh, sl)
        # Matted (snow-pressed) grass until July 1, standing after (NG_FWI default transition).
        return (cffdrs.seasonal_curing(pick(_days_since_greenup).astype(int)),
                pick(lambda d: d >= date(d.year, 7, 1)).astype(bool))

    def _step_chunk(self, run, fh, state, wx, sl):
        st = self._static_fields()
        hr, pick = self._local(run, fh, sl)
        doy = pick(lambda d: d.timetuple().tm_yday)
        leap = pick(lambda d: d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)).astype(bool)
        temp, rh, ws, prec = (wx[k][sl] for k in ("temp", "rh", "wspd", "prec"))
        sunrise, sunset, solrad = cffdrs.sunlight(st["lat"][sl], st["lon"][sl], st["tz"][sl], doy, leap, hr, temp, rh)
        cured, standing = self._grass(run, fh, sl)
        sub = _sub(state, sl)
        out = cffdrs.step(sub, temp, rh, ws, prec, hr, sunrise, sunset, solrad, cured, standing)
        for k in cffdrs.STATE_KEYS:
            state[k][sl] = sub[k]
        return out

    def _parallel(self, fn):
        n = self._static_fields()["lat"].size
        bounds = np.linspace(0, n, self.pool._max_workers * 2 + 1).astype(int)
        slices = [slice(a, b) for a, b in zip(bounds[:-1], bounds[1:])]
        parts = list(self.pool.map(fn, slices))
        return {k: np.concatenate([p[k] for p in parts]) for k in config.INDICES}

    def _write(self, run, fh, out):
        h, w = grid.raster_shape()
        outside = grid.get_map()["idx"] < 0
        def one(var):
            a = out[var].astype(np.float32)
            a[outside] = np.nan
            ingest.write_frame(self.store, run, var, fh, a.reshape(h, w))
        list(self.pool.map(one, config.INDICES))

    # ---- state chaining ---------------------------------------------------------------
    def _save_state(self, run, fh, state):
        valid = (_run_time(run) + timedelta(hours=fh)).strftime("%Y%m%d%H")
        path = config.FWI_STATE_DIR / f"{valid}_{run}.npz"
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, **{k: state[k].astype(np.float32) for k in cffdrs.STATE_KEYS})
        with open(tmp, "rb+") as f:  # on disk before the rename, so a crash can't leave a zero-filled state
            os.fsync(f.fileno())
        tmp.replace(path)
        self._prune_states()

    def _prune_states(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d%H")
        for p in config.FWI_STATE_DIR.glob("*.npz"):
            m = STATE_RE.search(p.name)
            if m and m[1] < cutoff:
                p.unlink(missing_ok=True)

    def _initial_state(self, run):
        """State valid at the run's T+0, or None to wait for an earlier run to produce it."""
        cands = sorted((p for p in config.FWI_STATE_DIR.glob(f"{run}_*.npz")
                        if (m := STATE_RE.search(p.name)) and m[2] < run),
                       key=lambda p: STATE_RE.search(p.name)[2], reverse=True)
        for best in cands:  # newest first; skip (and delete) a state damaged by a crash
            try:
                z = np.load(best)
                state = {k: z[k].astype(np.float64) for k in cffdrs.STATE_KEYS}
            except Exception as e:
                log.warning("FWI state %s unreadable (%s); deleting it", best.name, e)
                best.unlink(missing_ok=True)
                continue
            log.info("FWI %s: starting from state saved by run %s", run, STATE_RE.search(best.name)[2])
            return state
        # An earlier run on disk that will reach this run's T+0 at a save hour: wait for it.
        t0 = _run_time(run)
        for prev in self.store.runs:
            lead = (t0 - _run_time(prev)).total_seconds() / 3600
            if (prev < run and lead in config.FWI_STATE_HOURS and self._have_inputs(prev, int(lead))
                    and int(lead) not in self.store.runs[prev].get("fwi", ())):
                return None
        return self._cold_start(run)

    def _cold_start(self, run):
        st = self._static_fields()
        seed = fetch_cwfis_seed(st["lat"], st["lon"])
        in_domain = grid.get_map()["idx"] >= 0
        grid_frac = float(np.mean(~np.isnan(seed["dc"][in_domain])))
        stn_frac = fill_from_stations(st["lat"], st["lon"], seed)
        ffmc = np.where(np.isnan(seed["ffmc"]), cffdrs.FFMC_DEFAULT, seed["ffmc"])
        dmc = np.where(np.isnan(seed["dmc"]), cffdrs.DMC_DEFAULT, seed["dmc"])
        dc = np.where(np.isnan(seed["dc"]), cffdrs.DC_DEFAULT, seed["dc"])
        log.warning("FWI %s: cold start, DC seeded from CWFIS grid %.0f%%, CWFIS stations %.0f%%, defaults %.0f%% "
                    "of model domain", run, 100 * grid_frac, 100 * stn_frac[in_domain].mean(),
                    100 * np.isnan(seed["dc"][in_domain]).mean())
        return cffdrs.initial_state(st["lat"].shape, np.clip(ffmc, 0, 101), np.maximum(dmc, 0), np.maximum(dc, 0))


def _sub(state, sl):
    return {k: state[k][sl] for k in cffdrs.STATE_KEYS}


def fill_from_stations(lat, lon, seed, k=4, power=2.0):
    """Fill NaN gaps in `seed` (in place) by inverse-distance weighting of CWFIS station codes.

    Only points within CWFIS_STATION_MAX_KM of a reporting station are filled; each code
    uses only stations that reported it. Returns a bool mask of points filled (DC).
    """
    from scipy.spatial import cKDTree

    from . import externals

    data = externals.cwfis_station_codes()
    filled_dc = np.zeros(lat.shape, dtype=bool)
    if not data:
        log.warning("CWFIS station file unavailable; no station-based seed")
        return filled_dc
    to_xyz = lambda la, lo: grid._xyz(np.asarray(la, float), np.asarray(lo, float))
    max_chord = 2 * np.sin(config.CWFIS_STATION_MAX_KM / 6371 / 2)
    for code in ("ffmc", "dmc", "dc"):
        stns = [s for s in data["stations"] if s[code] is not None and s[code] >= 0]
        gap = np.isnan(seed[code])
        if not stns or not gap.any():
            continue
        tree = cKDTree(to_xyz([s["lat"] for s in stns], [s["lon"] for s in stns]))
        vals = np.array([s[code] for s in stns])
        kk = min(k, len(stns))
        d, i = tree.query(to_xyz(lat[gap], lon[gap]), k=kk, distance_upper_bound=max_chord)
        d, i = d.reshape(-1, kk), i.reshape(-1, kk)
        ok = np.isfinite(d)  # neighbours within range (missing ones come back as inf)
        w = np.where(ok, 1.0 / np.maximum(d, 1e-9) ** power, 0.0)
        wsum = w.sum(axis=1)
        est = (w * vals[np.where(ok, i, 0)]).sum(axis=1) / np.where(wsum > 0, wsum, 1)
        est[wsum == 0] = np.nan
        seed[code][gap] = est
        if code == "dc":
            filled_dc[gap] = ~np.isnan(est)
    log.info("CWFIS station seed: file %s, %d stations", data["file_date"], len(data["stations"]))
    return filled_dc


def fetch_cwfis_seed(lat, lon):
    """Nearest-neighbour sample of CWFIS current FFMC/DMC/DC at the given points (NaN = no data)."""
    import io
    import tifffile

    b, res = config.BOUNDS, config.CWFIS_SEED_RES
    w = int(round((b["east"] - b["west"]) / res))
    h = int(round((b["north"] - b["south"]) / res))
    out = {}
    for code in ("ffmc", "dmc", "dc"):
        try:
            r = requests.get(config.CWFIS_WCS, timeout=120, params={
                "service": "WCS", "version": "1.0.0", "request": "GetCoverage",
                "coverage": f"public:{code}_current", "crs": "EPSG:4326",
                "bbox": f'{b["west"]},{b["south"]},{b["east"]},{b["north"]}',
                "width": w, "height": h, "format": "GeoTIFF"})
            r.raise_for_status()
            logging.getLogger("tifffile").setLevel(logging.ERROR)
            with tifffile.TiffFile(io.BytesIO(r.content)) as tif:
                a = tif.pages[0].asarray().astype(np.float64)
                # GeoServer may ignore the requested bbox and return the coverage's own extent,
                # so locate pixels with the GeoTIFF's affine transform rather than our request.
                tags = tif.pages[0].tags
                if "ModelTransformationTag" in tags:
                    m = tags["ModelTransformationTag"].value
                    x0, sx, y0, sy = m[3], m[0], m[7], m[5]
                else:
                    scale, tie = tags["ModelPixelScaleTag"].value, tags["ModelTiepointTag"].value
                    x0, sx, y0, sy = tie[3], scale[0], tie[4], -scale[1]
            a[(a < 0) | (a > 5000)] = np.nan  # nodata is -3.4e38
            col = np.floor((lon - x0) / sx).astype(int)
            row = np.floor((lat - y0) / sy).astype(int)
            inside = (col >= 0) & (col < a.shape[1]) & (row >= 0) & (row < a.shape[0])
            vals = np.full(lat.shape, np.nan)
            vals[inside] = a[row[inside], col[inside]]
            out[code] = vals
        except Exception as e:
            log.warning("CWFIS %s seed unavailable (%s); using default", code, e)
            out[code] = np.full(lat.shape, np.nan)
    return out
