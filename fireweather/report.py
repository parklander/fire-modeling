"""Burn-support forecast report for a point, laid out like a prescribed-burn met support
plan: burn-day forecast at set local times, 2-day outlook, midday/afternoon updates with
overnight, FWI noon report and amendment-criteria checks.

Everything model-based comes from the RDPS frames on disk at the nearest grid point.
Classifications (stability, ventilation rating, thunderstorm risk, smoke settling) are
rule-of-thumb guidance from model fields, documented in METHODS below, and are meant
for a forecaster to review rather than issue verbatim.
"""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from . import config, externals, grid, ingest

_pool = ThreadPoolExecutor(8, thread_name_prefix="report")

HOURLY_VARS = ["temp", "rh", "wspd", "wdir", "precip"] + list(config.INDICES)
BURN_VARS = ["gust", "pbl", "cape"] + list(config.SMOKE)

PRODUCTS = {
    "morning": {"title": "0800 Burn-day forecast", "times": [10, 12, 15, 18]},
    "midday": {"title": "1200 Midday update", "times": [15, 18]},
    "afternoon": {"title": "1500 Afternoon update", "times": [18]},
    "spot": {"title": "Spot forecast", "times": None},  # next 6 hours
}

DEFAULT_THRESHOLDS = {"wind": 25.0, "gust": 40.0, "rh_min": 25.0, "cape": 500.0, "shift_deg": 45.0}

METHODS = [
    "Model: ECCC RDPS 10 km, nearest grid point; winds are 10 m in km/h, direction degrees true (from).",
    "Stability: from the model 2 m to 120 m temperature difference (ΔT; dry adiabatic is about −1.15 °C over "
    "that layer). Stable: ΔT > +0.5 °C (surface inversion). Neutral: −0.6 to +0.5. Slightly unstable: −1.6 to "
    "−0.6 (near dry adiabatic). Unstable: < −1.6 (superadiabatic). Inversion breakup is the first morning hour "
    "the inversion is gone.",
    "Mixing height: RDPS planetary boundary layer height (m above ground).",
    "Transport wind: thickness-weighted mean of the 10 m, 40 m, 120 m and 950–700 hPa winds below the "
    "mixing height.",
    f"Ventilation index = mixing height × transport wind (m²/s). Rating per BC smoke-control categories: "
    f"poor < {config.VI_POOR_BELOW}, fair {config.VI_POOR_BELOW}–{config.VI_GOOD_ABOVE}, good > {config.VI_GOOD_ABOVE}.",
    "Thunderstorm risk: from model surface CAPE (nil < 100, low < 500, moderate < 1500, high ≥ 1500 J/kg). "
    "No lightning observations are used.",
    "Precipitation: deterministic model amount only; RDPS gives no probability of precipitation.",
    "FWI: model values are FWI2025 hourly (cffdrs-ng) at 1200 LST; CWFIS station values are FWI1987 daily "
    "from noon observations. The two systems are close but not identical.",
]


@lru_cache(maxsize=1)
def _tzfinder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder()


def local_zone(lat, lon):
    name = _tzfinder().timezone_at(lat=lat, lng=lon)
    if name:
        return ZoneInfo(name)
    return timezone(timedelta(hours=round(lon / 15)))


def compass(deg):
    if deg is None:
        return None
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][
        round(deg / 22.5) % 16]


def stability(lapse):
    """Class from T(120 m) - T(2 m); dry adiabatic over the 118 m layer is about -1.15 °C."""
    if lapse is None:
        return None
    if lapse > 0.5:
        return "Stable"
    if lapse >= -0.6:
        return "Neutral"
    return "Slightly unstable" if lapse >= -1.6 else "Unstable"


def vi_rating(vi):
    if vi is None:
        return None
    return "Poor" if vi < config.VI_POOR_BELOW else "Good" if vi > config.VI_GOOD_ABOVE else "Fair"


def thunder_risk(cape):
    if cape is None:
        return None
    return "Nil" if cape < 100 else "Low" if cape < 500 else "Moderate" if cape < 1500 else "High"


def _dir_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _vector_mean_dir(recs, spd="wspd", d="wdir"):
    u = sum(-r[spd] * math.sin(math.radians(r[d])) for r in recs if r.get(spd) is not None and r.get(d) is not None)
    v = sum(-r[spd] * math.cos(math.radians(r[d])) for r in recs if r.get(spd) is not None and r.get(d) is not None)
    if u == 0 and v == 0:
        return None
    return math.degrees(math.atan2(-u, -v)) % 360


def _extreme(recs, key, fn=max):
    vals = [r for r in recs if r.get(key) is not None]
    if not vals:
        return None
    r = fn(vals, key=lambda r: r[key])
    return {"value": r[key], "time": r["local"]}


