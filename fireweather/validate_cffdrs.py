"""Check the vectorised FWI2025 port against cffdrs-ng's reference output.

    python -m fireweather.validate_cffdrs <path to cffdrs-ng checkout>

Runs PRF2007_hourly_wx.csv through fireweather.cffdrs and compares every output
column with PRF2007_standard_hourly_FWI.csv (which is rounded to 4 decimals).
"""
import csv
import sys
from datetime import date
from pathlib import Path

import numpy as np

from . import cffdrs

COLS = ["sunrise", "sunset", "solrad", "percent_cured", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr",
        "gfmc", "gsi", "gfwi", "mcffmc", "mcgfmc_matted", "mcgfmc_standing", "prec_cumulative", "canopy_drying"]


def days_since_greenup(d):
    start = date(d.year, 3, 12)
    if d < start:
        start = date(d.year - 1, 3, 12)
    return (d - start).days


def main(repo):
    repo = Path(repo)
    with open(repo / "data" / "PRF2007_hourly_wx.csv") as f:
        rows = list(csv.DictReader(f))
    with open(repo / "data" / "PRF2007_standard_hourly_FWI.csv") as f:
        ref = list(csv.DictReader(f))
    assert len(rows) == len(ref)
    a = lambda v: np.array([float(v)])
    s = cffdrs.initial_state((1,))
    worst = {c: 0.0 for c in COLS}
    for w, r in zip(rows, ref):
        d = date(int(w["yr"]), int(w["mon"]), int(w["day"]))
        hr, temp, rh, ws, prec = (a(w[k]) for k in ("hr", "temp", "rh", "ws", "prec"))
        lat, lon, tz = a(w["lat"]), a(w["long"]), a(w["timezone"])
        doy = d.timetuple().tm_yday
        leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
        sunrise, sunset, solrad = cffdrs.sunlight(lat, lon, tz, doy, leap, hr, temp, rh)
        cur = cffdrs.seasonal_curing(days_since_greenup(d))
        standing = d >= date(d.year, 7, 1)
        out = cffdrs.step(s, temp, rh, ws, prec, hr, sunrise, sunset, solrad, cur, standing)
        got = dict(out, sunrise=sunrise, sunset=sunset, solrad=solrad, percent_cured=cur,
                   mcffmc=s["mcffmc"], mcgfmc_matted=s["mcgfmc_matted"], mcgfmc_standing=s["mcgfmc_standing"],
                   prec_cumulative=s["rain_total_prev"], canopy_drying=s["drying_since_intercept"])
        for c in COLS:
            worst[c] = max(worst[c], abs(float(np.asarray(got[c]).ravel()[0]) - float(r[c])))
    print(f"{len(rows)} hourly rows")
    ok = True
    for c in COLS:
        flag = "ok" if worst[c] <= 1e-3 else "MISMATCH"
        ok &= flag == "ok"
        print(f"  {c:16s} max |diff| = {worst[c]:.6f}  {flag}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
