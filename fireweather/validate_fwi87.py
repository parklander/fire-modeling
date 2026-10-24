"""Check the FWI1987 port (hourly FFMC, FFMC, DMC, DC, BUI) against cffdrs fixtures.

    python -m fireweather.validate_fwi87 <path to cffdrs_r checkout>
"""
import csv
import sys
from pathlib import Path

import numpy as np

from . import fwi87

CASES = {
    "HourlyFineFuelMoistureCode": (fwi87.hourly_ffmc, ["temp", "rh", "ws", "prec", "ffmc_old", "time.step"]),
    "FineFuelMoistureCode": (fwi87.daily_ffmc, ["ffmc_yda", "temp", "rh", "ws", "prec"]),
    "DuffMoistureCode": (fwi87.duff_moisture_code, ["dmc_yda", "temp", "rh", "prec", "lat", "mon", "lat.adjust"]),
    "DroughtCode": (fwi87.drought_code, ["dc_yda", "temp", "rh", "prec", "lat", "mon", "lat.adjust"]),
    "BuildupIndex": (fwi87.buildup_index, ["dmc", "dc"]),
    "InitialSpreadIndex": (fwi87.initial_spread_index, ["ffmc", "ws", "fbpMod"]),
    "FireWeatherIndex": (fwi87.fire_weather_index, ["isi", "bui"]),
}


def val(v):
    if v in ("TRUE", "FALSE"):
        return v == "TRUE"
    return np.nan if v in ("", "NA") else float(v)


def main(repo):
    data = Path(repo) / "tests" / "testthat" / "data"
    ok = True
    for name, (fn, args) in CASES.items():
        with open(data / f"{name}.csv") as f:
            rows = list(csv.DictReader(f))
        cols = [np.array([val(r[a]) for r in rows]) for a in args]
        want = np.array([val(r[name]) for r in rows])
        got = fn(*cols)
        valid = ~np.isnan(want)
        err = np.abs(got[valid] - want[valid])
        tol = np.maximum(np.abs(want[valid]) * 1e-3, 1e-3)  # fixtures carry 4 significant digits
        bad = int((err > tol).sum() + (~np.isnan(got[valid])).size - (~np.isnan(got[valid])).sum())
        ok &= bad == 0
        print(f"{name:28s} {valid.sum():5d} rows, max abs err {err.max():.2e}, {bad} mismatches")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
