"""Check the numpy FBP port against the cffdrs R package's own test fixtures.

    python -m fireweather.validate_fbp <path to cffdrs_r checkout>

Runs data/test_fbp.csv through fireweather.fbp and compares every "All" output column
with tests/testthat/data/fbp_06.csv (cffdrs rounds fixtures to 4 significant digits).
"""
import csv
import sys
from pathlib import Path

import numpy as np

from . import fbp

COLS = {"FuelType": "fueltype", "LAT": "lat", "LONG": "long", "ELV": "elv", "FFMC": "ffmc", "BUI": "bui",
        "WS": "ws", "WD": "wd", "GS": "gs", "Dj": "dj", "D0": "d0", "hr": "hr", "PC": "pc", "PDF": "pdf",
        "GFL": "gfl", "cc": "cc", "theta": "theta", "Accel": "accel", "Aspect": "aspect", "BUIEff": "buieff",
        "CBH": "cbh", "CFL": "cfl", "ISI": "isi"}


def num(v):
    return np.nan if v.strip() in ("", "NA") else float(v)


def main(repo):
    repo = Path(repo)
    with open(repo / "data" / "test_fbp.csv") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    with open(repo / "tests" / "testthat" / "data" / "fbp_06.csv") as f:
        ref = list(csv.DictReader(f))
    kw = {}
    for src, dst in COLS.items():
        vals = [r[src] for r in rows]
        kw[dst] = np.array(vals) if dst == "fueltype" else np.array([num(v) for v in vals])
    out = fbp.fbp(**kw)
    worst, bad = {}, []
    for i, r in enumerate(ref):
        for col, want in r.items():
            if col == "ID":
                continue
            got = out[col][i]
            if col == "FD":
                if str(got) != want:
                    bad.append((i + 1, col, got, want))
                continue
            w, g = float(want), float(got)
            tol = max(abs(w) * 1e-3, 1e-4)  # 4 significant digits
            err = abs(g - w)
            worst[col] = max(worst.get(col, 0.0), err / max(abs(w), 1e-9) if abs(w) > 1e-4 else err)
            if not (err <= tol or (np.isnan(w) and np.isnan(g))):
                bad.append((i + 1, col, round(g, 6), w))
    print(f"{len(ref)} fixture rows x {len(ref[0]) - 1} outputs")
    for col in ref[0]:
        if col not in ("ID", "FD"):
            print(f"  {col:6s} max rel err {worst.get(col, 0):.2e}")
    if bad:
        print(f"{len(bad)} MISMATCHES:")
        for b in bad[:40]:
            print("  row %d %s got %s want %s" % b)
        return 1
    print("all outputs match cffdrs to 4 significant digits")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
