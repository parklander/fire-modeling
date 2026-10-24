"""Vectorised port of the FWI2025 hourly equations from nrcan-cfs-fire/cffdrs-ng.

Source: https://github.com/nrcan-cfs-fire/cffdrs-ng  FWI/Python/NG_FWI.py and util.py
(version 2026-08-13, commit ed143d7, MPL-2.0). Each function mirrors its scalar
counterpart there, branch for branch, but operates on numpy arrays so a whole grid
is advanced one hour at a time. Validate with `python -m fireweather.validate_cffdrs`.
"""
import numpy as np

FFMC_DEFAULT = 85.0
DMC_DEFAULT = 6.0
DC_DEFAULT = 15.0
PREC_MIN_FFMC = 0.5
PREC_MIN_DMC = 1.5
PREC_MIN_DC = 2.8
DMC_REGRESSION = 2.22e-4
DC_REGRESSION = 1.5e-2
DEFAULT_GRASS_FUEL_LOAD = 0.35
TARGET_DRYING_SINCE_INTERCEPT = 5.0
C_FFMC = 14875 / 101

STATE_KEYS = ("mcffmc", "mcdmc", "mcdc", "mcgfmc_matted", "mcgfmc_standing",
              "rain_total_prev", "drying_since_intercept")


# ---- conversions -------------------------------------------------------------
def ffmc_to_mcffmc(ffmc):
    return C_FFMC * (101 - ffmc) / (59.5 + ffmc)


def mcffmc_to_ffmc(mc):
    return 59.5 * (250 - mc) / (C_FFMC + mc)


def dmc_to_mcdmc(dmc):
    return 280 / np.exp(dmc / 43.43) + 20


def mcdmc_to_dmc(mc):
    return 43.43 * np.log(280 / (mc - 20))


def dc_to_mcdc(dc):
    return 400 * np.exp(-dc / 400)


def mcdc_to_dc(mc):
    return 400 * np.log(400 / mc)


def initial_state(shape, ffmc=FFMC_DEFAULT, dmc=DMC_DEFAULT, dc=DC_DEFAULT):
    full = lambda v: np.broadcast_to(np.asarray(v, dtype=np.float64), shape).copy()
    mcgfmc = ffmc_to_mcffmc(FFMC_DEFAULT)
    return {
        "mcffmc": ffmc_to_mcffmc(full(ffmc)),
        "mcdmc": dmc_to_mcdmc(full(dmc)),
        "mcdc": dc_to_mcdc(full(dc)),
        "mcgfmc_matted": full(mcgfmc),
        "mcgfmc_standing": full(mcgfmc),
        "rain_total_prev": full(0.0),
        "drying_since_intercept": full(0.0),
    }


# ---- moisture codes ------------------------------------------------------------
def prec_effective(prec, prec_sum, threshold, subtract, rate):
    return np.where(np.round(prec + prec_sum, 1) <= threshold, 0.0,
                    np.where(np.round(prec_sum, 1) > threshold, prec * rate,
                             (prec_sum + prec) * rate - subtract))


def rain_since_intercept_reset(rain, s):
    reset = (np.round(rain, 1) > 0) | (s["rain_total_prev"] == 0)
    drying = np.where(reset, 0.0, s["drying_since_intercept"] + 1.0)
    done = ~reset & (drying >= TARGET_DRYING_SINCE_INTERCEPT)
    s["rain_total_prev"] = np.where(done, 0.0, s["rain_total_prev"])
    s["drying_since_intercept"] = np.where(done, 0.0, drying)


def fine_fuel_moisture(mc_0, temp, rh, ws, prec, prec_sum, dt=1.0):
    pf = prec_effective(prec, prec_sum, PREC_MIN_FFMC, PREC_MIN_FFMC, 1)
    wet = pf > 0
    pfs = np.where(wet, pf, 1.0)
    var0 = pfs * np.exp(-100 / (251 - mc_0)) * (1 - np.exp(-6.93 / pfs))
    mc_r = mc_0 + 42.5 * var0
    mc_r = np.where(mc_0 > 150, mc_r + 1.5e-3 * (mc_0 - 150) ** 2 * np.sqrt(pfs), mc_r)
    mc_r = np.where(wet, np.minimum(mc_r, 250.0), mc_0)
    e_1 = 0.18 * (21.1 - temp) * (1 - np.exp(-0.115 * rh))
    e_w = 0.618 * rh ** 0.753 + 10 * np.exp((rh - 100) / 10) + e_1
    e_d = 0.942 * rh ** 0.679 + 11 * np.exp((rh - 100) / 10) + e_1
    wetting = mc_r < e_w
    eta = np.where(wetting, (100 - rh) / 100, rh / 100)
    k_0 = 0.424 * (1 - eta ** 1.7) + 0.0694 * np.sqrt(ws) * (1 - eta ** 8)
    k = 0.1158 * np.exp(0.0365 * temp) * k_0
    e = np.where(wetting, e_w, e_d)
    moved = e + (mc_r - e) * 10 ** (-k * dt)
    return np.where(wetting | (mc_r > e_d), moved, mc_r)


