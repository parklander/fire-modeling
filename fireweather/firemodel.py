"""Raster fire growth with the FBP System, driven by the RDPS hourly forecast.

Pipeline for one ignition:
1. Fuel: CWFIS FBP Fuel Types 2024 (30 m) clipped around the ignition (WCS), mapped to
   FBP fuel types; elevation from CWFIS elevation_250m (EPSG:3978) -> slope and aspect.
2. Weather: RDPS hourly point forecast at the ignition, converted to FWI1987 inputs
   (hourly FFMC; daily DMC/DC/BUI updated at 1200 LST), started from CWFIS FWI1987 codes.
3. Fire behaviour: FBP (fbp.py) for each hour. Weather is uniform over the domain, so FBP
   is evaluated once per unique (fuel, conifer %, slope, aspect) combination and scattered.
4. Growth: minimum travel time on the grid. Each cell spreads to 16 neighbours (incl.
   knight moves) with the FBP elliptical spread rate in that direction (Van Wagner 1969
   ellipse: head ROS, back ROS, length-to-breadth, net spread direction RAZ). Time
   stages (10–15 min early, hourly later) use that hour's weather; a super-source edge
   per front cell carries its arrival time so fronts continue smoothly across stages.
   Point-ignition acceleration (FBP Eq. 70/81) scales ROS and LB early on.

Not modelled: spotting, suppression, diurnal wind variation within an hour, breaching
of narrow non-fuel by knight moves is possible (as in many raster models).
"""
import base64
import gzip
import io
import logging
import math
import threading
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import requests
from scipy.ndimage import map_coordinates
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from . import cffdrs, config, externals, fbp, fwi87, fwi_engine, report

log = logging.getLogger("firemodel")
WCS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/wcs"
CACHE_DIR = config.DATA_DIR / "fuel_cache"
_lock = threading.Lock()

# CWFIS FBP Fuel Types 2024 grid codes -> (FBP fuel, conifer % for mixedwood, deciduous type)
FUEL_CODES = {
    1: ("C1", 0), 2: ("C2", 0), 3: ("C3", 0), 4: ("C4", 0), 5: ("C5", 0), 6: ("C6", 0), 7: ("C7", 0),
    11: ("D1", 0), 13: ("D12", 0), 21: ("S1", 0), 22: ("S2", 0), 23: ("S3", 0),
    31: ("O1A", 0), 32: ("O1B", 0),
    415: ("M1", 15), 425: ("M1", 25), 450: ("M1", 50), 475: ("M1", 75),
    525: ("M2", 25), 550: ("M2", 50), 575: ("M2", 75),
    625: ("M12", 25), 650: ("M12", 50), 675: ("M12", 75),
    101: ("NF", 0), 102: ("WA", 0), 105: ("NF", 0),
}
FUEL_LABELS = {
    1: "C-1 Spruce-Lichen Woodland", 2: "C-2 Boreal Spruce", 3: "C-3 Mature Jack/Lodgepole Pine",
    4: "C-4 Immature Jack/Lodgepole Pine", 5: "C-5 Red and White Pine", 6: "C-6 Conifer Plantation",
    7: "C-7 Ponderosa Pine/Douglas-fir", 11: "D-1 Leafless Aspen", 13: "D-1/D-2 Aspen",
    31: "O-1a Matted Grass", 32: "O-1b Standing Grass", 101: "Non-fuel", 102: "Water",
    105: "Vegetated Non-Fuel", 415: "M-1 Mixedwood leafless 15% conifer", 625: "M-1/M-2 Mixedwood 25% conifer",
    650: "M-1/M-2 Mixedwood 50% conifer", 675: "M-1/M-2 Mixedwood 75% conifer", -9999: "Unclassified (no FBP type)",
}
# 32-direction stencil (all coprime offsets up to 3 cells). Fewer directions make elongated
# ellipses zig-zag between stencil angles, which shortens the head and widens the flanks.
OFFSETS = [(dr, dc) for dr in range(-3, 4) for dc in range(-3, 4)
           if (dr, dc) != (0, 0) and math.gcd(abs(dr), abs(dc)) == 1]


