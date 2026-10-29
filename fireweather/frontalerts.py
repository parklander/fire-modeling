"""Front watch alerts: email (and show on the weather map) when RDPS forecasts a frontal wind shift at a base.

Configured in mailing_list.json (git-ignored) under "front_alerts":
    {"id": "shilo-fronts", "aoi": "shilo", "within_hours": 24, "strength": "strong", "to": [...], "cc": [], "bcc": []}
strength "strong" = wind >= 30 km/h or gusts >= 50 after the shift (frontwatch.STRONG_*), "any" = every frontal shift.

Checked whenever the front watch is recomputed (new model run or WPC analysis). Each event is emailed once: an event
counts as already sent if one was sent for the same alert within EVENT_MATCH_H of its time (runs move a shift by an
hour or two). Sent events are kept in data/front_alerts_state.json.
"""
import html
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("frontalerts")

EVENT_MATCH_H = 4
KIND = {"cold": "Cold front", "warm": "Warm front", "stationary": "Stationary front", "occluded": "Occluded front",
        "trough": "Trough"}


class FrontAlerts:
    def __init__(self, mailer):
        self.mailer = mailer
        self.lock = threading.Lock()
        self.state_file = config.DATA_DIR / "front_alerts_state.json"
        try:
            self.sent = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.sent = []  # [{"id", "aoi", "event_time", "sent_at", "result"}]
        self.active = []    # current alerts for the map: [{"id", "aoi", "name", "event": {...}, "tz"}]

    @staticmethod
    def configs():
        if not config.MAIL_LIST_FILE.exists():
            return []
        return json.loads(config.MAIL_LIST_FILE.read_text(encoding="utf-8")).get("front_alerts", [])

    def _already(self, aid, t):
        return any(s["id"] == aid and abs((datetime.fromisoformat(s["event_time"]) - t).total_seconds()) <= EVENT_MATCH_H * 3600
                   for s in self.sent)

    def check(self, bases):
        """Called with the front watch for every base. Returns True if the active list changed."""
        now = datetime.now(timezone.utc)
        by_id = {b["id"]: b for b in bases}
        active, changed = [], False
        for cfg in self.configs():
            b = by_id.get(cfg.get("aoi"))
            if not b:
                continue
            horizon = now + timedelta(hours=float(cfg.get("within_hours", 24)))
            for e in b.get("rdps", []):
                t = datetime.fromisoformat(e["time"])
                if not (now - timedelta(hours=1) <= t <= horizon):
                    continue
                if cfg.get("strength", "strong") == "strong" and not e["strong"]:
                    continue
                active.append({"id": cfg["id"], "aoi": b["id"], "name": b["name"], "event": e, "wpc": b.get("wpc"),
                               "tz": b.get("tz")})
                with self.lock:
                    if self._already(cfg["id"], t):
                        continue
                try:
                    res = self._email(cfg, b, e)
                except Exception as ex:
                    log.exception("front alert %s: email failed", cfg["id"])
                    res = f"email failed: {ex}"
                log.warning("FRONT ALERT %s: %s %s %s -> %s %s km/h (%s)", b["name"], e["time"], e["from_card"],
                            e["from_speed"], e["to_card"], e["to_speed"], res)
                with self.lock:
                    self.sent.append({"id": cfg["id"], "aoi": b["id"], "event_time": e["time"],
                                      "sent_at": now.isoformat(timespec="seconds"), "result": res})
                    cutoff = now - timedelta(days=7)
                    self.sent = [s for s in self.sent if datetime.fromisoformat(s["sent_at"]) >= cutoff]
                    self.state_file.write_text(json.dumps(self.sent, indent=1), encoding="utf-8")
                changed = True
        with self.lock:
            key = lambda xs: sorted((x["id"], x["event"]["time"]) for x in xs)
            changed = changed or key(active) != key(self.active)
            self.active = active
        return changed

    def view(self):
        with self.lock:
            return {"active": self.active, "recent": self.sent[-20:]}

    # ---- email -------------------------------------------------------------------------------------------------
    def _email(self, cfg, b, e):
        tz = ZoneInfo(b.get("tz") or "America/Winnipeg")
        t = datetime.fromisoformat(e["time"]).astimezone(tz)
        when = f"{t:%a %b} {t.day} {t:%H:%M %Z}"
        shift = f"{e['from_card']} {e['from_speed']} → {e['to_card']} {e['to_speed']} km/h"
        gust = f", gusts {e['max_gust']}" if e.get("max_gust") is not None else ""
        subject = f"⚠ Frontal wind shift forecast – {b['name']} – {when}: {shift}{gust}"
        lines = [f"RDPS forecasts a frontal wind shift at {b['name']} around {when}:",
                 f"  Wind {shift}, peak {e['max_wind']} km/h{gust} in the 6 h after",
                 f"  Temperature {e['temp_change']:+.1f} °C, RH {e['rh_change']:+d}%"
                 + (", with a pressure trough" if e.get("pressure_trough") else "")]
        w = b.get("wpc")
        if w:
            m = w.get("motion")
            wl = f"{KIND.get(w['kind'], 'Front')} {w['distance_km']} km {w['lies']} (WPC analysis)"
            if m and m["speed_kmh"] >= 3:
                wl += f", moving {m['toward']} at {round(m['speed_kmh'])} km/h"
                if w.get("eta"):
                    eta = datetime.fromisoformat(w["eta"]).astimezone(tz)
                    wl += f", straight-line arrival ~{eta:%a %H:%M}"
            lines.append("  " + wl)
        lines += ["", "A wind shift can turn a flank into a head fire: review active burns, range fires and crews.",
                  "Automated guidance from the RDPS model and WPC analyses, for review by a qualified forecaster."]
        if config.PUBLIC_URL:
            lines.append(f"Weather map: {config.PUBLIC_URL.rstrip('/')}/?aoi={b['id']}")
        text = "\n".join(lines)
        body = "<br>".join(html.escape(x) for x in lines)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.MAIL_FROM or config.SMTP_USER or "fireweather@localhost"
        if cfg.get("to"):
            msg["To"] = ", ".join(cfg["to"])
        if cfg.get("cc"):
            msg["Cc"] = ", ".join(cfg["cc"])
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="fireweather")
        msg.set_content(text)
        msg.add_alternative(f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
                            f'border-left:4px solid #b42318;padding:6px 12px">{body}</div>', subtype="html")
        rcpt = [*cfg.get("to", []), *cfg.get("cc", []), *cfg.get("bcc", [])]
        return self.mailer.deliver(msg, rcpt, cfg["id"])
