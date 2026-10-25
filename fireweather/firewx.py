"""Daily fire weather forecast for a point, in tabular form.

Two independent sources, side by side:

- CWFIS daily grids (2 km, EPSG:3978, WCS with a TIME dimension). The dated series runs
  from the past to about 8 days ahead; recent dates are interpolated observations and the
  dates from today on behave as CWFIS's forecast (CWFIS doesn't label the boundary, so the
  report says it's inferred). Noon weather, FWI1987 codes and indices, FBP outputs for the
  cell's fuel, and the CWFIS fire danger rating.
- RDPS (this app's model data, ~3.5 days): noon-LST weather and FWI1987 codes recomputed
  daily (fwi87.py, cffdrs-validated) from a CWFIS seed, plus afternoon extremes, gusts,
  mixing/ventilation and the hourly FWI2025 peaks already computed for the viewer.
"""
import io
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone

import numpy as np
import requests

from . import config, fwi87, report

log = logging.getLogger("firewx")
WCS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/wcs"
CWFIS_VARS = ("temp", "rh", "ws", "wdir", "precip", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr",
              "hfi", "ros", "cfb", "sfc", "tfc", "ft", "fdr", "fmc")
FBP_VARS = ("hfi", "ros", "cfb", "sfc", "tfc", "ft", "fmc")
FDR_LABELS = ["Low", "Moderate", "High", "Very High", "Extreme"]
FT_LABELS = {1: "Surface", 2: "Intermittent crown", 3: "Crown"}
_pool = ThreadPoolExecutor(16, thread_name_prefix="cwfis")
_session = requests.Session()
_cache, _cache_lock = {}, threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def cwfis_date_range():
    """(first, last) dates of the CWFIS dated daily series (from DescribeCoverage)."""
    def fetch():
        r = _session.get(WCS, timeout=60, params={"service": "WCS", "version": "2.0.1",
                                                  "request": "DescribeCoverage", "coverageId": "public__fwi"})
        r.raise_for_status()
        import re
        end = re.search(r"<gml:endPosition>(\d{4}-\d\d-\d\d)", r.text)
        return date.fromisoformat(end.group(1)) if end else None
    try:
        return _cached("cwfis-range", 3600, fetch)
    except Exception as e:
        log.warning("CWFIS date range unavailable: %s", e)
        return None


FALLBACK_M = 5000  # if the point's cell has no data (e.g. water), use the nearest valid cell within this


