"""ECCC public weather alerts for each area of interest.

An area with an "alert_feed" in aoi.json (e.g. "mbrm67") is read from its ECCC battleboard Atom feed,
https://weather.gc.ca/rss/battleboard/<code>_e.xml, the same region the public warnings page uses.
Other areas fall back to the MSC GeoMet weather-alerts API at the area's point (externals.alerts).

Polled every ALERT_POLL_MIN minutes; changes are logged and pushed to the pages over SSE.
"""
import html
import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from . import config, externals

log = logging.getLogger("alerts")

FEED_URL = "https://weather.gc.ca/rss/battleboard/{code}_e.xml"
PAGE_URL = "https://weather.gc.ca/warnings/report_e.html?{code}"
ATOM = "{http://www.w3.org/2005/Atom}"
_session = requests.Session()


def level(name):
    """Coarse severity from the alert name: warning > watch > advisory/statement."""
    n = name.lower()
    if "warning" in n or n.startswith("red"):
        return "warning"
    if "watch" in n or n.startswith("orange"):
        return "watch"
    return "statement"


def _strip(s):
    return re.sub(r"[ \t\xa0]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_feed(xml_text):
    """Active alerts from a battleboard Atom feed (skips "No alerts in effect" and ENDED entries)."""
    root = ET.fromstring(xml_text)
    region = (root.findtext(f"{ATOM}title") or "").split(" - Weather Alert")[0].strip()
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        t = title.lower()
        if t.startswith("no ") or " ended" in t or t.startswith("ended"):
            continue
        name = re.sub(r"\s+in effect$", "", title.split(",")[0].strip(), flags=re.I)
        link = e.find(f"{ATOM}link")
        out.append({
            "name": name.title() if name.isupper() else name,
            "level": level(name),
            "issued": e.findtext(f"{ATOM}updated"),
            "expires": None,
            "text": _strip(e.findtext(f"{ATOM}summary")),
            "link": link.get("href") if link is not None else None,
            "air_quality": any(w in t for w in externals.AIR_QUALITY_WORDS),
        })
    return region, out


class Alerts:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_area = {}  # area id -> {...}
        self.missing = {}  # (area id, alert name) -> consecutive checks it has been absent

    def _fetch_area(self, a):
        code = a.get("alert_feed")
        if code:
            r = _session.get(FEED_URL.format(code=code), timeout=20)
            r.raise_for_status()
            region, items = parse_feed(r.text)
            return {"source": "ECCC battleboard", "feed": code, "region": region,
                    "page": PAGE_URL.format(code=code), "alerts": items}
        items = [x | {"level": level(x["name"])} for x in externals.alerts(a["lat"], a["lon"], strict=True)]
        return {"source": "MSC GeoMet", "feed": None, "region": items[0].get("area") if items else None,
                "page": externals.alert_page(a["lat"], a["lon"]), "alerts": items}

    def poll(self):
        areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changed = False
        for a in areas:
            with self.lock:
                prev = self.by_area.get(a["id"])
            try:
                cur = self._fetch_area(a) | {"error": None}
            except Exception as e:  # keep the last good result, flag the error
                log.warning("alerts for %s unavailable: %s", a["id"], e)
                cur = (prev or {"source": None, "feed": a.get("alert_feed"), "region": None, "page": None,
                                "alerts": []}) | {"error": str(e)}
            cur |= {"id": a["id"], "name": a["name"], "prov": a.get("prov"), "lat": a["lat"], "lon": a["lon"],
                    "checked": now}
            # ECCC re-issues ongoing alerts ("continued"); for a moment the old version is gone and the new one
            # isn't listed yet. Keep a vanished alert until it has been missing for ALERT_END_POLLS checks in a row.
            seen = {x["name"] for x in cur["alerts"]}
            for x in (prev or {}).get("alerts", []):
                k = (a["id"], x["name"])
                if x["name"] in seen:
                    continue
                self.missing[k] = self.missing.get(k, 0) + 1
                if self.missing[k] < config.ALERT_END_POLLS:
                    cur["alerts"].append(x)
                else:
                    self.missing.pop(k, None)
            for n in seen:
                self.missing.pop((a["id"], n), None)
            old = {x["name"] for x in (prev or {}).get("alerts", [])}
            new = {x["name"] for x in cur["alerts"]}
            if prev is None or old != new:
                changed = changed or prev is not None or bool(new)
                for n in new - old:
                    log.warning("ALERT %s: %s", a["name"], n)
                for n in old - new:
                    log.info("alert ended %s: %s", a["name"], n)
            with self.lock:
                self.by_area[a["id"]] = cur
        return changed

    def view(self):
        with self.lock:
            return list(self.by_area.values())

    def for_area(self, area_id):
        with self.lock:
            return self.by_area.get(area_id)

    def near(self, lat, lon, km=30):
        """Feed-based alerts of the closest area within km of a point (for reports at custom points)."""
        with self.lock:
            best = min(((externals.km_between(lat, lon, v["lat"], v["lon"]), v) for v in self.by_area.values()
                        if v.get("feed")), default=(None, None), key=lambda t: t[0])
        return best[1] if best[0] is not None and best[0] <= km else None

    def loop(self, stop, on_change=None):
        while not stop.is_set():
            try:
                if self.poll() and on_change:
                    on_change()
            except Exception:
                log.exception("alert poll failed")
            stop.wait(config.ALERT_POLL_MIN * 60)