def _daytime(hr, sunrise, sunset):
    return ((sunrise <= hr) & (hr <= sunset)) | ((hr < 6) & (sunrise <= hr + 24) & (hr + 24 <= sunset))


def duff_moisture(last_mcdmc, hr, temp, rh, prec, sunrise, sunset, prec_sum, dt=1.0):
    rw = prec_effective(prec, prec_sum, PREC_MIN_DMC, 1.27, 0.92)
    last_dmc = mcdmc_to_dmc(last_mcdmc)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.where(last_dmc <= 33, 100.0 / (0.3 * last_dmc + 0.5),
                     np.where(last_dmc <= 65, -1.3 * np.log(last_dmc) + 14.0, 6.2 * np.log(last_dmc) - 17.2))
    mr = np.minimum(last_mcdmc + (1e3 * rw) / (b * rw + 48.77), 300.0)
    t = np.where(np.round(temp, 1) < 0, 0.0, temp)
    invtau = DMC_REGRESSION * t * (100.0 - rh) / 43.43
    dried = (mr - 20.0) * np.exp(-dt * invtau) + 20.0
    return np.minimum(np.where(_daytime(hr, sunrise, sunset), dried, mr), 300.0)


def drought_code(last_mcdc, hr, temp, prec, sunrise, sunset, prec_sum, dt=1.0):
    rw = prec_effective(prec, prec_sum, PREC_MIN_DC, 1.27, 0.83)
    mr = np.minimum(last_mcdc + 3.937 * rw / 2.0, 400.0)
    pe = np.where(np.round(temp, 1) > 0, DC_REGRESSION * temp + 3.0 / 16.0, 0.0)
    dried = mr * np.exp(-dt * pe / 400.0)
    return np.minimum(np.where(_daytime(hr, sunrise, sunset), dried, mr), 400.0)


# ---- fire behaviour indices ------------------------------------------------------
def initial_spread_index(ws, ffmc):
    fm = ffmc_to_mcffmc(ffmc)
    fw = np.where(np.round(ws) >= 40, 12 * (1 - np.exp(-0.0818 * (ws - 28))), np.exp(0.05039 * ws))
    ff = 91.9 * np.exp(-0.1386 * fm) * (1.0 + fm ** 5.31 / 4.93e7)
    return 0.208 * fw * ff


def buildup_index(dmc, dc):
    with np.errstate(divide="ignore", invalid="ignore"):
        bui = np.where((dmc == 0) & (dc == 0), 0.0, 0.8 * dc * dmc / (dmc + 0.4 * dc))
        p = (dmc - bui) / dmc
        cc = 0.92 + (0.0114 * dmc) ** 1.7
        adj = np.maximum(dmc - cc * p, 0.0)
    return np.where(bui < dmc, adj, bui)


