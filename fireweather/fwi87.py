"""FWI1987 (Van Wagner 1987) moisture codes needed as FBP System inputs.

CFS guidance is that FWI2025 values should not drive FBP1992, so fire behaviour uses
these: hourly FFMC (Van Wagner 1977 / Van Wagner & Pickett 1985), daily FFMC, DMC, DC
and BUI. Ported from the cffdrs R package (v1.10.0) and checked against its fixtures by
`python -m fireweather.validate_fwi87 <cffdrs_r checkout>`.
"""
import numpy as np

FFMC_COEFFICIENT = 250.0 * 59.5 / 101.0
_ELL = {  # DMC effective day length by month
    "46N": (6.5, 7.5, 9, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8, 7, 6),
    "20N": (7.9, 8.4, 8.9, 9.5, 9.9, 10.2, 10.1, 9.7, 9.1, 8.6, 8.1, 7.8),
    "20S": (10.1, 9.6, 9.1, 8.5, 8.1, 7.8, 7.9, 8.3, 8.9, 9.4, 9.9, 10.2),
    "40S": (11.5, 10.5, 9.2, 7.9, 6.8, 6.2, 6.5, 7.4, 8.7, 10, 11.2, 11.8),
}
_FL = {  # DC day length factor by month
    "N": (-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5, 2.4, 0.4, -1.6, -1.6),
    "S": (6.4, 5, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.6, 0.9, 3.8, 5.8),
}
_np = lambda *a: [np.asarray(x, dtype=float) for x in a]


def hourly_ffmc(temp, rh, ws, prec, ffmc_old, t0=1.0):
    """Van Wagner (1977) hourly FFMC for a time step of t0 hours."""
    temp, rh, ws, prec, fo, t0 = _np(temp, rh, ws, prec, ffmc_old, t0)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        mo = FFMC_COEFFICIENT * (101 - fo) / (59.5 + fo)
        rf = prec
        wet = mo + 42.5 * rf * np.exp(-100 / (251 - mo)) * (1 - np.exp(-6.93 / rf))
        mr = np.where(mo <= 150, wet, wet + 0.0015 * ((mo - 150) ** 2) * (rf ** 0.5))
        mr = np.where(mr > 250, 250, mr)
        mo = np.where(prec > 0.0, mr, mo)
        ed = 0.942 * rh ** 0.679 + 11 * np.exp((rh - 100) / 10) + 0.18 * (21.1 - temp) * (1 - np.exp(-0.115 * rh))
        ko = 0.424 * (1 - (rh / 100) ** 1.7) + 0.0694 * ws ** 0.5 * (1 - (rh / 100) ** 8)
        kd = ko * 0.0579 * np.exp(0.0365 * temp)
        md = ed + (mo - ed) * 10 ** (-kd * t0)
        ew = 0.618 * rh ** 0.753 + 10 * np.exp((rh - 100) / 10) + 0.18 * (21.1 - temp) * (1 - np.exp(-0.115 * rh))
        k1 = 0.424 * (1 - ((100 - rh) / 100) ** 1.7) + 0.0694 * ws ** 0.5 * (1 - ((100 - rh) / 100) ** 8)
        kw = k1 * 0.0579 * np.exp(0.0365 * temp)
        mw = ew - (ew - mo) * 10 ** (-kw * t0)
        m = np.where(mo > ed, md, mw)
        m = np.where((ed >= mo) & (mo >= ew), mo, m)
        fo = 59.5 * (250 - m) / (FFMC_COEFFICIENT + m)
    return np.where(fo <= 0, 0, fo)


def daily_ffmc(ffmc_yda, temp, rh, ws, prec):
    fo, temp, rh, ws, prec = _np(ffmc_yda, temp, rh, ws, prec)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        wmo = FFMC_COEFFICIENT * (101 - fo) / (59.5 + fo)
        ra = np.where(prec > 0.5, prec - 0.5, prec)
        rain = wmo + 42.5 * ra * np.exp(-100 / (251 - wmo)) * (1 - np.exp(-6.93 / ra))
        wmo = np.where(prec > 0.5, np.where(wmo > 150, rain + 0.0015 * (wmo - 150) * (wmo - 150) * np.sqrt(ra), rain), wmo)
        wmo = np.where(wmo > 250, 250, wmo)
        ed = 0.942 * rh ** 0.679 + 11 * np.exp((rh - 100) / 10) + 0.18 * (21.1 - temp) * (1 - 1 / np.exp(rh * 0.115))
        ew = 0.618 * rh ** 0.753 + 10 * np.exp((rh - 100) / 10) + 0.18 * (21.1 - temp) * (1 - 1 / np.exp(rh * 0.115))
        wetting = (wmo < ed) & (wmo < ew)
        z = np.where(wetting, 0.424 * (1 - ((100 - rh) / 100) ** 1.7) + 0.0694 * np.sqrt(ws) * (1 - ((100 - rh) / 100) ** 8), 0)
        x = z * 0.581 * np.exp(0.0365 * temp)
        wm = np.where(wetting, ew - (ew - wmo) / 10 ** x, wmo)
        z = np.where(wmo > ed, 0.424 * (1 - (rh / 100) ** 1.7) + 0.0694 * np.sqrt(ws) * (1 - (rh / 100) ** 8), z)
        x = z * 0.581 * np.exp(0.0365 * temp)
        wm = np.where(wmo > ed, ed + (wmo - ed) / 10 ** x, wm)
        f = 59.5 * (250 - wm) / (FFMC_COEFFICIENT + wm)
    return np.clip(f, 0, 101)