# ---- spatial inputs -----------------------------------------------------------------
def _tiff(content):
    import tifffile
    logging.getLogger("tifffile").setLevel(logging.ERROR)
    with tifffile.TiffFile(io.BytesIO(content)) as tif:
        page = tif.pages[0]
        a = page.asarray()
        tags = page.tags
        if "ModelTransformationTag" in tags:
            m = tags["ModelTransformationTag"].value
            return a, (m[3], m[0], m[7], m[5])
        scale, tie = tags["ModelPixelScaleTag"].value, tags["ModelTiepointTag"].value
        return a, (tie[3], scale[0], tie[4], -scale[1])


def fetch_fuel(lat, lon, radius_km):
    """Fuel codes on the native ~30 m lat/lon grid around the point (cached)."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    key = f"{lat:.3f}_{lon:.3f}_{radius_km:.1f}".replace("-", "m")
    path = CACHE_DIR / f"{key}.npz"
    if path.exists():
        z = np.load(path)
        return z["codes"], tuple(z["geo"])
    r = requests.get(WCS, timeout=180, params=[
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", "public__cffdrs_fbp_fuel_types"), ("format", "image/geotiff"),
        ("subsettingCrs", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("subset", f"Long({lon - dlon},{lon + dlon})"), ("subset", f"Lat({lat - dlat},{lat + dlat})")])
    r.raise_for_status()
    codes, geo = _tiff(r.content)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, codes=codes.astype(np.int16), geo=np.array(geo))
    return codes.astype(np.int16), geo


def fetch_elevation(lats, lons):
    """Elevation (m) at the given lat/lon arrays, bilinear from CWFIS elevation_250m (EPSG:3978)."""
    from pyproj import Transformer
    t = Transformer.from_crs(4326, 3978, always_xy=True)
    x, y = t.transform(lons, lats)
    pad = 600
    r = requests.get(WCS, timeout=120, params=[
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", "public__elevation_250m"), ("format", "image/geotiff"),
        ("subset", f"E({x.min() - pad},{x.max() + pad})"), ("subset", f"N({y.min() - pad},{y.max() + pad})")])
    r.raise_for_status()
    dem, (x0, sx, y0, sy) = _tiff(r.content)
    dem = dem.astype(float)
    dem[dem < -1000] = np.nan
    if np.isnan(dem).all():
        return np.zeros_like(lats, dtype=float)
    dem = np.where(np.isnan(dem), np.nanmean(dem), dem)
    # pixel-centre coordinates
    col = (x - x0) / sx - 0.5
    row = (y - y0) / sy - 0.5
    return map_coordinates(dem, [row.ravel(), col.ravel()], order=1, mode="nearest").reshape(lats.shape)


def build_domain(lat, lon, radius_km, res_m, unclassified="NF"):
    codes, (x0, sx, y0, sy) = fetch_fuel(lat, lon, radius_km)
    lat_m = 111320.0
    lon_m = 111320.0 * math.cos(math.radians(lat))
    native_dx, native_dy = sx * lon_m, -sy * lat_m
    step = max(1, int(round(res_m / max(native_dx, native_dy))))
    codes = codes[::step, ::step]
    sx, sy = sx * step, sy * step
    h, w = codes.shape
    lons = x0 + (np.arange(w) + 0.5) * sx
    lats = y0 + (np.arange(h) + 0.5) * sy
    glon, glat = np.meshgrid(lons, lats)
    try:
        elev = fetch_elevation(glat, glon)
    except Exception as e:
        log.warning("elevation unavailable (%s); assuming flat terrain", e)
        elev = np.zeros_like(glat)
    dx, dy = sx * lon_m, -sy * lat_m
    dz_drow, dz_dcol = np.gradient(elev)
    dzdx, dzdy = dz_dcol / dx, -dz_drow / dy  # east, north
    slope = 100 * np.hypot(dzdx, dzdy)
    aspect = np.degrees(np.arctan2(-dzdx, -dzdy)) % 360  # direction the slope faces (downhill)
    fuel = np.full(codes.shape, "NF", dtype="<U3")
    pc = np.zeros(codes.shape)
    for code, (ft, p) in FUEL_CODES.items():
        m = codes == code
        fuel[m], pc[m] = ft, p
    if unclassified != "NF":
        fuel[codes == -9999] = unclassified
    return {"codes": codes, "fuel": fuel, "pc": pc, "elev": elev, "slope": slope, "aspect": aspect,
            "lat": glat, "lon": glon, "west": x0, "north": y0, "sx": sx, "sy": sy, "dx": dx, "dy": dy,
            "h": h, "w": w}


# ---- weather stream (FWI1987) ---------------------------------------------------------
def leaf_state(d, mode):
    if mode in ("leafless", "green"):
        return mode
    return "green" if date(d.year, 5, 25) <= d <= date(d.year, 9, 15) else "leafless"


def weather_stream(run, lat, lon, start_utc, hours, ffmc0=None, dmc0=None, dc0=None):
    """Hourly FWI1987 inputs at the ignition from the RDPS run start to start+hours."""
    recs, _, _ = report.point_series(run, lat, lon)
    tz = report.local_zone(lat, lon)
    seed = fwi_engine.fetch_cwfis_seed(np.array([lat]), np.array([lon]))
    fwi_engine.fill_from_stations(np.array([lat]), np.array([lon]), seed)
    codes = {k: (float(seed[k][0]) if np.isfinite(seed[k][0]) else None) for k in ("ffmc", "dmc", "dc")}
    stations = externals.cwfis_station_codes()
    seed_date = date.fromisoformat(stations["file_date"]) if stations else None
    source = "CWFIS (grid/stations)" if codes["dc"] is not None else "FWI startup defaults"
    ffmc = ffmc0 if ffmc0 is not None else (codes["ffmc"] or cffdrs.FFMC_DEFAULT)
    dmc = dmc0 if dmc0 is not None else (codes["dmc"] if codes["dmc"] is not None else cffdrs.DMC_DEFAULT)
    dc = dc0 if dc0 is not None else (codes["dc"] if codes["dc"] is not None else cffdrs.DC_DEFAULT)
    std = tz.utcoffset(datetime(2000, 1, 15)) if hasattr(tz, "utcoffset") else timedelta(0)
    by_utc = {r["utc"]: r for r in recs}
    end_utc = start_utc + timedelta(hours=hours)
    out = []
    for r in recs:
        if r["utc"] > end_utc:
            break
        ws, wd, rh, t = r.get("wspd"), r.get("wdir"), r.get("rh"), r.get("temp")
        if None in (ws, wd, rh, t):
            continue
        rh = min(max(rh, 0), 100)
        if r["fh"] > 0:
            ffmc = float(fwi87.hourly_ffmc(t, rh, ws, r.get("precip_1h") or 0.0, ffmc, 1.0))
        # daily DMC/DC at 1200 LST (standard time), using the forecast noon weather
        local_std = r["utc"] + std
        if local_std.hour == 12 and (seed_date is None or local_std.date() > seed_date):
            prev = by_utc.get(r["utc"] - timedelta(hours=24))
            rain24 = (r.get("precip") or 0) - ((prev or {}).get("precip") or 0)
            mon = local_std.month
            dmc = float(fwi87.duff_moisture_code(dmc, t, rh, rain24, lat, mon))
            dc = float(fwi87.drought_code(dc, t, rh, rain24, lat, mon))
        bui = float(fwi87.buildup_index(dmc, dc))
        isi = float(fbp.initial_spread_index(np.array(ffmc), np.array(ws)))
        if r["utc"] >= start_utc - timedelta(hours=1):
            out.append({"utc": r["utc"].isoformat(), "local": r["utc"].astimezone(tz).isoformat(),
                        "temp": t, "rh": rh, "ws": ws, "wd": wd, "precip": r.get("precip_1h"),
                        "ffmc": round(ffmc, 1), "dmc": round(dmc, 1), "dc": round(dc, 1),
                        "bui": round(bui, 1), "isi": round(isi, 2)})
    return out, {"source": source, "seed_date": seed_date.isoformat() if seed_date else None,
                 "ffmc": codes["ffmc"], "dmc": codes["dmc"], "dc": codes["dc"]}, tz


# ---- fire behaviour per stage --------------------------------------------------------------
def _fbp_unique(dom, wx, opts, dj):
    """Run FBP once per unique cell type and scatter. Returns dict of 2D arrays."""
    fuel = dom["fuel"].copy()
    leaf = opts["leaf"]
    fuel[fuel == "D12"] = "D1"
    fuel[fuel == "M12"] = "M2" if leaf == "green" else "M1"
    burnable = ~np.isin(fuel, ("NF", "WA"))
    slope_q = np.round(np.clip(dom["slope"], 0, 150)).astype(np.int16)
    aspect_q = (np.round(dom["aspect"] / 10) % 36).astype(np.int16)
    aspect_q[slope_q == 0] = 0
    ftypes, fidx = np.unique(fuel, return_inverse=True)
    key = ((fidx.reshape(fuel.shape).astype(np.int64) * 101 + dom["pc"].astype(np.int64)) * 151
           + slope_q) * 36 + aspect_q
    ukeys, first, inv = np.unique(key.ravel(), return_index=True, return_inverse=True)
    f_u = fuel.ravel()[first]
    res = fbp.fbp(f_u, ffmc=wx["ffmc"], bui=wx["bui"], ws=wx["ws"], wd=wx["wd"],
                  gs=slope_q.ravel()[first].astype(float), aspect=aspect_q.ravel()[first] * 10.0,
                  lat=dom["lat"].ravel()[first], long=dom["lon"].ravel()[first], elv=dom["elev"].ravel()[first],
                  dj=dj, pc=dom["pc"].ravel()[first], cc=opts["curing"], gfl=opts["gfl"], hr=1.0)
    out = {k: res[k][inv].reshape(fuel.shape) for k in ("ROS", "BROS", "LB", "RAZ", "HFI", "CFB", "SFC", "RSO",
                                                       "TFC", "FD", "ISI")}
    # D-2 (green aspen): FBP gives no equations; the convention is 20% of D-1 spread.
    if leaf == "green":
        d2 = dom["fuel"] == "D12"
        for k in ("ROS", "BROS"):
            out[k] = np.where(d2, out[k] * 0.2, out[k])
        out["HFI"] = np.where(d2, out["HFI"] * 0.2, out["HFI"])
    cfl = fbp.crown_fuel_load(fuel, np.zeros(fuel.shape))
    out["CFL"] = np.where(burnable, cfl, 0)
    out["burnable"] = burnable & (out["ROS"] > 1e-4)
    out["fuel"] = fuel
    return out


def ros_at(R, B, LB, raz_deg, az_deg):
    """Spread rate (m/min) from the ignition point toward azimuth az on the FBP ellipse."""
    a = (R + B) / 2
    c = (R - B) / 2
    b = a / np.maximum(LB, 1.0)
    psi = np.radians(az_deg - raz_deg)
    cp, sp = np.cos(psi), np.sin(psi)
    with np.errstate(divide="ignore", invalid="ignore"):
        A = cp * cp / (a * a) + sp * sp / (b * b)
        q = c * cp / (a * a)
        s = (q + np.sqrt(q * q - A * (c * c / (a * a) - 1))) / A
    return np.where(a > 0, s, 0.0)


# ---- growth ---------------------------------------------------------------------------
def _stage_bounds(duration_min):
    early = [0, 10, 20, 30, 45, 60]
    return sorted({t for t in early if t < duration_min} | set(range(60, int(duration_min) + 1, 60)) | {duration_min})


def simulate(store, run, lat, lon, start_utc, duration_h, radius_km=8.0, res_m=None, curing=None,
             leaf="auto", unclassified="NF", gfl=0.35, ffmc0=None, dmc0=None, dc0=None):
    t_start = datetime.now()
    if res_m is None:
        res_m = 30 if radius_km <= 6 else 60 if radius_km <= 12 else 90 if radius_km <= 20 else 120
    with _lock:  # fuel cache writes and memory: one simulation at a time
        dom = build_domain(lat, lon, radius_km, res_m, unclassified)
    h, w = dom["h"], dom["w"]
    stream, seed_info, tz = weather_stream(run, lat, lon, start_utc, duration_h, ffmc0, dmc0, dc0)
    if not stream:
        raise ValueError("no forecast weather for that start time in this run")
    local_start = start_utc.astimezone(tz)
    if curing is None:
        g = date(local_start.year, 3, 12)
        days = (local_start.date() - (g if local_start.date() >= g else date(local_start.year - 1, 3, 12))).days
        curing = float(cffdrs.seasonal_curing(days))
    leaf_s = leaf_state(local_start.date(), leaf)
    opts = {"curing": curing, "gfl": gfl, "leaf": leaf_s}
    dj = local_start.timetuple().tm_yday
    by_hour = {datetime.fromisoformat(s["utc"]): s for s in stream}

    # ignition cell, snapped to the nearest burnable cell within 300 m (clicks often land on roads etc.)
    ir = int((lat - dom["north"]) / dom["sy"])
    ic = int((lon - dom["west"]) / dom["sx"])
    if not (0 <= ir < h and 0 <= ic < w):
        raise ValueError("ignition outside the fuel domain")
    can_burn = ~np.isin(dom["fuel"], ("NF", "WA"))
    snapped_m = 0.0
    if not can_burn[ir, ic]:
        rr, cc = np.nonzero(can_burn)
        dd = np.hypot((rr - ir) * dom["dy"], (cc - ic) * dom["dx"])
        if not dd.size or dd.min() > 300:
            raise ValueError(f"no burnable fuel within 300 m of the ignition "
                             f"({FUEL_LABELS.get(int(dom['codes'][ir, ic]), 'unknown')})")
        k = int(dd.argmin())
        ir, ic, snapped_m = int(rr[k]), int(cc[k]), float(dd[k])
    ig_lat, ig_lon = float(dom["lat"][ir, ic]), float(dom["lon"][ir, ic])

    arrival = np.full(h * w, np.inf)
    arrival[ir * w + ic] = 0.0
    direction = np.full(h * w, np.nan)  # azimuth the fire arrived travelling
    hfi = np.zeros(h * w)
    ftype = np.zeros(h * w, dtype=np.uint8)
    dur = duration_h * 60.0
    bounds = _stage_bounds(dur)
    fbp_cache = {}
    hourly = []
    ig_fbp = []
    dist = [math.hypot(dr * dom["dy"], dc * dom["dx"]) for dr, dc in OFFSETS]
    azs = [math.degrees(math.atan2(dc * dom["dx"], -dr * dom["dy"])) % 360 for dr, dc in OFFSETS]
    burnable_ign = None

    for s0, s1 in zip(bounds[:-1], bounds[1:]):
        hour_utc = (start_utc + timedelta(minutes=s0)).replace(minute=0, second=0, microsecond=0)
        wx = by_hour.get(hour_utc) or min(stream, key=lambda s: abs(datetime.fromisoformat(s["utc"]) - hour_utc))
        if wx["utc"] not in fbp_cache:
            fbp_cache.clear()
            fbp_cache[wx["utc"]] = _fbp_unique(dom, wx, opts, dj)
        F = fbp_cache[wx["utc"]]
        if burnable_ign is None:
            burnable_ign = bool(F["burnable"][ir, ic])
            if not burnable_ign:
                raise ValueError(f"ignition is in non-burnable fuel ({FUEL_LABELS.get(int(dom['codes'][ir, ic]), 'unknown')})")
        # point-ignition acceleration (FBP Eq. 70 / 81), at the stage midpoint
        tm = (s0 + s1) / 2
        cfb = F["CFB"]
        alpha = np.where(np.isin(F["fuel"], fbp.ALPHA_FIXED), 0.115, 0.115 - 18.8 * cfb ** 2.5 * np.exp(-8 * cfb))
        acc = 1 - np.exp(-alpha * tm)
        R, B = F["ROS"] * acc, F["BROS"] * acc
        LB = (F["LB"] - 1) * acc + 1
        RAZ = F["RAZ"]

        burned = np.isfinite(arrival).reshape(h, w)
        if not burned.any():
            break
        # active window: burned bbox grown by the farthest this stage could spread
        rr, cc = np.nonzero(burned)
        reach = float(np.nanmax(np.where(F["burnable"], R, 0))) * (s1 - s0) + 3 * max(dom["dx"], dom["dy"])
        pr, pc_ = int(reach / dom["dy"]) + 3, int(reach / dom["dx"]) + 3
        r0, r1 = max(rr.min() - pr, 0), min(rr.max() + pr + 1, h)
        c0, c1 = max(cc.min() - pc_, 0), min(cc.max() + pc_ + 1, w)
        wh, ww = r1 - r0, c1 - c0
        n = wh * ww
        sub = lambda a: a[r0:r1, c0:c1]
        Rw, Bw, LBw, RAZw, okw = sub(R), sub(B), sub(LB), sub(RAZ), sub(F["burnable"])
        arr_w = sub(arrival.reshape(h, w))
        rows_l, cols_l, data_l = [], [], []
        idx = np.arange(n).reshape(wh, ww)
        for (dr, dc), d, az in zip(OFFSETS, dist, azs):
            sr = slice(max(0, -dr), wh - max(0, dr)); sc = slice(max(0, -dc), ww - max(0, dc))
            tr = slice(max(0, dr), wh - max(0, -dr)); tc = slice(max(0, dc), ww - max(0, -dc))
            ok = okw[sr, sc] & okw[tr, tc] & ~np.isfinite(arr_w[tr, tc])
            if not ok.any():
                continue
            rs = ros_at(Rw[sr, sc][ok], Bw[sr, sc][ok], LBw[sr, sc][ok], RAZw[sr, sc][ok], az)
            rt = ros_at(Rw[tr, tc][ok], Bw[tr, tc][ok], LBw[tr, tc][ok], RAZw[tr, tc][ok], az)
            good = (rs > 1e-6) & (rt > 1e-6)
            rows_l.append(idx[sr, sc][ok][good]); cols_l.append(idx[tr, tc][ok][good])
            data_l.append(d / 2 / rs[good] + d / 2 / rt[good])
        # super-source -> front cells (burned with an unburned neighbour), weighted by arrival time
        front = np.isfinite(arr_w)
        t_ref = arr_w[front].min()
        src = idx[front]
        rows_l.append(np.full(src.size, n)); cols_l.append(src)
        data_l.append(arr_w[front] - t_ref + 1e-6)
        rows = np.concatenate(rows_l); cols = np.concatenate(cols_l); data = np.concatenate(data_l)
        g = csr_matrix((data, (rows, cols)), shape=(n + 1, n + 1))
        dist_w, pred = dijkstra(g, directed=True, indices=n, limit=s1 - t_ref, return_predecessors=True)
        t_w = dist_w[:n] + t_ref
        new = np.isfinite(t_w) & ~np.isfinite(arr_w.ravel()) & (t_w <= s1)
        if new.any():
            gidx = ((np.nonzero(new)[0] // ww) + r0) * w + (np.nonzero(new)[0] % ww) + c0
            arrival[gidx] = np.maximum(t_w[new], s0)
            # direction of arrival from the predecessor cell -> intensity in that direction
            p = pred[:n][new]
            pr_r, pr_c = p // ww, p % ww
            nr, nc = np.nonzero(new)[0] // ww, np.nonzero(new)[0] % ww
            az = np.degrees(np.arctan2((nc - pr_c) * dom["dx"], -(nr - pr_r) * dom["dy"])) % 360
            az = np.where(p >= 0, az, RAZw.ravel()[new])
            direction[gidx] = az
            Rg = R.ravel()[gidx]
            rdir = ros_at(Rg, B.ravel()[gidx], LB.ravel()[gidx], RAZ.ravel()[gidx], az)
            rso, sfc, cfl = F["RSO"].ravel()[gidx], F["SFC"].ravel()[gidx], F["CFL"].ravel()[gidx]
            cfb_d = np.where(cfl > 0, fbp.crown_fraction_burned(rdir, rso), 0)
            pcv = dom["pc"].ravel()[gidx]
            ftv = F["fuel"].ravel()[gidx]
            cfc = cfl * cfb_d * np.where(np.isin(ftv, ("M1", "M2")), pcv / 100, 1.0)
            hfi[gidx] = 300 * (sfc + cfc) * rdir
            ftype[gidx] = np.where(cfb_d >= 0.9, 3, np.where(cfb_d >= 0.1, 2, 1))
        if s1 % 60 == 0 or s1 == dur:
            done = np.isfinite(arrival) & (arrival <= s1)
            hour_mask = done & (arrival > s1 - 60)
            area_ha = done.sum() * dom["dx"] * dom["dy"] / 1e4
            grid2 = done.reshape(h, w)
            edges = (np.abs(np.diff(grid2.astype(np.int8), axis=0)).sum() * dom["dx"]
                     + np.abs(np.diff(grid2.astype(np.int8), axis=1)).sum() * dom["dy"])
            hourly.append({
                "minutes": int(s1), "local": (start_utc + timedelta(minutes=s1)).astimezone(tz).isoformat(),
                "area_ha": round(float(area_ha), 2), "perimeter_km": round(float(edges * math.pi / 4 / 1000), 2),
                "hfi_max": round(float(hfi[hour_mask].max()), 0) if hour_mask.any() else None,
                "hfi_mean": round(float(hfi[hour_mask].mean()), 0) if hour_mask.any() else None,
                "crown_fraction": round(float((ftype[hour_mask] >= 2).mean()), 3) if hour_mask.any() else None,
                "wx": {k: wx[k] for k in ("local", "temp", "rh", "ws", "wd", "ffmc", "bui", "isi")},
                "head": {k: _r(F[k][ir, ic]) for k in ("ROS", "BROS", "LB", "RAZ", "HFI", "CFB", "ISI")}
                        | {"FD": str(F["FD"][ir, ic]), "fuel": str(F["fuel"][ir, ic])},
            })

    burned = np.isfinite(arrival)
    grid_burned = burned.reshape(h, w)
    edge_hit = bool(grid_burned[:2].any() or grid_burned[-2:].any() or grid_burned[:, :2].any() or grid_burned[:, -2:].any())
    # FBP at the ignition cell for each forecast hour of the fire (head-fire potential)
    for s in stream:
        F = _fbp_unique({**dom, **{k: dom[k][ir:ir + 1, ic:ic + 1] for k in
                                   ("fuel", "pc", "slope", "aspect", "lat", "lon", "elev", "codes")}}, s, opts, dj)
        ig_fbp.append({"local": s["local"], "ffmc": s["ffmc"], "bui": s["bui"], "ws": s["ws"], "wd": s["wd"],
                       **{k: _r(F[k][0, 0]) for k in ("ISI", "ROS", "BROS", "LB", "RAZ", "HFI", "CFB", "TFC")},
                       "FD": str(F["FD"][0, 0])})
    enc = lambda a, dtype: base64.b64encode(gzip.compress(a.astype(dtype).tobytes(), 5)).decode()
    arr_out = np.where(burned, np.minimum(np.round(arrival) + 1, 65534), 0).reshape(h, w)
    fuels_present = {int(c): int(n) for c, n in zip(*np.unique(dom["codes"], return_counts=True))}
    log.info("fire sim %.1fs: %dx%d cells @ %.0f m, %.1f ha", (datetime.now() - t_start).total_seconds(),
             w, h, max(dom["dx"], dom["dy"]), hourly[-1]["area_ha"] if hourly else 0)
    return {
        "grid": {"west": dom["west"], "north": dom["north"], "sx": dom["sx"], "sy": dom["sy"], "w": w, "h": h,
                 "dx": dom["dx"], "dy": dom["dy"]},
        "arrival_u16": enc(arr_out, "<u2"),
        "hfi_u16": enc(np.minimum(hfi / 10, 65535).reshape(h, w), "<u2"),
        "ftype_u8": enc(ftype.reshape(h, w), "u1"),
        "hourly": hourly, "ignition_fbp": ig_fbp, "weather": stream, "seed": seed_info,
        "ignition": {"lat": ig_lat, "lon": ig_lon, "requested": [lat, lon], "snapped_m": round(snapped_m),
                     "fuel_code": int(dom["codes"][ir, ic]),
                     "fuel": FUEL_LABELS.get(int(dom["codes"][ir, ic]), str(dom["codes"][ir, ic])),
                     "elev": _r(dom["elev"][ir, ic]), "slope": _r(dom["slope"][ir, ic]),
                     "aspect": _r(dom["aspect"][ir, ic])},
        "options": {"start_local": local_start.isoformat(), "duration_h": duration_h, "radius_km": radius_km,
                    "res_m": round(max(dom["dx"], dom["dy"])), "curing": round(curing, 1), "leaf": leaf_s,
                    "gfl": gfl, "unclassified": unclassified, "run": run, "timezone": str(tz)},
        "fuels": [{"code": c, "label": FUEL_LABELS.get(c, str(c)), "cells": n} for c, n in
                  sorted(fuels_present.items(), key=lambda t: -t[1])],
        "edge_hit": edge_hit,
        "runtime_s": round((datetime.now() - t_start).total_seconds(), 1),
    }


def _r(v):
    v = float(v)
    return None if not np.isfinite(v) else round(v, 3)