def _cwfis_value(var, d, x, y):
    """Value of a CWFIS daily grid at the point -> (value, km to the cell used) or (None, None).

    Takes the cell containing the point; if that cell has no data, the nearest valid cell
    within FALLBACK_M (not for FBP outputs, where no data means no fuel).
    """
    import tifffile
    logging.getLogger("tifffile").setLevel(logging.ERROR)
    pad = 1500 if var in FBP_VARS else FALLBACK_M
    r = _session.get(WCS, timeout=60, params=[
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", f"public__{var}"), ("format", "image/geotiff"),
        ("subset", f"E({x - pad},{x + pad})"), ("subset", f"N({y - pad},{y + pad})"),
        ("subset", f'time("{d.isoformat()}T00:00:00.000Z")')])
    if r.status_code != 200 or not r.content[:2] in (b"II", b"MM"):
        return None, None
    with tifffile.TiffFile(io.BytesIO(r.content)) as t:
        page = t.pages[0]
        a = page.asarray().astype(float)
        tags = page.tags
        if "ModelTransformationTag" in tags:
            m = tags["ModelTransformationTag"].value
            x0, sx, y0, sy = m[3], m[0], m[7], m[5]
        else:
            scale, tie = tags["ModelPixelScaleTag"].value, tags["ModelTiepointTag"].value
            x0, sx, y0, sy = tie[3], scale[0], tie[4], -scale[1]
    valid = np.isfinite(a) & (a > -1e5)
    if var in FBP_VARS:
        valid &= a > -100  # CWFIS marks non-fuel cells (water, agriculture, urban) with -101 in the FBP grids
    # pixel centres relative to the point, in metres
    cx = x0 + (np.arange(a.shape[1]) + 0.5) * sx - x
    cy = y0 + (np.arange(a.shape[0]) + 0.5) * sy - y
    dist = np.hypot(cx[None, :], cy[:, None])
    row = min(max(int((y - y0) // sy), 0), a.shape[0] - 1)
    col = min(max(int((x - x0) // sx), 0), a.shape[1] - 1)
    if valid[row, col]:
        v, km = float(a[row, col]), 0.0
    elif var not in FBP_VARS and valid.any():
        k = np.unravel_index(np.argmin(np.where(valid, dist, np.inf)), a.shape)
        v, km = float(a[k]), float(dist[k]) / 1000
    else:
        return None, None
    if var == "wdir":
        v %= 360  # CWFIS stores -180..180, direction the wind blows from
    return round(v, 2), round(km, 1)


def cwfis_daily(lat, lon, first, last, variables=CWFIS_VARS):
    """{date: {var: value}} from the CWFIS dated grids at the point (2 km cell).

    Each row also carries `cell_km`: distance to the cell used when the point's own cell
    had no FWI data (0 when it did).
    """
    from pyproj import Transformer
    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(lon, lat)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    jobs = [(v, d) for d in days for v in variables]
    key = lambda v, d: f"cwfis:{lat:.3f},{lon:.3f}:{v}:{d}"
    vals = _pool.map(lambda j: _cached(key(*j), 3 * 3600, lambda: _cwfis_value(j[0], j[1], x, y)), jobs)
    out = {d: {} for d in days}
    for (v, d), (val, km) in zip(jobs, vals):
        out[d][v] = val
        if v == "fwi":
            out[d]["cell_km"] = km
    for d, row in out.items():
        row["fdr_label"] = FDR_LABELS[int(row["fdr"])] if row.get("fdr") is not None and 0 <= row["fdr"] <= 4 else None
        row["ft_label"] = FT_LABELS.get(int(row["ft"])) if row.get("ft") else None
    return {d: row for d, row in out.items() if row.get("fwi") is not None or row.get("temp") is not None}


def rdps_daily(run, lat, lon, seed):
    """Daily rows from the RDPS hourly point series for each local date with a 1200 LST hour.

    FWI1987 codes are chained day to day from `seed` (CWFIS FFMC/DMC/DC for the day before).
    """
    recs, cell_lat, cell_lon = report.point_series(run, lat, lon)
    tz = report.local_zone(lat, lon)
    report._decorate(recs, tz)
    std = tz.utcoffset(datetime(2000, 1, 15)) if hasattr(tz, "utcoffset") else timedelta(0)
    by_utc = {r["utc"]: r for r in recs}
    ffmc, dmc, dc = seed["ffmc"], seed["dmc"], seed["dc"]
    rows = []
    for r in recs:
        local_std = r["utc"] + std
        if local_std.hour != 12:
            continue
        d = local_std.date()
        prev = by_utc.get(r["utc"] - timedelta(hours=24))
        rain24 = (r.get("precip") or 0) - ((prev or {}).get("precip") or 0)
        t, rh, ws = r["temp"], min(max(r["rh"], 0), 100), r["wspd"]
        ffmc = float(fwi87.daily_ffmc(ffmc, t, rh, ws, rain24))
        dmc = float(fwi87.duff_moisture_code(dmc, t, rh, rain24, lat, d.month))
        dc = float(fwi87.drought_code(dc, t, rh, rain24, lat, d.month))
        isi = float(fwi87.initial_spread_index(ffmc, ws))
        bui = float(fwi87.buildup_index(dmc, dc))
        fwi = float(fwi87.fire_weather_index(isi, bui))
        day = report._window(recs, datetime.combine(d, dtime(0), tz), datetime.combine(d, dtime(23), tz))
        afternoon = report._window(recs, datetime.combine(d, dtime(10), tz), datetime.combine(d, dtime(20), tz))
        mx = lambda k, w=day: max((x[k] for x in w if x.get(k) is not None), default=None)
        mn = lambda k, w=day: min((x[k] for x in w if x.get(k) is not None), default=None)
        precip_day = sum(x["precip_1h"] for x in day if x.get("precip_1h") is not None)
        rows.append({
            "date": d.isoformat(), "noon_local": r["local"], "rain24_partial": prev is None,
            "temp": round(t, 1), "rh": round(rh), "ws": round(ws), "wdir": round(r["wdir"]) if r.get("wdir") is not None else None,
            "rain24": round(rain24, 1),
            "ffmc": round(ffmc, 1), "dmc": round(dmc, 1), "dc": round(dc, 1),
            "isi": round(isi, 1), "bui": round(bui, 1), "fwi": round(fwi, 1),
            "dsr": round(float(fwi87.daily_severity_rating(fwi)), 2),
            "max_temp": mx("temp"), "min_rh": mn("rh"), "max_wind": mx("wspd"), "max_gust": mx("gust"),
            "precip_day": round(precip_day, 1), "day_complete": len(day) >= 23,
            "max_mixing": mx("pbl", afternoon), "max_vi": mx("vi", afternoon),
            "fwi2025_max": mx("fwi", afternoon), "isi2025_max": mx("isi", afternoon),
            "ffmc2025_max": mx("ffmc", afternoon), "fwi2025_noon": r.get("fwi"),
        })
    return rows, (cell_lat, cell_lon), tz


HFI_CLASS_BOUNDS = (10, 500, 2000, 4000, 10000)  # FBP head fire intensity classes 1-6 (kW/m)


def area_fuels(lat, lon, radius_km, min_share=0.01):
    """Burnable FBP fuel types within radius_km (CWFIS 30 m fuel grid), with area shares."""
    from . import firemodel
    codes, (x0, sx, y0, sy) = firemodel.fetch_fuel(lat, lon, radius_km)
    h, w = codes.shape
    lons = x0 + (np.arange(w) + 0.5) * sx
    lats = y0 + (np.arange(h) + 0.5) * sy
    dy = (lats[:, None] - lat) * 111.32
    dx = (lons[None, :] - lon) * 111.32 * math.cos(math.radians(lat))
    inside = np.hypot(dx, dy) <= radius_km
    vals, counts = np.unique(codes[inside], return_counts=True)
    burnable = {int(v): int(c) for v, c in zip(vals, counts)
                if int(v) in firemodel.FUEL_CODES and firemodel.FUEL_CODES[int(v)][0] not in ("NF", "WA")}
    total = sum(burnable.values())
    out = []
    for code, c in sorted(burnable.items(), key=lambda t: -t[1]):
        if total and c / total >= min_share:
            ft, pc = firemodel.FUEL_CODES[code]
            out.append({"code": code, "label": firemodel.FUEL_LABELS.get(code, str(code)), "fuel": ft, "pc": pc,
                        "share": round(c / total, 3)})
    cell_area = abs(sx * 111.32 * math.cos(math.radians(lat)) * sy * 111.32)
    return out, {"burnable_km2": round(float(total * cell_area), 1), "area_km2": round(float(inside.sum() * cell_area), 1),
                 "unclassified_km2": round(float((codes[inside] == -9999).sum() * cell_area), 1)}


def area_fbp(fuels, lat, lon, d, ffmc, bui, ws, curing=None):
    """FBP (flat ground) for each area fuel type on date d; returns (per-fuel dict, worst case)."""
    from . import cffdrs, fbp, firemodel
    if not fuels or None in (ffmc, bui, ws):
        return None, None
    leaf = firemodel.leaf_state(d, "auto")
    greenup = date(d.year, 3, 12) if d >= date(d.year, 3, 12) else date(d.year - 1, 3, 12)
    cc = curing if curing is not None else float(cffdrs.seasonal_curing((d - greenup).days))
    ft = []
    for f in fuels:
        t = f["fuel"]
        if t == "D12":
            t = "D1"
        elif t == "M12":
            t = "M2" if leaf == "green" else "M1"
        ft.append(t)
    res = fbp.fbp(np.array(ft), ffmc=ffmc, bui=bui, ws=ws, wd=0, gs=0, aspect=0, lat=lat, long=lon, elv=0,
                  dj=d.timetuple().tm_yday, pc=np.array([f["pc"] or 50 for f in fuels], dtype=float), cc=cc,
                  gfl=0.35, hr=1, secondary=False)
    per = {}
    for i, f in enumerate(fuels):
        ros, hfi = float(res["ROS"][i]), float(res["HFI"][i])
        if f["fuel"] == "D12" and leaf == "green":  # D-2: 20% of D-1 spread
            ros, hfi = ros * 0.2, hfi * 0.2
        per[f["code"]] = {"ros": round(ros, 2), "hfi": round(hfi), "cfb": round(float(res["CFB"][i]), 2),
                          "fd": str(res["FD"][i]), "hfi_class": 1 + sum(hfi >= b for b in HFI_CLASS_BOUNDS)}
    worst_code = max(per, key=lambda c: per[c]["hfi"])
    worst = {"code": worst_code, "label": next(f["label"] for f in fuels if f["code"] == worst_code),
             **per[worst_code], "curing": round(cc), "leaf": leaf}
    return per, worst


def build(store, lat, lon, run=None, past_days=2, radius_km=10.0, curing=None, with_fbp=True):
    summ = {r["run"]: r for r in store.summary()}
    if run is None:
        run = next((r for r, s in sorted(summ.items(), reverse=True) if s["complete"]), None) \
            or next((r for r, s in sorted(summ.items(), reverse=True) if len(s["hours"]) >= 49), None)
    if run not in summ:
        raise LookupError("no model run available")
    tz = report.local_zone(lat, lon)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    last = cwfis_date_range() or today + timedelta(days=7)
    cwfis = cwfis_daily(lat, lon, today - timedelta(days=past_days), last)

    # seed the RDPS FWI1987 chain from CWFIS for the day before the run's first noon
    run_t0 = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    first_noon = (run_t0.astimezone(tz) + timedelta(hours=1)).date()
    seed_day = first_noon - timedelta(days=1)
    seed_src = cwfis.get(seed_day) or cwfis_daily(lat, lon, seed_day, seed_day).get(seed_day) or {}
    seed = {k: seed_src.get(k) for k in ("ffmc", "dmc", "dc")}
    defaults = {"ffmc": 85.0, "dmc": 6.0, "dc": 15.0}
    seeded = all(v is not None for v in seed.values())
    seed = {k: (v if v is not None else defaults[k]) for k, v in seed.items()}
    rdps, cell, tz = rdps_daily(run, lat, lon, seed)

    days = sorted({*(d.isoformat() for d in cwfis), *(r["date"] for r in rdps)})
    by_rdps = {r["date"]: r for r in rdps}
    fuels, fuel_area = [], None
    if with_fbp:  # optional section: the fuel inventory and per-fuel FBP add a few seconds
        try:
            fuels, fuel_area = area_fuels(lat, lon, radius_km)
        except Exception as e:
            log.warning("fuel inventory unavailable: %s", e)
    rows = []
    for d in days:
        dd = date.fromisoformat(d)
        cw, rd = cwfis.get(dd), by_rdps.get(d)
        cw_per, cw_worst = area_fbp(fuels, lat, lon, dd, cw.get("ffmc"), cw.get("bui"), cw.get("ws"), curing) if cw else (None, None)
        rd_per, rd_worst = area_fbp(fuels, lat, lon, dd, rd["ffmc"], rd["bui"], rd["ws"], curing) if rd else (None, None)
        rows.append({
            "date": d,
            "kind": "past" if dd < today else "today" if dd == today else "forecast",
            "cwfis": cw,
            "rdps": rd,
            "fbp_cwfis": {"by_fuel": cw_per, "worst": cw_worst} if cw_per else None,
            "fbp_rdps": {"by_fuel": rd_per, "worst": rd_worst} if rd_per else None,
        })
    return {
        "meta": {"lat": lat, "lon": lon, "grid_lat": round(cell[0], 3), "grid_lon": round(cell[1], 3),
                 "timezone": str(tz), "today": today.isoformat(), "run": run, "run_complete": summ[run]["complete"],
                 "issued": datetime.now(timezone.utc).astimezone(tz).isoformat(),
                 "cwfis_last": last.isoformat() if last else None,
                 "seed": {"date": seed_day.isoformat(), "source": "CWFIS daily grid" if seeded else "FWI startup defaults",
                          **{k: round(v, 1) for k, v in seed.items()}},
                 "area": {"radius_km": radius_km, "fuels": fuels, "included": with_fbp, **(fuel_area or {})}},
        "days": rows,
    }
