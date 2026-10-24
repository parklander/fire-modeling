"""Canadian Forest Fire Behaviour Prediction (FBP) System, vectorised with numpy.

Port of `fire_behaviour_prediction()` and its helpers from the cffdrs R package
(https://github.com/cffdrs/cffdrs_r, v1.10.0, commit 4d20a30): FCFDG (1992) with the
Wotton et al. (2009) updates. Every function mirrors its R counterpart; R's `ifelse`
becomes `np.where` (both branches evaluated, hence the errstate guards). Validate with
`python -m fireweather.validate_fbp <cffdrs_r checkout>`.

Inputs follow cffdrs conventions: FFMC/BUI/ISI as FWI1987 values, wind speed km/h,
wind direction (WD) and aspect in degrees, slope (GS) in percent, LONG positive west or
negative (both accepted), HR in hours since ignition.
"""
import numpy as np

FFMC_COEFFICIENT = 250.0 * 59.5 / 101.0
FUELS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "D1", "M1", "M2", "M3", "M4",
         "S1", "S2", "S3", "O1A", "O1B")
_A = dict(zip(FUELS, (90, 110, 110, 110, 30, 30, 45, 30, 0, 0, 120, 100, 75, 40, 55, 190, 250)))
_B = dict(zip(FUELS, (0.0649, 0.0282, 0.0444, 0.0293, 0.0697, 0.0800, 0.0305, 0.0232, 0, 0, 0.0572,
                      0.0404, 0.0297, 0.0438, 0.0829, 0.0310, 0.0350)))
_C0 = dict(zip(FUELS, (4.5, 1.5, 3.0, 1.5, 4.0, 3.0, 2.0, 1.6, 0, 0, 1.4, 1.48, 1.3, 1.7, 3.2, 1.4, 1.7)))
_CBH = dict(zip(FUELS, (2, 3, 8, 4, 18, 7, 10, 0, 6, 6, 6, 6, 0, 0, 0, 0, 0)))
_CFL = dict(zip(FUELS, (0.75, 0.8, 1.15, 1.2, 1.2, 1.8, 0.5, 0, 0.8, 0.8, 0.8, 0.8, 0, 0, 0, 0, 0)))
_BUIO = dict(zip(FUELS, (72, 64, 62, 66, 56, 62, 106, 32, 50, 50, 50, 50, 38, 63, 31, 1, 1)))
_Q = dict(zip(FUELS, (0.9, 0.7, 0.75, 0.8, 0.8, 0.8, 0.85, 0.9, 0.8, 0.8, 0.8, 0.8, 0.75, 0.75, 0.75, 1.0, 1.0)))
BASIC = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "D1", "S1", "S2", "S3")
NO_CROWN = ("D1", "S1", "S2", "S3", "O1A", "O1B")
ALPHA_FIXED = ("C1", "O1A", "O1B", "S1", "S2", "S3", "D1")


def _tab(table, fuel):
    """Per-element lookup of a fuel-type table (NaN for NF/WA/unknown)."""
    u, inv = np.unique(fuel, return_inverse=True)
    return np.array([table.get(x, np.nan) for x in u], dtype=float)[inv].reshape(fuel.shape)


def _is(fuel, *types):
    return np.isin(fuel, types)


def normalize_fuel(fuel):
    f = np.char.upper(np.char.replace(np.char.replace(np.asarray(fuel, dtype=str), "-", ""), " ", ""))
    return f


# ---- component functions ----------------------------------------------------------
def initial_spread_index(ffmc, ws, fbp_mod=False):
    fm = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    fw = np.where((ws >= 40) & fbp_mod, 12 * (1 - np.exp(-0.0818 * (ws - 28))), np.exp(0.05039 * ws))
    ff = 91.9 * np.exp(-0.1386 * fm) * (1 + (fm ** 5.31) / 49300000)
    return 0.208 * fw * ff


def buildup_effect(fuel, bui):
    buio, q = _tab(_BUIO, fuel), _tab(_Q, fuel)
    with np.errstate(divide="ignore", invalid="ignore"):
        be = np.exp(50 * np.log(q) * (1 / bui - 1 / buio))
    return np.where((bui > 0) & (buio > 0), be, 1.0)