def point_series(run, lat, lon):
    found = grid.point_pixel(lat, lon)
    if found is None:
        raise ValueError("location is outside the model display area")
    idx, cell_lat, cell_lon = found

    def read(var, fh):
        p = ingest.frame_path(run, var, fh)
        if not p.exists():
            return var, fh, None
        v = grid.decode_pixel(p.read_bytes(), var, idx)
        return var, fh, None if v is None else round(v, 2)

    jobs = [(v, fh) for v in HOURLY_VARS for fh in config.FORECAST_HOURS]
    jobs += [(v, fh) for v in BURN_VARS for fh in config.BURN_HOURS]
    t0 = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    recs = {fh: {"fh": fh, "utc": t0 + timedelta(hours=fh)} for fh in config.FORECAST_HOURS}
    for var, fh, val in _pool.map(lambda j: read(*j), jobs):
        recs[fh][var] = val
    out = [recs[fh] for fh in config.FORECAST_HOURS if recs[fh].get("temp") is not None]
    prev = None
    for r in out:
        r["precip_1h"] = None if prev is None or r.get("precip") is None or prev.get("precip") is None \
            else round(max(r["precip"] - prev["precip"], 0.0), 2)
        prev = r
    return out, cell_lat, cell_lon


def _decorate(recs, tz):
    for r in recs:
        loc = r["utc"].astimezone(tz)
        r["local_dt"] = loc
        r["local"] = loc.isoformat()
        r["wdir_card"] = compass(r.get("wdir"))
        r["twdir_card"] = compass(r.get("twdir"))
        r["stability"] = stability(r.get("lapse"))
        r["vi_rating"] = vi_rating(r.get("vi"))
        r["thunder"] = thunder_risk(r.get("cape"))


def _public(r):
    return {k: v for k, v in r.items() if k not in ("utc", "local_dt")}


def _at(recs, when, tolerance_h=1.5):
    best = min(recs, key=lambda r: abs((r["local_dt"] - when).total_seconds()), default=None)
    if best is None or abs((best["local_dt"] - when).total_seconds()) > tolerance_h * 3600:
        return None
    return best


def _window(recs, start, end):
    return [r for r in recs if start <= r["local_dt"] <= end]


def wind_shifts(recs, shift_deg, min_speed=5.0):
    """Direction changes > shift_deg within 3 h (both ends >= min_speed km/h)."""
    out, skip_until = [], None
    for i, r in enumerate(recs):
        if skip_until and r["local_dt"] < skip_until:
            continue
        if r.get("wdir") is None or (r.get("wspd") or 0) < min_speed:
            continue
        for later in recs[i + 1:]:
            dt = (later["local_dt"] - r["local_dt"]).total_seconds() / 3600
            if dt > 3:
                break
            if later.get("wdir") is None or (later.get("wspd") or 0) < min_speed:
                continue
            if _dir_diff(r["wdir"], later["wdir"]) > shift_deg:
                dtemp = (later.get("temp") or 0) - (r.get("temp") or 0)
                out.append({
                    "start": r["local"], "end": later["local"],
                    "from_dir": r["wdir"], "from_card": compass(r["wdir"]), "from_speed": r["wspd"],
                    "to_dir": later["wdir"], "to_card": compass(later["wdir"]), "to_speed": later["wspd"],
                    "temp_change": round(dtemp, 1),
                    "frontal": dtemp <= -3 or ((later.get("rh") or 0) - (r.get("rh") or 0)) >= 15,
                })
                skip_until = later["local_dt"] + timedelta(hours=2)
                break
    return out


def inversion_breakup(recs, day, tz):
    morning = _window(recs, datetime.combine(day, time(4), tz), datetime.combine(day, time(15), tz))
    morning = [r for r in morning if r.get("lapse") is not None]
    if not morning:
        return {"status": "unknown"}
    if not any(r["lapse"] > 0.5 for r in morning[:4]):
        return {"status": "none", "note": "No surface inversion in the early morning."}
    for i, r in enumerate(morning):
        if r["lapse"] <= 0 and all(x["lapse"] <= 0.5 for x in morning[i + 1:i + 3]):
            return {"status": "breaks", "time": r["local"]}
    return {"status": "persists", "note": "Inversion forecast to persist through the burn period."}