def duff_moisture_code(dmc_yda, temp, rh, prec, lat, mon, lat_adjust=True):
    dmc_yda, temp, rh, prec, lat = _np(dmc_yda, temp, rh, prec, lat)
    mon = np.asarray(mon, dtype=int) - 1
    lat_adjust = np.broadcast_to(np.asarray(lat_adjust, dtype=bool), lat.shape)
    temp = np.where(temp < -1.1, -1.1, temp)
    base = 1.894 * (temp + 1.1) * (100 - rh) * 1e-04
    ell = lambda k: np.asarray(_ELL[k])[mon]
    rk = base * ell("46N")
    rk = np.where(lat_adjust & (lat <= 30) & (lat > 10), base * ell("20N"), rk)
    rk = np.where(lat_adjust & (lat <= -10) & (lat > -30), base * ell("20S"), rk)
    rk = np.where(lat_adjust & (lat <= -30) & (lat >= -90), base * ell("40S"), rk)
    rk = np.where(lat_adjust & (lat <= 10) & (lat > -10), base * 9, rk)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        rw = 0.92 * prec - 1.27
        wmi = 20 + 280 / np.exp(0.023 * dmc_yda)
        b = np.where(dmc_yda <= 33, 100 / (0.5 + 0.3 * dmc_yda),
                     np.where(dmc_yda <= 65, 14 - 1.3 * np.log(dmc_yda), 6.2 * np.log(dmc_yda) - 17.2))
        wmr = wmi + 1000 * rw / (48.77 + b * rw)
        pr = np.where(prec <= 1.5, dmc_yda, 43.43 * (5.6348 - np.log(wmr - 20)))
    pr = np.where(pr < 0, 0, pr)
    dmc = pr + rk
    return np.where(dmc < 0, 0, dmc)


def drought_code(dc_yda, temp, rh, prec, lat, mon, lat_adjust=True):
    dc_yda, temp, rh, prec, lat = _np(dc_yda, temp, rh, prec, lat)
    mon = np.asarray(mon, dtype=int) - 1
    lat_adjust = np.broadcast_to(np.asarray(lat_adjust, dtype=bool), lat.shape)
    temp = np.where(temp < -2.8, -2.8, temp)
    pe = (0.36 * (temp + 2.8) + np.asarray(_FL["N"])[mon]) / 2
    pe = np.where(lat_adjust & (lat <= -20), (0.36 * (temp + 2.8) + np.asarray(_FL["S"])[mon]) / 2, pe)
    pe = np.where(lat_adjust & (lat > -20) & (lat <= 20), (0.36 * (temp + 2.8) + 1.4) / 2, pe)
    pe = np.where(pe < 0, 0, pe)
    with np.errstate(invalid="ignore", divide="ignore"):
        rw = 0.83 * prec - 1.27
        smi = 800 * np.exp(-1 * dc_yda / 400)
        dr0 = dc_yda - 400 * np.log(1 + 3.937 * rw / smi)
    dr0 = np.where(dr0 < 0, 0, dr0)
    dr = np.where(prec <= 2.8, dc_yda, dr0)
    dc = dr + pe
    return np.where(dc < 0, 0, dc)


def initial_spread_index(ffmc, ws, fbp_mod=False):
    ffmc, ws = _np(ffmc, ws)
    fm = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    fw = np.where((ws >= 40) & np.asarray(fbp_mod, dtype=bool), 12 * (1 - np.exp(-0.0818 * (ws - 28))),
                  np.exp(0.05039 * ws))
    ff = 91.9 * np.exp(-0.1386 * fm) * (1 + fm ** 5.31 / 49300000)
    return 0.208 * fw * ff


def fire_weather_index(isi, bui):
    isi, bui = _np(isi, bui)
    bb = np.where(bui > 80, 0.1 * isi * (1000 / (25 + 108.64 / np.exp(0.023 * bui))),
                  0.1 * isi * (0.626 * bui ** 0.809 + 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        big = np.exp(2.72 * (0.434 * np.log(np.where(bb > 1, bb, np.e))) ** 0.647)
    return np.where(bb <= 1, bb, big)


def daily_severity_rating(fwi):
    return 0.0272 * np.asarray(fwi, dtype=float) ** 1.77


def buildup_index(dmc, dc):
    dmc, dc = _np(dmc, dc)
    with np.errstate(invalid="ignore", divide="ignore"):
        bui1 = np.where((dmc == 0) & (dc == 0), 0, 0.8 * dc * dmc / (dmc + 0.4 * dc))
        p = np.where(dmc == 0, 0, (dmc - bui1) / dmc)
        cc = 0.92 + (0.0114 * dmc) ** 1.7
        bui0 = dmc - cc * p
    bui0 = np.where(bui0 < 0, 0, bui0)
    return np.where(bui1 < dmc, bui0, bui1)