def fire_weather_index(isi, bui):
    bb = np.where(bui > 80, 0.1 * isi * 1000 / (25 + 108.64 / np.exp(0.023 * bui)),
                  0.1 * isi * (0.626 * bui ** 0.809 + 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        big = np.exp(2.72 * (0.434 * np.log(np.where(bb > 1, bb, np.e))) ** 0.647)
    return np.where(bb <= 1, bb, big)


def daily_severity_rating(fwi):
    return 0.0272 * fwi ** 1.77


# ---- grassland ---------------------------------------------------------------
def grass_fuel_moisture(lastmc, temp, rh, ws, rain, solrad, load, dt=1.0):
    rf, drf = 0.27, 0.389633
    mo = np.where(rain != 0.0, np.minimum(lastmc + rain / load * 100.0, 250.0), lastmc)
    tf = temp + 17.9 * solrad * np.exp(-0.034 * ws)
    rhf = np.where(tf > temp,
                   rh * 6.107 * 10.0 ** (7.5 * temp / (temp + 237.0)) / (6.107 * 10.0 ** (7.5 * tf / (tf + 237.0))),
                   rh)
    e1 = rf * (26.7 - tf) * (1.0 - 1.0 / np.exp(0.115 * rhf))
    ed = 1.62 * rhf ** 0.532 + 13.7 * np.exp((rhf - 100) / 13.0) + e1
    ew = 1.42 * rhf ** 0.512 + 12.0 * np.exp((rhf - 100) / 18.0) + e1
    moed, moew = mo - ed, mo - ew
    hold = (moed == 0) | ((moew >= 0) & (moed < 0))
    drying = moed > 0
    a1 = np.maximum(np.where(drying, rhf / 100.0, (100.0 - rhf) / 100.0), 0.0)
    e = np.where(drying, ed, ew)
    moe = np.where(drying, moed, moew)
    xkd = (0.424 * (1 - a1 ** 1.7) + 0.0694 * np.sqrt(ws) * (1 - a1 ** 8)) * drf * np.exp(0.0365 * tf)
    return np.where(hold, mo, e + moe * np.exp(-1.0 * np.log(10.0) * xkd * dt))


def curing_factor(cur):
    return np.where(cur >= 20.0, 1.036 / (1 + 103.989 * np.exp(-0.0996 * (cur - 20))), 0.0)


def mcgfmc_to_gfmc(mc, cur, wind):
    intercept, c_moist, c_wind = 1.49, -0.11, 0.075
    wind2m = 0.75 * wind
    probign = 1.0 / (1.0 + np.exp(-1.0 * (intercept + c_moist * mc + c_wind * wind2m)))
    new_p = curing_factor(cur) * probign
    with np.errstate(divide="ignore", invalid="ignore"):
        egmc = np.where(new_p > 0.0,
                        (np.log(new_p / (1.0 - new_p)) - intercept - c_wind * wind2m) / c_moist, 250.0)
    return mcffmc_to_ffmc(np.minimum(egmc, 250.0))


def _grass_fm(ws, mc):
    rws = np.round(ws)
    fm = np.where(mc < 12, np.exp(-0.108 * mc),
                  np.where((mc < 20.0) & (rws < 10.0), 0.6838 - 0.0342 * mc,
                           np.where((mc < 23.9) & (rws >= 10.0), 0.547 - 0.0228 * mc, 0.0)))
    return np.maximum(fm, 0.0)


def grass_spread_index(ws, mc, cur, standing):
    w5 = np.maximum(ws - 5.0, 0.0) ** 0.844
    fw_matted = 16.67 * np.where(ws < 5, 0.054 + 0.209 * ws, 1.1 + 0.715 * w5)
    fw_standing = 16.67 * np.where(ws < 5, 0.054 + 0.269 * ws, 1.4 + 0.838 * w5)
    fw = np.where(standing, fw_standing, fw_matted)
    return 1.11 * fw * _grass_fm(ws, mc) * curing_factor(cur)


def grass_fire_weather_index(gsi, load):
    fint = 300.0 * load * (gsi / 1.11)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(fint > 100, np.log(np.maximum(fint, 1e-9) / 60.0) / 0.14, fint / 25.0)


# ---- seasonal curing & sun (util.py) ------------------------------------------
_PERCENT_CURED = np.array([96.0, 95.0, 93.0, 92.0, 90.5, 88.4, 84.4, 78.1, 68.7, 50.3, 32.9, 23.0, 22.0,
                           21.0, 20.0, 25.7, 35.0, 43.0, 49.8, 60.0, 68.0, 72.0, 75.0, 78.9, 86.0, 96.0])


def seasonal_curing(days_since_greenup):
    """`days_since_greenup`: (date - most recent Mar 12).days, as in util.seasonal_curing."""
    days_in = np.asarray(days_since_greenup) + 1
    i = np.clip(days_in // 10, 0, len(_PERCENT_CURED) - 2)
    interp = _PERCENT_CURED[i] + (_PERCENT_CURED[i + 1] - _PERCENT_CURED[i]) * (days_in % 10) / 10.0
    return np.where(days_in < (len(_PERCENT_CURED) - 1) * 10, interp, _PERCENT_CURED[-1])


def sunlight(lat, lon, tz, doy, leap, hour, temp=None, rh=None):
    """Sunrise/sunset (local hours) and optionally solar radiation (kW/m^2)."""
    frac = 2.0 * np.pi * (doy - 1.0) / np.where(leap, 366.0, 365.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(frac) - 0.032077 * np.sin(frac)
                       - 0.014615 * np.cos(2.0 * frac) - 0.040849 * np.sin(2.0 * frac))
    decl = (0.006918 - 0.399912 * np.cos(frac) + 0.070257 * np.sin(frac)
            - 0.006758 * np.cos(frac * 2.0) + 0.000907 * np.sin(2.0 * frac)
            - 0.002697 * np.cos(3.0 * frac) + 0.00148 * np.sin(3.0 * frac))
    zenith = 90.833 * np.pi / 180.0
    rlat = lat * np.pi / 180.0
    timeoffset = eqtime + 4 * lon - 60 * tz
    x_tmp = np.clip(np.cos(zenith) / (np.cos(rlat) * np.cos(decl)) - np.tan(rlat) * np.tan(decl), -1, 1)
    halfday = 180.0 / np.pi * np.arccos(x_tmp)
    sunrise = (720.0 - 4.0 * (lon + halfday) - eqtime) / 60 + tz
    sunset = (720.0 - 4.0 * (lon - halfday) - eqtime) / 60 + tz
    if temp is None:
        return sunrise, sunset, None
    hourangle = (hour * 60.0 + timeoffset) / 4 - 180
    zen = np.minimum(np.pi / 2, np.arccos(np.sin(rlat) * np.sin(decl)
                                          + np.cos(rlat) * np.cos(decl) * np.cos(hourangle * np.pi / 180)))
    vpd = 6.11 * (1.0 - rh / 100.0) * np.exp(17.29 * temp / (temp + 237.3))
    solrad = np.cos(zen) * 0.92 * (1.0 - np.exp(-0.22 * vpd))
    return sunrise, sunset, np.where(solrad < 1e-4, 0.0, solrad)


# ---- one hourly step (NG_FWI._stnHFWI loop body) --------------------------------
def step(s, temp, rh, ws, prec, hr, sunrise, sunset, solrad, percent_cured, standing,
         load=DEFAULT_GRASS_FUEL_LOAD):
    """Advance state `s` (dict, modified in place) by one hour and return the indices."""
    rain_since_intercept_reset(prec, s)
    s["mcffmc"] = fine_fuel_moisture(s["mcffmc"], temp, rh, ws, prec, s["rain_total_prev"])
    s["mcdmc"] = duff_moisture(s["mcdmc"], hr, temp, rh, prec, sunrise, sunset, s["rain_total_prev"])
    s["mcdc"] = drought_code(s["mcdc"], hr, temp, prec, sunrise, sunset, s["rain_total_prev"])
    s["rain_total_prev"] = s["rain_total_prev"] + prec
    s["mcgfmc_matted"] = grass_fuel_moisture(s["mcgfmc_matted"], temp, rh, ws, prec, solrad, load)
    s["mcgfmc_standing"] = grass_fuel_moisture(s["mcgfmc_standing"], temp, rh, ws, prec * 0.06, 0.0, load)
    return indices(s, ws, percent_cured, standing, load)


def indices(s, ws, percent_cured, standing, load=DEFAULT_GRASS_FUEL_LOAD):
    """FWI system outputs from the current moisture state and wind."""
    ffmc = mcffmc_to_ffmc(s["mcffmc"])
    dmc = mcdmc_to_dmc(s["mcdmc"])
    dc = mcdc_to_dc(s["mcdc"])
    isi = initial_spread_index(ws, ffmc)
    bui = buildup_index(dmc, dc)
    fwi = fire_weather_index(isi, bui)
    mcgfmc = np.where(standing, s["mcgfmc_standing"], s["mcgfmc_matted"])
    gsi = grass_spread_index(ws, mcgfmc, percent_cured, standing)
    return {
        "ffmc": ffmc, "dmc": dmc, "dc": dc, "isi": isi, "bui": bui, "fwi": fwi,
        "dsr": daily_severity_rating(fwi),
        "gfmc": mcgfmc_to_gfmc(mcgfmc, percent_cured, ws), "gsi": gsi,
        "gfwi": grass_fire_weather_index(gsi, load),
    }