def summarize(recs, start, end, shift_deg):
    w = _window(recs, start, end)
    if not w:
        return None
    burn = [r for r in w if r.get("vi") is not None]
    precip = sum(r["precip_1h"] for r in w if r.get("precip_1h") is not None)
    peak_vi = _extreme(burn, "vi")
    if peak_vi:
        peak_vi["rating"] = vi_rating(peak_vi["value"])
    max_cape = _extreme(w, "cape")
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "max_temp": _extreme(w, "temp"), "min_temp": _extreme(w, "temp", min),
        "min_rh": _extreme(w, "rh", min), "max_rh": _extreme(w, "rh"),
        "max_wind": _extreme(w, "wspd"), "max_gust": _extreme(w, "gust"),
        "prevailing_dir": compass(_vector_mean_dir(w)),
        "max_mixing_height": _extreme(burn, "pbl"),
        "peak_vi": peak_vi,
        "precip_total": round(precip, 1),
        "max_cape": max_cape, "thunder": thunder_risk(max_cape["value"]) if max_cape else None,
        "max_fwi": _extreme(w, "fwi"), "max_isi": _extreme(w, "isi"),
        "wind_shifts": wind_shifts(w, shift_deg),
        "cadence_h": 3 if any(r["fh"] > 48 for r in w) else 1,
    }


def overnight(recs, day, tz):
    start = datetime.combine(day, time(18), tz)
    end = datetime.combine(day + timedelta(days=1), time(8), tz)
    w = _window(recs, start, end)
    if not w:
        return None
    stable = [r for r in w if r.get("stability") == "Stable"]
    tw = [r["twspd"] for r in w if r.get("twspd") is not None]
    frac = len(stable) / max(1, len([r for r in w if r.get("stability")]))
    mean_tw = sum(tw) / len(tw) if tw else None
    if mean_tw is None:
        settling = None
    elif frac >= 0.6 and mean_tw < 10:
        settling = "High"
    elif frac >= 0.3 or mean_tw < 15:
        settling = "Moderate"
    else:
        settling = "Low"
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "min_temp": _extreme(w, "temp", min), "max_rh": _extreme(w, "rh"),
        "wind_range": [min((r["wspd"] for r in w if r.get("wspd") is not None), default=None),
                       max((r["wspd"] for r in w if r.get("wspd") is not None), default=None)],
        "prevailing_dir": compass(_vector_mean_dir(w)),
        "stable_fraction": round(frac, 2),
        "min_mixing_height": _extreme([r for r in w if r.get("pbl") is not None], "pbl", min),
        "mean_transport_wind": None if mean_tw is None else round(mean_tw, 1),
        "smoke_settling": settling,
    }


def fwi_noon(recs, days, tz):
    """Model FWI2025 values at 1200 local standard time for each day."""
    std = tz.utcoffset(datetime(days[0].year, 1, 15)) if hasattr(tz, "utcoffset") else timedelta(0)
    out = []
    for d in days:
        noon_utc = datetime.combine(d, time(12), timezone.utc) - std
        r = next((x for x in recs if x["utc"] == noon_utc), None)
        if not r:
            out.append({"date": d.isoformat(), "available": False})
            continue
        r24 = next((x for x in recs if x["utc"] == noon_utc - timedelta(hours=24)), None)
        rain24 = None if r.get("precip") is None else round(r["precip"] - (r24["precip"] if r24 else 0.0), 1)
        out.append({
            "date": d.isoformat(), "available": True, "local": r["local"],
            "rain24_partial": r24 is None,
            **{k: r.get(k) for k in ("temp", "rh", "wspd", "wdir", "ffmc", "dmc", "dc", "isi", "bui", "fwi")},
            "rain24": rain24,
        })
    return out


def triggers(recs, start, end, th, shifts):
    w = _window(recs, start, end)
    hours = lambda cond: [r["local"] for r in w if cond(r)]
    return [
        {"id": "a", "label": f"Wind shift > {th['shift_deg']:.0f}°",
         "hits": [s["start"] for s in shifts]},
        {"id": "a", "label": f"Sustained wind > {th['wind']:.0f} km/h",
         "hits": hours(lambda r: (r.get("wspd") or 0) > th["wind"])},
        {"id": "a", "label": f"Gusts > {th['gust']:.0f} km/h",
         "hits": hours(lambda r: (r.get("gust") or 0) > th["gust"])},
        {"id": "b", "label": f"RH < {th['rh_min']:.0f}%",
         "hits": hours(lambda r: r.get("rh") is not None and r["rh"] < th["rh_min"])},
        {"id": "c", "label": f"Convective potential (CAPE > {th['cape']:.0f} J/kg at the burn location; "
                             "lightning distance not available)",
         "hits": hours(lambda r: (r.get("cape") or 0) > th["cape"])},
        {"id": "d", "label": "Frontal passage signature (wind shift with ≥3 °C drop or ≥15% RH rise)",
         "hits": [s["start"] for s in shifts if s["frontal"]]},
    ]