def crown_base_height(fuel, cbh, sd, sh):
    default = np.where(_is(fuel, "C6") & (sd > 0) & (sh > 0), -11.2 + 1.06 * sh + 0.0017 * sd, _tab(_CBH, fuel))
    cbh = np.where((cbh <= 0) | (cbh > 50) | np.isnan(cbh), default, cbh)
    return np.where(cbh < 0, 1e-07, cbh)


def crown_fuel_load(fuel, cfl):
    return np.where((cfl <= 0) | (cfl > 2) | np.isnan(cfl), _tab(_CFL, fuel), cfl)


def foliar_moisture_content_minimum(lat, long, elv, dj, d0):
    unset = (d0 <= 0) | np.isnan(d0)
    latn = np.where(unset, np.where(elv <= 0, 46 + 23.4 * np.exp(-0.0360 * (150 - long)),
                                    43 + 33.7 * np.exp(-0.0351 * (150 - long))), 0)
    d0 = np.where(unset, np.where(elv <= 0, 151 * (lat / latn), 142.1 * (lat / latn) + 0.0172 * elv), d0)
    return np.round(d0)  # numpy rounds half to even, as R's round() does


def foliar_moisture_content(lat, long, elv, dj, d0):
    nd = np.abs(dj - d0)
    return np.where(nd < 30, 85 + 0.0189 * nd ** 2,
                    np.where((nd >= 30) & (nd < 50), 32.9 + 3.17 * nd - 0.0288 * nd ** 2, 120))


def surface_fuel_consumption(fuel, ffmc, bui, pc, gfl):
    with np.errstate(invalid="ignore"):
        c1 = np.where(ffmc > 84, 0.75 + 0.75 * (1 - np.exp(-0.23 * (ffmc - 84))) ** 0.5,
                      0.75 - 0.75 * (1 - np.exp(-0.23 * (84 - ffmc))) ** 0.5)
    sfc = np.full(ffmc.shape, -999.0)
    sfc = np.where(_is(fuel, "C1"), c1, sfc)
    sfc = np.where(_is(fuel, "C2", "M3", "M4"), 5.0 * (1 - np.exp(-0.0115 * bui)), sfc)
    sfc = np.where(_is(fuel, "C3", "C4"), 5.0 * (1 - np.exp(-0.0164 * bui)) ** 2.24, sfc)
    sfc = np.where(_is(fuel, "C5", "C6"), 5.0 * (1 - np.exp(-0.0149 * bui)) ** 2.48, sfc)
    sfc = np.where(_is(fuel, "C7"), np.where(ffmc > 70, 2 * (1 - np.exp(-0.104 * (ffmc - 70))), 0)
                   + 1.5 * (1 - np.exp(-0.0201 * bui)), sfc)
    sfc = np.where(_is(fuel, "D1"), 1.5 * (1 - np.exp(-0.0183 * bui)), sfc)
    sfc = np.where(_is(fuel, "M1", "M2"), pc / 100 * (5.0 * (1 - np.exp(-0.0115 * bui)))
                   + (100 - pc) / 100 * (1.5 * (1 - np.exp(-0.0183 * bui))), sfc)
    sfc = np.where(_is(fuel, "O1A", "O1B"), gfl, sfc)
    sfc = np.where(_is(fuel, "S1"), 4.0 * (1 - np.exp(-0.025 * bui)) + 4.0 * (1 - np.exp(-0.034 * bui)), sfc)
    sfc = np.where(_is(fuel, "S2"), 10.0 * (1 - np.exp(-0.013 * bui)) + 6.0 * (1 - np.exp(-0.060 * bui)), sfc)
    sfc = np.where(_is(fuel, "S3"), 12.0 * (1 - np.exp(-0.0166 * bui)) + 20.0 * (1 - np.exp(-0.0210 * bui)), sfc)
    return np.where(sfc <= 0, 0.000001, sfc)


def critical_surface_intensity(fmc, cbh):
    return 0.001 * cbh ** 1.5 * (460 + 25.9 * fmc) ** 1.5


def surface_fire_rate_of_spread(csi, sfc):
    return csi / (300 * sfc)


def crown_fraction_burned(ros, rso):
    return np.where(ros > rso, 1 - np.exp(-0.23 * (ros - rso)), 0.0)


