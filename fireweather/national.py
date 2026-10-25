"""Weekly national fire weather summary for all areas of interest (CWFIS daily grids).

For each area (fireweather/aoi.json) and each day from today to the last date CWFIS has
published (about a week ahead), sample the CWFIS daily FWI1987 grids, flag conditions of
concern and give each area an overall status for the period:

  Concern  CWFIS fire danger Very High or Extreme, or FWI >= 30, on any day
  Watch    CWFIS fire danger High, FWI >= 20, ISI >= 10, BUI >= 60, DC >= 425, or wind >= 30 km/h
           with RH <= 30% (critical fire weather) on any day
  Normal   none of the above

Thresholds are the CWFIS map class boundaries (Very High classes for FWI/ISI/BUI). They're
module constants so they can be tuned to CFFM's needs.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, firewx

SUMMARY_VARS = ("temp", "rh", "ws", "wdir", "precip", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "fdr")
REGION_TZ = ZoneInfo("America/Winnipeg")  # "today" for the national product: central time

# (key, label, test(day) -> bool, severity) ; severity 2 = Concern, 1 = Watch
CRITERIA = [
    ("fdr_vh", "Fire danger Very High or Extreme", lambda c: (c.get("fdr") or 0) >= 3, 2),
    ("fwi_30", "FWI 30 or more (Extreme class)", lambda c: (c.get("fwi") or 0) >= 30, 2),
    ("fdr_h", "Fire danger High", lambda c: (c.get("fdr") or 0) == 2, 1),
    ("fwi_20", "FWI 20 or more", lambda c: 20 <= (c.get("fwi") or 0) < 30, 1),
    ("isi_10", "ISI 10 or more (fast spread)", lambda c: (c.get("isi") or 0) >= 10, 1),
    ("bui_60", "BUI 60 or more (deep burning)", lambda c: (c.get("bui") or 0) >= 60, 1),
    ("dc_425", "DC 425 or more (extreme drought; holdover fires)", lambda c: (c.get("dc") or 0) >= 425, 1),
    ("wind_rh", "Wind 30 km/h or more with RH 30% or less", lambda c: (c.get("ws") or 0) >= 30 and (c.get("rh") or 100) <= 30, 1),
]
STATUS = ["Normal", "Watch", "Concern"]


def evaluate(row):
    """Flags for one day -> (list of criterion keys, severity 0-2)."""
    hits = [(k, sev) for k, _, test, sev in CRITERIA if row and test(row)]
    return [k for k, _ in hits], max((s for _, s in hits), default=0)


def build(max_days=8):
    areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
    today = datetime.now(timezone.utc).astimezone(REGION_TZ).date()
    last = firewx.cwfis_date_range() or today + timedelta(days=6)
    last = min(last, today + timedelta(days=max_days - 1))
    days = [today + timedelta(days=k) for k in range((last - today).days + 1)]

    def one(a):
        daily = firewx.cwfis_daily(a["lat"], a["lon"], today, last, SUMMARY_VARS)
        rows, worst, flags_all = [], 0, {}
        for d in days:
            c = daily.get(d)
            keys, sev = evaluate(c)
            worst = max(worst, sev)
            for k in keys:
                flags_all.setdefault(k, []).append(d.isoformat())
            rows.append({"date": d.isoformat(), "flags": keys, "severity": sev,
                         **({k: c.get(k) for k in (*SUMMARY_VARS, "fdr_label", "cell_km")} if c else {"missing": True})})
        fwis = [(r["fwi"], r["date"]) for r in rows if r.get("fwi") is not None]
        max_fwi = max(fwis, default=(None, None))
        return {
            "id": a["id"], "name": a["name"], "prov": a.get("prov"), "lat": a["lat"], "lon": a["lon"],
            "status": STATUS[worst], "severity": worst, "flags": flags_all,
            "max_fwi": max_fwi[0], "max_fwi_date": max_fwi[1],
            "cell_km": max((r.get("cell_km") or 0) for r in rows),
            "days": rows,
        }

    with ThreadPoolExecutor(4) as ex:  # each area already fans out over the shared CWFIS pool
        bases = list(ex.map(one, areas))
    bases.sort(key=lambda b: b["lon"])  # west to east
    return {
        "issued": datetime.now(timezone.utc).astimezone(REGION_TZ).isoformat(),
        "dates": [d.isoformat() for d in days],
        "cwfis_last": firewx.cwfis_date_range().isoformat() if firewx.cwfis_date_range() else None,
        "criteria": [{"key": k, "label": label, "severity": STATUS[sev]} for k, label, _, sev in CRITERIA],
        "counts": {s: sum(b["status"] == s for b in bases) for s in STATUS},
        "bases": bases,
    }
