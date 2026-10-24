"""Smoke-management diagnostics derived from the RDPS burn inputs, per forecast hour.

- lapse: T(120 m) - T(2 m). Positive = surface-based inversion (stable); more negative
  than about -1.4 °C over the 118 m layer is steeper than dry adiabatic (unstable).
- twspd / twdir: transport wind, the thickness-weighted vector mean wind from 10 m up
  to the mixing height (RDPS boundary-layer height). Pressure-level heights above ground
  come from surface pressure using the standard atmosphere, which is close enough for
  weighting layers (errors of a few % in height).
- vi: ventilation index = mixing height (m) x transport wind speed (m/s), in m^2/s.

Once an hour's products are written its aux inputs (winds aloft, T120, surface pressure)
are deleted to save disk.
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import config, grid, ingest

log = logging.getLogger("smoke")

LEVELS_AGL = [(10, "wspd", "wdir")] + [(h, f"ws_{h}m", f"wd_{h}m") for h in config.TRANSPORT_AGL]
LEVELS_HPA = [(p, f"ws_{p}", f"wd_{p}") for p in config.TRANSPORT_HPA]
INPUTS = ["temp", "t120", "pbl", "psfc"] + [v for _, s, d in LEVELS_AGL + LEVELS_HPA for v in (s, d)]


def std_height(p_hpa):
    """Standard-atmosphere height (m ASL) of a pressure level."""
    return 44330.8 * (1 - (p_hpa / 1013.25) ** 0.190263)


def transport_wind(pbl, psfc, winds_agl, winds_hpa):
    """Mixed-layer mean wind. winds_* are lists of (level, speed km/h, dir deg) arrays."""
    z_sfc = std_height(psfc)
    heights, us, vs = [], [], []
    levels = [(np.full_like(psfc, float(h)), spd, d) for h, spd, d in winds_agl]
    levels += [(np.where(p < psfc, std_height(p) - z_sfc, np.inf), spd, d)  # below ground -> unused
               for p, spd, d in winds_hpa]
    for h, spd, d in levels:
        rad = np.radians(d)
        heights.append(h)
        us.append(-spd * np.sin(rad))
        vs.append(-spd * np.cos(rad))
    H, U, V = np.array(heights), np.array(us), np.array(vs)
    order = np.argsort(H, axis=0)
    H, U, V = (np.take_along_axis(a, order, axis=0) for a in (H, U, V))
    mid = (H[:-1] + H[1:]) / 2
    lo = np.concatenate([np.zeros_like(H[:1]), mid])
    hi = np.concatenate([mid, np.full_like(H[:1], np.inf)])
    top = np.maximum(pbl, 20.0)
    w = np.clip(np.minimum(hi, top) - lo, 0, None)
    w = np.where(np.isfinite(H) & np.isfinite(U), w, 0)
    wsum = w.sum(axis=0)
    u = (w * np.nan_to_num(U)).sum(axis=0) / wsum
    v = (w * np.nan_to_num(V)).sum(axis=0) / wsum
    spd = np.hypot(u, v)
    direction = np.degrees(np.arctan2(-u, -v)) % 360
    return spd, direction


class SmokeEngine:
    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.pool = ThreadPoolExecutor(max(1, min(8, (os.cpu_count() or 2) - 1)), thread_name_prefix="smoke")
        store.listeners.append(self._on_frame)

    def _on_frame(self, run, var, fh):
        if var in INPUTS:
            self.wake.set()

    def loop(self, stop):
        while not stop.is_set():
            self.wake.wait(60)
            self.wake.clear()
            try:
                for run in sorted(self.store.runs):
                    for fh in config.BURN_HOURS:
                        if stop.is_set():
                            return
                        if not self.store.has(run, "vi", fh) and all(self.store.has(run, v, fh) for v in INPUTS):
                            self._derive(run, fh)
            except Exception:
                log.exception("smoke diagnostics failed")

    def _derive(self, run, fh):
        t0 = time.time()
        load = lambda v: grid.decode(ingest.frame_path(run, v, fh).read_bytes(), v)
        f = dict(zip(INPUTS, self.pool.map(load, INPUTS)))
        n = f["temp"].size
        bounds = np.linspace(0, n, self.pool._max_workers * 2 + 1).astype(int)

        def chunk(sl):
            s = lambda v: f[v][sl].astype(np.float64)
            spd, d = transport_wind(s("pbl"), s("psfc"),
                                    [(h, s(a), s(b)) for h, a, b in LEVELS_AGL],
                                    [(p, s(a), s(b)) for p, a, b in LEVELS_HPA])
            return {"twspd": spd, "twdir": d, "vi": s("pbl") * spd / 3.6, "lapse": s("t120") - s("temp")}

        parts = list(self.pool.map(chunk, [slice(a, b) for a, b in zip(bounds[:-1], bounds[1:])]))
        h, w = grid.raster_shape()
        for var in ("lapse", "twspd", "twdir", "vi"):  # vi last: it marks the hour as done
            a = np.concatenate([p[var] for p in parts]).astype(np.float32)
            a[np.isnan(f["temp"])] = np.nan
            ingest.write_frame(self.store, run, var, fh, a.reshape(h, w))
        for v in config.AUX_VARS:
            self.store.remove(run, v, fh)
        log.info("smoke %s T+%02d  %.1fs", run, fh, time.time() - t0)