def _curing_factor(cc):
    return np.where(cc < 58.8, 0.005 * (np.exp(0.061 * cc) - 1), 0.176 + 0.02 * (cc - 58.8))


def _const(name, fuel_like):
    return np.full(fuel_like.shape, name, dtype="<U3")


def rate_of_spread_extended(fuel, isi, bui, fmc, sfc, pc, pdf, cc, cbh):
    nobui = np.full(isi.shape, -1.0)
    a, b, c0 = _tab(_A, fuel), _tab(_B, fuel), _tab(_C0, fuel)
    ros_of = lambda ft: rate_of_spread(_const(ft, fuel), isi, nobui, fmc, sfc, pc, pdf, cc, cbh)
    with np.errstate(invalid="ignore", over="ignore"):
        rsi = np.full(isi.shape, -1.0)
        rsi = np.where(_is(fuel, "C1", "C2", "C3", "C4", "C5", "C7", "D1", "S1", "S2", "S3"),
                       a * (1 - np.exp(-b * isi)) ** c0, rsi)
        mixed = _is(fuel, "M1", "M2", "M3", "M4")
        if mixed.any():
            c2, d1 = ros_of("C2"), ros_of("D1")
            rsi = np.where(_is(fuel, "M1"), pc / 100 * c2 + (100 - pc) / 100 * d1, rsi)
            rsi = np.where(_is(fuel, "M2"), pc / 100 * c2 + 0.2 * (100 - pc) / 100 * d1, rsi)
            rsi_m3 = _A["M3"] * (1 - np.exp(-_B["M3"] * isi)) ** _C0["M3"]
            rsi = np.where(_is(fuel, "M3"), pdf / 100 * rsi_m3 + (1 - pdf / 100) * d1, rsi)
            rsi_m4 = _A["M4"] * (1 - np.exp(-_B["M4"] * isi)) ** _C0["M4"]
            rsi = np.where(_is(fuel, "M4"), pdf / 100 * rsi_m4 + 0.2 * (1 - pdf / 100) * d1, rsi)
        cf = _curing_factor(cc)
        rsi = np.where(_is(fuel, "O1A", "O1B"), a * (1 - np.exp(-b * isi)) ** c0 * cf, rsi)
        csi = critical_surface_intensity(fmc, cbh)
        rso = surface_fire_rate_of_spread(csi, sfc)
        c6 = _is(fuel, "C6")
        rsi = np.where(c6, 30 * (1 - np.exp(-0.08 * isi)) ** 3.0, rsi)
        fme = ((1.5 - 0.00275 * fmc) ** 4.0) / (460 + 25.9 * fmc) * 1000
        rsc = np.where(c6, 60 * (1 - np.exp(-0.0497 * isi)) * fme / 0.778, np.nan)
        rss = buildup_effect(fuel, bui) * rsi  # same expression for C6 (Eq. 63) and others
        cfb = np.where(c6, np.where((rsc > rss) & (rss > rso), crown_fraction_burned(rss, rso), 0.0),
                       crown_fraction_burned(rss, rso))
        ros = np.where(c6, np.where(rsc > rss, rss + cfb * (rsc - rss), rss), rss)
    ros = np.where(ros <= 0, 0.000001, ros)
    return {"ROS": ros, "CFB": cfb, "CSI": csi, "RSO": rso}


def rate_of_spread(fuel, isi, bui, fmc, sfc, pc, pdf, cc, cbh):
    return rate_of_spread_extended(fuel, isi, bui, fmc, sfc, pc, pdf, cc, cbh)["ROS"]


def _slope_isf(rsf, a, b, c0):
    with np.errstate(invalid="ignore", divide="ignore"):
        x = 1 - (rsf / a) ** (1 / c0)
        return np.where(x >= 0.01, np.log(x) / (-b), np.log(0.01) / (-b))


