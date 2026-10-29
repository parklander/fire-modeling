"""Hourly precipitation rate and type, derived per forecast hour.

- prate: mm/h, the difference between consecutive hourly accumulated-precipitation frames (precip[T] - precip[T-1]),
  i.e. the amount that fell in the hour ending at T.
- ptype: 0 none, 1 rain, 2 rain/snow mix, 3 freezing rain, 4 ice pellets, 5 snow. From the RDPS instantaneous
  precipitation type (ptype_raw, ECCC codes 1-5 as above, 6 = no precipitation at that instant). Where the hour had
  precipitation but the model's type at the end of the hour is "none" (it stopped), the 2 m temperature decides:
  <= 0 °C snow, < 2 °C mix, else rain. Hours with less than PRATE_MIN are typed "none".

Codes were matched against the RDPS rain / snow / freezing rain / ice pellet 1 h accumulations for the same hour.
"""
import logging
import threading
import time

import numpy as np

from . import config, grid, ingest

log = logging.getLogger("precip")

INPUTS = ("precip", "temp", "ptype_raw")


class PrecipEngine:
    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        store.listeners.append(self._on_frame)

    def _on_frame(self, run, var, fh):
        if var in INPUTS:
            self.wake.set()

    def ready(self, run, fh):
        s = self.store
        return (not s.has(run, "ptype", fh) and s.has(run, "precip", fh - 1)
                and all(s.has(run, v, fh) for v in INPUTS))

    def loop(self, stop):
        while not stop.is_set():
            self.wake.wait(60)
            self.wake.clear()
            try:
                for run in sorted(self.store.runs):
                    for fh in config.FORECAST_HOURS[1:]:
                        if stop.is_set():
                            return
                        if self.ready(run, fh):
                            self._derive(run, fh)
            except Exception:
                log.exception("precipitation rate/type failed")

    def _derive(self, run, fh):
        t0 = time.time()
        load = lambda v, h: grid.decode(ingest.frame_path(run, v, h).read_bytes(), v)
        p1, p0, temp, raw = load("precip", fh), load("precip", fh - 1), load("temp", fh), load("ptype_raw", fh)
        rate = np.clip(p1 - p0, 0, None)  # accumulations never decrease; clip quantisation noise
        code = np.rint(np.nan_to_num(raw, nan=6))
        typ = np.where((code >= 1) & (code <= 5), code, 0)
        wet = rate >= config.PRATE_MIN
        fallback = np.where(temp <= 0, 5, np.where(temp < 2, 2, 1))  # stopped by the end of the hour: by temperature
        typ = np.where(wet, np.where(typ == 0, fallback, typ), 0).astype(np.float32)
        typ[np.isnan(rate)] = np.nan
        h, w = grid.raster_shape()
        ingest.write_frame(self.store, run, "prate", fh, rate.astype(np.float32).reshape(h, w))
        ingest.write_frame(self.store, run, "ptype", fh, typ.reshape(h, w))  # last: marks the hour done
        log.debug("precip %s T+%02d  %.1fs", run, fh, time.time() - t0)
