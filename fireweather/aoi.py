"""Areas of interest (aoi.json): validation and saving for the Areas editor page.

Each area: id (slug), name, prov, lat/lon (the reference point used for point forecasts), and optionally
alert_feed (ECCC battleboard code), mb_stations (Manitoba report stations, nearest first) and boundary
(the training area as a polygon, [[lat, lon], ...], a drawn bounding box being a 4-corner polygon). The server adds
bbox [west, south, east, north] from the boundary. Every save keeps the previous file in data/aoi_backups/.
"""
import ipaddress
import json
import re
from datetime import datetime

from . import config

MAX_VERTICES = 2000
NOTE = ("Areas of interest for the dropdowns. The first entry is the default. Edit with the Areas page (/static/aoi.html) "
        "or by hand. lat/lon is the reference point for point forecasts; boundary (optional) is the training area as "
        "[[lat, lon], ...] and bbox [west, south, east, north] is derived from it. alert_feed (optional) is the ECCC "
        "public alert region code from weather.gc.ca/rss/battleboard/<code>_e.xml; areas without one use ECCC alerts "
        "at the point. mb_stations (optional) names the Manitoba Wildfire Service report stations for the area, "
        "nearest first.")


def load():
    return json.loads(config.AOI_FILE.read_text(encoding="utf-8"))


def may_edit(host):
    """Edits only from this machine or a Tailscale device (100.64.0.0/10), not from anyone else on the LAN."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _num(v, lo, hi, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not lo <= x <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return round(x, 5)


def validate(doc):
    """A clean aoi.json document from an edited one, or ValueError naming the problem."""
    areas = doc.get("areas")
    if not isinstance(areas, list) or not areas:
        raise ValueError("there must be at least one area")
    w, e, s, n = config.BOUNDS["west"], config.BOUNDS["east"], config.BOUNDS["south"], config.BOUNDS["north"]
    out, ids = [], set()
    for i, a in enumerate(areas, 1):
        aid = str(a.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", aid):
            raise ValueError(f"area {i}: id must be 1-40 lowercase letters, digits, - or _")
        if aid in ids:
            raise ValueError(f"area {i}: id {aid!r} is used twice")
        ids.add(aid)
        name = str(a.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError(f"{aid}: name is required (80 characters at most)")
        clean = {"id": aid, "name": name, "prov": str(a.get("prov", "")).strip().upper()[:3],
                 "lat": _num(a.get("lat"), s, n, f"{aid}: latitude"), "lon": _num(a.get("lon"), w, e, f"{aid}: longitude")}
        feed = str(a.get("alert_feed") or "").strip().lower()
        if feed:
            if not re.fullmatch(r"[a-z]{2}rm?\d{1,4}|[a-z0-9]{2,12}", feed):
                raise ValueError(f"{aid}: alert feed code looks wrong (e.g. mbrm67)")
            clean["alert_feed"] = feed
        st = [str(x).strip() for x in (a.get("mb_stations") or []) if str(x).strip()]
        if st:
            clean["mb_stations"] = st[:5]
        b = a.get("boundary")
        if b:
            if not isinstance(b, list) or not 3 <= len(b) <= MAX_VERTICES:
                raise ValueError(f"{aid}: boundary needs 3 to {MAX_VERTICES} corners")
            ring = [[_num(p[0], -90, 90, f"{aid}: boundary latitude"), _num(p[1], -180, 180, f"{aid}: boundary longitude")]
                    for p in b]
            if ring[0] == ring[-1]:
                ring = ring[:-1]
            clean["boundary"] = ring
            lats, lons = [p[0] for p in ring], [p[1] for p in ring]
            clean["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
        out.append(clean)
    default = str(doc.get("default") or out[0]["id"])
    if default not in ids:
        raise ValueError(f"default area {default!r} isn't in the list")
    return {"_note": NOTE, "default": default, "areas": out}


def save(doc):
    clean = validate(doc)
    backups = config.DATA_DIR / "aoi_backups"
    backups.mkdir(parents=True, exist_ok=True)
    if config.AOI_FILE.exists():
        (backups / f"aoi-{datetime.now():%Y%m%d-%H%M%S}.json").write_bytes(config.AOI_FILE.read_bytes())
    tmp = config.AOI_FILE.with_suffix(".json.tmp")
    rows = ",\n".join("    " + json.dumps(a, ensure_ascii=False) for a in clean["areas"])  # one area per line
    tmp.write_text("{\n" f'  "_note": {json.dumps(clean["_note"])},\n  "default": {json.dumps(clean["default"])},\n'
                   f'  "areas": [\n{rows}\n  ]\n}}\n', encoding="utf-8")
    tmp.replace(config.AOI_FILE)
    return clean


def backups():
    d = config.DATA_DIR / "aoi_backups"
    return sorted((p.name for p in d.glob("aoi-*.json")), reverse=True)[:30] if d.exists() else []


def restore(name):
    p = config.DATA_DIR / "aoi_backups" / name
    if not re.fullmatch(r"aoi-\d{8}-\d{6}\.json", name) or not p.exists():
        raise ValueError("no such backup")
    return save(json.loads(p.read_text(encoding="utf-8")))