def slope_adjustment(fuel, ffmc, bui, ws, waz, gs, saz, fmc, sfc, pc, pdf, cc, cbh, isi):
    nobui = np.full(ffmc.shape, -1.0)
    sf = np.where(gs >= 70, 10, np.exp(3.533 * (gs / 100) ** 1.2))
    isz = initial_spread_index(ffmc, 0)
    ros_of = lambda ft, pdf_=pdf: rate_of_spread(_const(ft, fuel), isz, nobui, fmc, sfc, pc, pdf_, cc, cbh)
    a, b, c0 = _tab(_A, fuel), _tab(_B, fuel), _tab(_C0, fuel)
    isf = np.full(ffmc.shape, -99.0)
    rsf = rate_of_spread(fuel, isz, nobui, fmc, sfc, pc, pdf, cc, cbh) * sf
    isf = np.where(_is(fuel, *BASIC), _slope_isf(rsf, a, b, c0), isf)
    isf_of = lambda ft, rs: _slope_isf(rs, _A[ft], _B[ft], _C0[ft])
    if _is(fuel, "M1", "M2").any():
        isf_c2, isf_d1 = isf_of("C2", ros_of("C2") * sf), isf_of("D1", ros_of("D1") * sf)
        isf = np.where(_is(fuel, "M1", "M2"), pc / 100 * isf_c2 + (1 - pc / 100) * isf_d1, isf)
    pdf100 = np.full(ffmc.shape, 100.0)
    for ft in ("M3", "M4"):
        if _is(fuel, ft).any():
            isf_m, isf_d1 = isf_of(ft, ros_of(ft, pdf100) * sf), isf_of("D1", ros_of("D1", pdf100) * sf)
            isf = np.where(_is(fuel, ft), pdf / 100 * isf_m + (1 - pdf / 100) * isf_d1, isf)
    cf = _curing_factor(cc)
    isf = np.where(_is(fuel, "O1A", "O1B"), _slope_isf(rsf, cf * a, b, c0), isf)
    m = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    ff = 91.9 * np.exp(-0.1386 * m) * (1 + (m ** 5.31) / 4.93e7)
    with np.errstate(invalid="ignore", divide="ignore"):
        wse = 1 / 0.05039 * np.log(isf / (0.208 * ff))
        wse = np.where((wse > 40) & (isf < 0.999 * 2.496 * ff), 28 - (1 / 0.0818 * np.log(1 - isf / (2.496 * ff))), wse)
        wse = np.where((wse > 40) & (isf >= 0.999 * 2.496 * ff), 112.45, wse)
        wsx = ws * np.sin(waz) + wse * np.sin(saz)
        wsy = ws * np.cos(waz) + wse * np.cos(saz)
        wsv = np.sqrt(wsx * wsx + wsy * wsy)
        wsv = np.where(_is(fuel, "NF", "WA"), np.nan, wsv)
        raz = np.arccos(wsy / wsv)
    raz = np.where(wsx < 0, 2 * np.pi - raz, raz)
    raz = np.where(_is(fuel, "NF", "WA"), np.nan, raz)
    return wsv, raz


def length_to_breadth(fuel, wsv):
    with np.errstate(invalid="ignore"):
        grass = np.where(wsv >= 1.0, 1.1 * wsv ** 0.464, 1.0)
        return np.where(_is(fuel, "O1A", "O1B"), grass, 1.0 + 8.729 * (1 - np.exp(-0.030 * wsv)) ** 2.155)


def _alpha(fuel, cfb):
    return np.where(_is(fuel, *ALPHA_FIXED), 0.115, 0.115 - 18.8 * cfb ** 2.5 * np.exp(-8 * cfb))


def length_to_breadth_at_time(fuel, lb, hr, cfb):
    return (lb - 1) * (1 - np.exp(-_alpha(fuel, cfb) * hr)) + 1


def rate_of_spread_at_time(fuel, ros, hr, cfb):
    return ros * (1 - np.exp(-_alpha(fuel, cfb) * hr))


def distance_at_time(fuel, ros, hr, cfb):
    a = _alpha(fuel, cfb)
    return ros * (hr + np.exp(-a * hr) / a - 1 / a)


def back_rate_of_spread(fuel, ffmc, bui, wsv, fmc, sfc, pc, pdf, cc, cbh):
    m = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    ff = 91.9 * np.exp(-0.1386 * m) * (1.0 + (m ** 5.31) / 4.93e7)
    bisi = 0.208 * np.exp(-0.05039 * wsv) * ff
    return rate_of_spread(fuel, bisi, bui, fmc, sfc, pc, pdf, cc, cbh)


