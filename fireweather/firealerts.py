"""Fire detection alerts: satellite hotspots (NASA FIRMS) inside an area's drawn boundary.

Checked after every FIRMS poll. An area is on alert while it has a detection inside its boundary in the last
WINDOW_H hours; persistent industrial heat sources are left out. Areas without a drawn boundary are skipped
(draw one in the area editor).

Optional email, configured in mailing_list.json (git-ignored) under "fire_alerts":
    {"id": "shilo-fires", "aois": ["shilo"], "include_low_conf": false, "to": [...], "cc": [], "bcc": []}
"aois" may be "all". Each detection is emailed once; new detections in later passes send a follow-up.
Low-confidence detections are shown on the map but only emailed with include_low_conf. Sent detection ids are kept
in data/fire_alerts_state.json.
"""
import html
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config, report

log = logging.getLogger("firealerts")

WINDOW_H = 24


def inside(lat, lon, ring):
    """Point in polygon (ring of [lat, lon]), ray casting."""
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        (yi, xi), (yj, xj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


class FireAlerts:
    def __init__(self, mailer):
        self.mailer = mailer
        self.lock = threading.Lock()
        self.state_file = config.DATA_DIR / "fire_alerts_state.json"
        try:
            self.sent = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.sent = []  # [{"id", "aoi", "detections": [ids], "sent_at", "result"}]
        self.active = []    # areas with detections: [{"aoi", "name", "count", "latest", ...}]
        self.checked = None

    @staticmethod
    def configs():
        if not config.MAIL_LIST_FILE.exists():
            return []
        return json.loads(config.MAIL_LIST_FILE.read_text(encoding="utf-8")).get("fire_alerts", [])

    def check(self, detections):
        """Called with the current FIRMS detections after each poll."""
        areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_H)).isoformat()
        recent = [d for d in detections if d["time"] >= cutoff and not d["persistent"]]
        active = []
        for a in areas:
            ring = a.get("boundary")
            if not ring or len(ring) < 3:
                continue
            w, s, e, n = a.get("bbox") or (min(p[1] for p in ring), min(p[0] for p in ring),
                                          max(p[1] for p in ring), max(p[0] for p in ring))
            hits = sorted((d for d in recent if s <= d["lat"] <= n and w <= d["lon"] <= e
                           and inside(d["lat"], d["lon"], ring)), key=lambda d: d["time"])
            if not hits:
                continue
            active.append({
                "aoi": a["id"], "name": a["name"], "count": len(hits),
                "first": hits[0]["time"], "latest": hits[-1]["time"],
                "max_frp": round(max(d["frp"] for d in hits), 1),
                "low_conf_only": all(d["low_conf"] for d in hits),
                "sensors": sorted({d["sensor"] for d in hits}),
                "detections": [{k: d[k] for k in ("id", "lat", "lon", "time", "sensor", "frp", "low_conf")} for d in hits],
                "tz": str(report.local_zone(a["lat"], a["lon"])),
            })
        with self.lock:
            self.active, self.checked = active, datetime.now(timezone.utc).isoformat(timespec="seconds")
        for cfg in self.configs():
            for hit in active:
                if cfg.get("aois", "all") != "all" and hit["aoi"] not in cfg.get("aois", []):
                    continue
                self._notify(cfg, hit)

    def _notify(self, cfg, hit):
        with self.lock:
            done = {i for s in self.sent if s["id"] == cfg["id"] and s["aoi"] == hit["aoi"] for i in s["detections"]}
        new = [d for d in hit["detections"] if d["id"] not in done and (cfg.get("include_low_conf") or not d["low_conf"])]
        if not new:
            return
        follow_up = bool(done)
        try:
            res = self._email(cfg, hit, new, follow_up)
        except Exception as ex:
            log.exception("fire alert %s: email failed", cfg["id"])
            res = f"email failed: {ex}"
        log.warning("FIRE ALERT %s: %d new detection(s), latest %s (%s)", hit["name"], len(new), new[-1]["time"], res)
        now = datetime.now(timezone.utc)
        with self.lock:
            self.sent.append({"id": cfg["id"], "aoi": hit["aoi"], "detections": [d["id"] for d in new],
                              "sent_at": now.isoformat(timespec="seconds"), "result": res})
            cutoff = now - timedelta(days=7)
            self.sent = [s for s in self.sent if datetime.fromisoformat(s["sent_at"]) >= cutoff]
            self.state_file.write_text(json.dumps(self.sent, indent=1), encoding="utf-8")

    def view(self):
        with self.lock:
            return {"active": self.active, "checked": self.checked, "window_h": WINDOW_H,
                    "recent": [{k: s[k] for k in ("id", "aoi", "sent_at")} | {"detections": len(s["detections"])}
                               for s in self.sent[-20:]]}

    # ---- email -------------------------------------------------------------------------------------------------
    def _email(self, cfg, hit, new, follow_up):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(hit["tz"])
        local = lambda iso: (lambda t: f"{t:%a %b} {t.day} {t:%H:%M %Z}")(datetime.fromisoformat(iso).astimezone(tz))
        n = len(new)
        what = f"{'New satellite' if follow_up else 'Satellite'} fire detection{'s' if n > 1 else ''}"
        subject = f"🔥 {what} – {hit['name']} – {local(new[-1]['time'])}"
        lines = [f"{n} {'new ' if follow_up else ''}satellite fire detection{'s' if n > 1 else ''} (NASA FIRMS) inside the "
                 f"{hit['name']} boundary:"]
        for d in new:
            lines.append(f"  {local(d['time'])}  {d['lat']:.4f}, {d['lon']:.4f}  {d['sensor']}  FRP {d['frp']:.1f} MW"
                         + ("  (low confidence)" if d["low_conf"] else ""))
        lines.append(f"In the last {WINDOW_H} h: {hit['count']} detection{'s' if hit['count'] > 1 else ''}, "
                     f"max FRP {hit['max_frp']} MW.")
        lines += ["", "Satellite detections are about 375 m (VIIRS) or 1 km (MODIS) pixels, seen at overpass times only, "
                  "and can include prescribed burns or other hot surfaces. Confirm on the ground or with range control.",
                  "Automated alert, for review by qualified staff."]
        if config.PUBLIC_URL:
            lines.append(f"Weather map: {config.PUBLIC_URL.rstrip('/')}/?aoi={hit['aoi']}")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.MAIL_FROM or config.SMTP_USER or "fireweather@localhost"
        if cfg.get("to"):
            msg["To"] = ", ".join(cfg["to"])
        if cfg.get("cc"):
            msg["Cc"] = ", ".join(cfg["cc"])
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="fireweather")
        msg.set_content("\n".join(lines))
        body = "<br>".join(html.escape(x).replace("  ", "&nbsp;&nbsp;") for x in lines)
        msg.add_alternative(f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
                            f'border-left:4px solid #d9480f;padding:6px 12px">{body}</div>', subtype="html")
        rcpt = [*cfg.get("to", []), *cfg.get("cc", []), *cfg.get("bcc", [])]
        return self.mailer.deliver(msg, rcpt, cfg["id"])