def prescription_days(recs, day, tz, noon, aq, n=3):
    """Per day: hourly records 06–21 local, noon-LST daily codes and the day's max forecast AQHI."""
    aq_by_day = {}
    for f in (aq or {}).get("forecast", []):
        d = datetime.fromisoformat(f["time"].replace("Z", "+00:00")).astimezone(tz).date().isoformat()
        aq_by_day[d] = max(aq_by_day.get(d, f["aqhi"]), f["aqhi"])
    out = []
    for k in range(n):
        d = day + timedelta(days=k)
        hours = _window(recs, datetime.combine(d, time(6), tz), datetime.combine(d, time(21), tz))
        out.append({
            "date": d.isoformat(),
            "hours": [_public(r) for r in hours],
            "noon": next((x for x in noon if x["date"] == d.isoformat()), None),
            "aqhi_max": aq_by_day.get(d.isoformat()),
        })
    return out


def build(store, lat, lon, run=None, product="morning", burn_date=None, thresholds=None, name=None):
    th = {**DEFAULT_THRESHOLDS, **{k: v for k, v in (thresholds or {}).items() if v is not None}}
    summ = {r["run"]: r for r in store.summary()}
    if run is None:
        run = next((r for r, s in sorted(summ.items(), reverse=True) if len(s["hours"]) >= 49), None)
    if run not in summ:
        raise LookupError("no model run available")
    tz = local_zone(lat, lon)
    recs, cell_lat, cell_lon = point_series(run, lat, lon)
    _decorate(recs, tz)

    now_local = datetime.now(timezone.utc).astimezone(tz)
    if burn_date:
        day = date.fromisoformat(burn_date)
    else:
        day = now_local.date() if now_local.hour < 18 else now_local.date() + timedelta(days=1)
    spec = PRODUCTS.get(product, PRODUCTS["morning"])

    if spec["times"] is None:  # spot: next 6 hours from now
        base = now_local.replace(minute=0, second=0, microsecond=0)
        key_times = [base + timedelta(hours=k) for k in range(1, 7)]
        day = base.date()
    else:
        key_times = [datetime.combine(day, time(h), tz) for h in spec["times"]]
    columns = []
    for t in key_times:
        r = _at(recs, t)
        columns.append({"label": t.strftime("%H%M"), "target": t.isoformat(),
                        "values": _public(r) if r else None})

    burn_start = datetime.combine(day, time(8), tz)
    burn_end = datetime.combine(day, time(20), tz)
    if product in ("midday", "afternoon", "spot"):
        burn_start = max(burn_start, now_local.replace(minute=0, second=0, microsecond=0))
    period = summarize(recs, burn_start, burn_end, th["shift_deg"]) or {}
    shifts_ahead = wind_shifts(_window(recs, burn_start, burn_end + timedelta(hours=12)), th["shift_deg"])

    aq = externals.aqhi(lat, lon)
    noon = fwi_noon(recs, [day + timedelta(days=k) for k in range(3)], tz)
    report = {
        "meta": {
            "product": product, "title": spec["title"], "name": name,
            "lat": lat, "lon": lon, "grid_lat": round(cell_lat, 3), "grid_lon": round(cell_lon, 3),
            "run": run, "run_complete": summ[run]["complete"],
            "issued": now_local.isoformat(), "timezone": str(tz),
            "tz_abbr": now_local.tzname(), "burn_date": day.isoformat(), "thresholds": th,
        },
        "observation": externals.latest_obs(lat, lon),
        "columns": columns,
        "burn_period": period,
        "inversion": inversion_breakup(recs, day, tz),
        "wind_shifts": shifts_ahead,
        "aqhi": aq,
        "alerts": externals.alerts(lat, lon),
        "triggers": triggers(recs, burn_start, burn_end, th, period.get("wind_shifts", [])),
        "fwi": {"model": noon, "stations": externals.cwfis_stations_near(lat, lon)},
        "hourly": [_public(r) for r in _window(recs, datetime.combine(day, time(6), tz),
                                               datetime.combine(day, time(21), tz))],
        # inputs for the prescription (GO/NO-GO) check, done in the browser so limits can be edited live
        "days": prescription_days(recs, day, tz, noon, aq),
        "methods": METHODS,
    }
    if product == "morning":
        report["outlook"] = [
            {"date": (day + timedelta(days=k)).isoformat(),
             **(summarize(recs, datetime.combine(day + timedelta(days=k), time(8), tz),
                          datetime.combine(day + timedelta(days=k), time(20), tz), th["shift_deg"]) or {"missing": True})}
            for k in (1, 2)]
    if product == "afternoon":
        report["overnight"] = overnight(recs, day, tz)
        nxt = day + timedelta(days=1)
        report["next_day"] = {"date": nxt.isoformat(), **(summarize(
            recs, datetime.combine(nxt, time(8), tz), datetime.combine(nxt, time(20), tz), th["shift_deg"]) or {})}
    return report