def flank_rate_of_spread(ros, bros, lb):
    return (ros + bros) / lb / 2


def total_fuel_consumption(fuel, cfl, cfb, sfc, pc, pdf, option="TFC"):
    cfc = cfl * cfb
    cfc = np.where(_is(fuel, "M1", "M2"), pc / 100 * cfc, np.where(_is(fuel, "M3", "M4"), pdf / 100 * cfc, cfc))
    return cfc if option == "CFC" else sfc + cfc


def fire_intensity(fc, ros):
    return 300 * fc * ros


# ---- main entry ---------------------------------------------------------------------
def _arr(v, n, default):
    if v is None:
        return np.full(n, float(default))
    a = np.asarray(v, dtype=float)
    return np.broadcast_to(a, (n,)).astype(float) if a.ndim == 0 or a.shape != (n,) else a.astype(float)


def fbp(fueltype, ffmc=None, bui=None, ws=None, wd=None, gs=None, aspect=None, lat=None, long=None, elv=None,
        dj=None, d0=None, hr=None, pc=None, pdf=None, cc=None, gfl=None, cbh=None, cfl=None, isi=None,
        accel=None, buieff=None, theta=None, fmc=None, sd=None, sh=None, secondary=True):
    """FBP for N cells/rows. All inputs broadcast to N. Returns dict of arrays (cffdrs "All" output)."""
    fuel = normalize_fuel(np.atleast_1d(fueltype))
    n = fuel.size
    fuel = fuel.reshape(n)
    A = lambda v, d: _arr(v, n, d)
    FFMC, BUI, WS, WD, GS = A(ffmc, 90), A(bui, 60), A(ws, 10), A(wd, 0), A(gs, 0)
    ASPECT, LAT, LONG, ELV = A(aspect, 0), A(lat, 55), A(long, -120), A(elv, 0)
    DJ, D0, HR, PC, PDF = A(dj, 180), A(d0, 0), A(hr, 1), A(pc, 50), A(pdf, 35)
    CC, GFL, CBH, CFL, ISI = A(cc, 80), A(gfl, 0.35), A(cbh, 0), A(cfl, 0), A(isi, 0)
    ACCEL, BUIEFF, THETA, FMC, SD, SH = A(accel, 0), A(buieff, 1), A(theta, 0), A(fmc, 0), A(sd, 0), A(sh, 0)

    # input cleaning, in the same order as cffdrs
    WD = WD * np.pi / 180
    THETA = THETA * np.pi / 180
    ASPECT = np.where(np.isnan(ASPECT), 0, ASPECT)
    ASPECT = np.where(ASPECT < 0, ASPECT + 360, ASPECT) * np.pi / 180
    ACCEL = np.where(np.isnan(ACCEL) | (ACCEL < 0), 0, ACCEL)
    ACCEL = np.where(~np.isin(ACCEL, (0, 1)), 1, ACCEL)
    DJ = np.where((DJ < 0) | (DJ > 366), 0, DJ)
    DJ = np.where(np.isnan(DJ), 180, DJ)
    D0 = np.where(np.isnan(D0) | (D0 < 0) | (D0 > 366), 0, D0)
    ELV = np.where((ELV < 0) | (ELV > 10000) | np.isnan(ELV), 0, ELV)
    BUIEFF = np.where(np.isnan(BUIEFF), 1, np.where(BUIEFF <= 0, 0, 1))
    HR = np.where(HR < 0, -HR, HR)
    HR = np.where(HR > 366 * 24, 24, HR)
    HR = np.where(np.isnan(HR), 0, HR)
    FFMC = np.where((FFMC < 0) | (FFMC > 101), 0, FFMC)
    FFMC = np.where(np.isnan(FFMC), 90, FFMC)
    ISI = np.where(np.isnan(ISI) | (ISI < 0) | (ISI > 300), 0, ISI)
    BUI = np.where((BUI < 0) | (BUI > 1000), 0, BUI)
    BUI = np.where(np.isnan(BUI), 60, BUI)
    WS = np.where((WS < 0) | (WS > 300), 0, WS)
    WS = np.where(np.isnan(WS), 10, WS)
    WD = np.where(np.isnan(WD) | (WD < -2 * np.pi) | (WD > 2 * np.pi), 0, WD)
    GS = np.where(np.isnan(GS) | (GS < 0) | (GS > 200), 0, GS)
    GS = np.where((ASPECT < -2 * np.pi) | (ASPECT > 2 * np.pi), 0, GS)
    PC = np.where(np.isnan(PC) | (PC < 0) | (PC > 100), 50, PC)
    PDF = np.where(np.isnan(PDF) | (PDF < 0) | (PDF > 100), 35, PDF)
    CC = np.where((CC <= 0) | (CC > 100), 95, CC)
    CC = np.where(np.isnan(CC), 80, CC)
    GFL = np.where(np.isnan(GFL) | (GFL <= 0) | (GFL > 100), 0.35, GFL)
    LAT = np.where((LAT < -90) | (LAT > 90), 0, LAT)
    LAT = np.where(np.isnan(LAT), 55, LAT)
    LONG = np.where((LONG < -180) | (LONG > 360), 0, LONG)
    LONG = np.where(LONG > 180, LONG - 180, LONG)
    LONG = np.where(np.isnan(LONG), -120, LONG)
    LONG = np.where(LONG < 0, -LONG, LONG)
    THETA = np.where(np.isnan(THETA) | (THETA < -2 * np.pi) | (THETA > 2 * np.pi), 0, THETA)
    SD = np.where(np.isnan(SD), 0, np.where((SD < 0) | (SD > 1e5), -999, SD))
    SH = np.where(np.isnan(SH), 0, np.where((SH < 0) | (SH > 100), -999, SH))

    HR = HR * 60  # minutes
    WAZ = WD + np.pi
    WAZ = np.where(WAZ > 2 * np.pi, WAZ - 2 * np.pi, WAZ)
    SAZ = ASPECT + np.pi
    SAZ = np.where(SAZ > 2 * np.pi, SAZ - 2 * np.pi, SAZ)

    CBH = crown_base_height(fuel, CBH, SD, SH)
    CFL = crown_fuel_load(fuel, CFL)
    with np.errstate(invalid="ignore", divide="ignore"):
        D0 = np.where(D0 <= 0, foliar_moisture_content_minimum(LAT, LONG, ELV, DJ, D0), D0)
        FMC = np.where((FMC <= 0) | (FMC > 120) | np.isnan(FMC), foliar_moisture_content(LAT, LONG, ELV, DJ, D0), FMC)
    FMC = np.where(_is(fuel, *NO_CROWN), 0, FMC)
    D0 = np.where(_is(fuel, *NO_CROWN), 0, D0)

    SFC = surface_fuel_consumption(fuel, FFMC, BUI, PC, GFL)
    BUI = np.where(BUIEFF != 1, 0, BUI)
    WSV0, RAZ0 = slope_adjustment(fuel, FFMC, BUI, WS, WAZ, GS, SAZ, FMC, SFC, PC, PDF, CC, CBH, ISI)
    WSV = np.where((GS > 0) & (FFMC > 0), WSV0, WS)
    RAZ = np.where((GS > 0) & (FFMC > 0), RAZ0, WAZ)
    ISI = np.where(ISI > 0, ISI, initial_spread_index(FFMC, WSV, True))
    rv = rate_of_spread_extended(fuel, ISI, BUI, FMC, SFC, PC, PDF, CC, CBH)
    ROS, CSI, RSO = rv["ROS"], rv["CSI"], rv["RSO"]
    CFB = np.where(CFL > 0, rv["CFB"], 0.0)
    TFC = total_fuel_consumption(fuel, CFL, CFB, SFC, PC, PDF)
    HFI = fire_intensity(TFC, ROS)
    RAZ = RAZ * 180 / np.pi
    RAZ = np.where(RAZ == 360, 0, RAZ)
    FD = np.where(CFB < 0.1, "S", np.where(CFB >= 0.9, "C", "I"))
    CFC = total_fuel_consumption(fuel, CFL, CFB, SFC, PC, PDF, option="CFC")
    out = {"CFB": CFB, "CFC": CFC, "FD": FD, "HFI": HFI, "RAZ": RAZ, "ROS": ROS, "SFC": SFC, "TFC": TFC}

    if secondary:
        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
            SF = np.where(GS >= 70, 10, np.exp(3.533 * (GS / 100) ** 1.2))
            BE = buildup_effect(fuel, BUI)
            LB = length_to_breadth(fuel, WSV)
            LBt = np.where(ACCEL == 0, LB, length_to_breadth_at_time(fuel, LB, HR, CFB))
            BROS = back_rate_of_spread(fuel, FFMC, BUI, WSV, FMC, SFC, PC, PDF, CC, CBH)
            FROS = flank_rate_of_spread(ROS, BROS, LB)
            E = np.sqrt(1 - 1 / LB / LB)
            # NB: cffdrs has already converted RAZ to degrees here while THETA is in radians, so
            # TROS/TROSt mix units. Kept as-is to match cffdrs; the growth model doesn't use TROS.
            TROS = ROS * (1 - E) / (1 - E * np.cos(THETA - RAZ))
            ROSt = np.where(ACCEL == 0, ROS, rate_of_spread_at_time(fuel, ROS, HR, CFB))
            BROSt = np.where(ACCEL == 0, BROS, rate_of_spread_at_time(fuel, BROS, HR, CFB))
            FROSt = np.where(ACCEL == 0, FROS, flank_rate_of_spread(ROSt, BROSt, LBt))
            Et = np.sqrt(1 - 1 / LBt / LBt)
            TROSt = np.where(ACCEL == 0, TROS, ROSt * (1 - Et) / (1 - Et * np.cos(THETA - RAZ)))
            crown = lambda r: np.where(CFL == 0, 0.0, np.where(_is(fuel, "C6"), 0.0, crown_fraction_burned(r, RSO)))
            FCFB, BCFB, TCFB = crown(FROS), crown(BROS), crown(TROS)
            FTFC = total_fuel_consumption(fuel, CFL, FCFB, SFC, PC, PDF)
            BTFC = total_fuel_consumption(fuel, CFL, BCFB, SFC, PC, PDF)
            TTFC = total_fuel_consumption(fuel, CFL, TCFB, SFC, PC, PDF)
            FFI, BFI, TFI = fire_intensity(FTFC, FROS), fire_intensity(BTFC, BROS), fire_intensity(TTFC, TROS)
            HROSt = np.where(HR < 0, -ROSt, ROSt)

            def t_init(cfb, r):
                a = 0.115 - 18.8 * cfb ** 2.5 * np.exp(-8 * cfb)
                x = 1 - RSO / r
                return np.log(np.where(x > 0, x, 1)) / (-a)
            TI, FTI, BTI, TTI = t_init(CFB, ROS), t_init(FCFB, FROS), t_init(BCFB, BROS), t_init(TCFB, TROS)
            DH = np.where(ACCEL == 1, distance_at_time(fuel, ROS, HR, CFB), ROS * HR)
            DB = np.where(ACCEL == 1, distance_at_time(fuel, BROS, HR, CFB), BROS * HR)
            DF = np.where(ACCEL == 1, (DH + DB) / (LBt * 2), (DH + DB) / (LB * 2))
        out.update({"BE": BE, "SF": SF, "ISI": ISI, "FFMC": FFMC, "FMC": FMC, "D0": D0, "RSO": RSO, "CSI": CSI,
                    "FROS": FROS, "BROS": BROS, "HROSt": HROSt, "FROSt": FROSt, "BROSt": BROSt, "FCFB": FCFB,
                    "BCFB": BCFB, "FFI": FFI, "BFI": BFI, "FTFC": FTFC, "BTFC": BTFC, "TI": TI, "FTI": FTI,
                    "BTI": BTI, "LB": LB, "LBt": LBt, "WSV": WSV, "DH": DH, "DB": DB, "DF": DF, "TROS": TROS,
                    "TROSt": TROSt, "TCFB": TCFB, "TFI": TFI, "TTFC": TTFC, "TTI": TTI})
    nf = _is(fuel, "WA", "NF")
    for k, v in out.items():
        if k == "FD":
            out[k] = np.where(nf, "NA", v)
        else:
            out[k] = np.where(nf, 0.0, v)
    return out
