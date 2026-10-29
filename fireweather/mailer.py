"""Scheduled email of the fire weather forecast report.

Recipients and schedule live in mailing_list.json (project root, git-ignored; see mailing_list.example.json):

    {"reports": [{"id": "shilo-daily", "aoi": "shilo", "time": "06:30", "days": ["mon", "tue", "wed", "thu", "fri"],
                  "fire_behaviour": false, "scale": "mb", "to": ["chief@example.ca"], "cc": [], "bcc": []}]}

"time" is local time at the area. Each report goes out once a day, at or after its time (up to MAIL_LATE_LIMIT_H
late, e.g. after a restart). The email carries a summary table, any ECCC alerts in effect, and the full report as
a PDF printed by a headless Edge/Chrome from /static/firewx.html.

SMTP settings come from .env (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY, MAIL_FROM).
Without SMTP_HOST nothing is sent: each email is written to data/outbox/ as a .eml file instead (dry run).
"""
import html
import json
import logging
import os
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlencode

from . import config, firewx, report

log = logging.getLogger("mailer")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Manitoba scale from FWI (as on the report) and CWFIS's own classes; colours match the report cells
MB = [(14, "Low", "#dbe8fb"), (24, "Moderate", "#dcf3e2"), (33, "High", "#fff3b8"), (1e9, "Extreme", "#ffcfca")]
FDR = [("Low", "#dbe8fb"), ("Moderate", "#dcf3e2"), ("High", "#fff3b8"), ("Very High", "#ffdcb0"), ("Extreme", "#ffcfca")]
ALERT_BG = {"warning": "#fdecec", "watch": "#fff4e5", "statement": "#fffbe8"}


def run_tag(run):
    """Forecast version for names: the RDPS run, e.g. "2026-10-26 12Z"."""
    return f"{run[:4]}-{run[4:6]}-{run[6:8]} {run[8:10]}Z"


def rating(row, scale):
    """(label, colour) for one day's danger on the chosen scale."""
    if not row:
        return None, None
    if scale == "cwfis":
        k = row.get("fdr")
        return FDR[int(k)] if k is not None and 0 <= k <= 4 else (None, None)
    fwi = row.get("fwi")
    if fwi is None:
        return None, None
    return next((lbl, col) for ub, lbl, col in MB if fwi < ub)


def find_browser():
    """A Chromium-family browser for printing the PDF: PDF_BROWSER, else Edge/Chrome in the usual places."""
    cands = [os.environ.get("PDF_BROWSER"),
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             *(shutil.which(n) for n in ("msedge", "google-chrome", "chromium", "chromium-browser"))]
    return next((c for c in cands if c and Path(c).exists()), None)


def print_pdf(url, out_path):
    browser = find_browser()
    if not browser:
        raise RuntimeError("no Edge/Chrome found for PDF printing (set PDF_BROWSER)")
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as prof:
        subprocess.run([browser, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}", "--no-first-run",
                        "--no-pdf-header-footer", "--virtual-time-budget=90000", f"--print-to-pdf={out_path}", url],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240, check=False)
    if not Path(out_path).exists() or Path(out_path).stat().st_size < 5000:
        raise RuntimeError("PDF printing produced no output")


class Mailer:
    def __init__(self, store, alerts):
        self.store, self.alerts = store, alerts
        self.lock = threading.Lock()
        self.state_file = config.DATA_DIR / "mail_state.json"
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}  # report id -> {"sent": "YYYY-MM-DD", "at": iso, "error": str|None, "tried": epoch}

    # ---- configuration ---------------------------------------------------------------------------
    @staticmethod
    def reports():
        if not config.MAIL_LIST_FILE.exists():
            return []
        lst = json.loads(config.MAIL_LIST_FILE.read_text(encoding="utf-8")).get("reports", [])
        return [r for r in lst if r.get("id") and r.get("aoi") and (r.get("to") or r.get("bcc"))]

    @staticmethod
    def area(aoi_id):
        areas = json.loads(config.AOI_FILE.read_text(encoding="utf-8"))["areas"]
        a = next((x for x in areas if x["id"] == aoi_id), None)
        if not a:
            raise LookupError(f"unknown area {aoi_id!r} in mailing list")
        return a

    def _save(self):
        self.state_file.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    # ---- building the email ----------------------------------------------------------------------
    def build(self, rep):
        a = self.area(rep["aoi"])
        fbp, scale = bool(rep.get("fire_behaviour")), rep.get("scale", "mb")
        if not firewx.in_manitoba(a["lat"], a["lon"]):
            scale = "cwfis"  # the Manitoba scale applies only in Manitoba
        data = firewx.build(self.store, a["lat"], a["lon"], None, 2, float(rep.get("radius_km", 10)), None, fbp)
        al = (self.alerts.for_area(a["id"]) or {}).get("alerts", [])
        tz = report.local_zone(a["lat"], a["lon"])
        today = datetime.now(timezone.utc).astimezone(tz)

        rows = []
        for d in data["days"]:
            if d["kind"] == "past":
                continue
            src = d["cwfis"] or d["rdps"]  # CWFIS daily grid, else the RDPS daily values
            if not src:
                continue
            lbl, col = rating(src, scale)
            rows.append({"date": date.fromisoformat(d["date"]), "src": "CWFIS" if d["cwfis"] else "RDPS",
                         "label": lbl, "colour": col, **{k: src.get(k) for k in
                                                         ("fwi", "isi", "bui", "ws", "wdir", "rh", "temp", "precip")}})
        first = rows[0] if rows else None
        scale_name = "CWFIS rating" if scale == "cwfis" else "Manitoba scale"
        subject = f"Fire Weather Forecast – {a['name']} – {today:%a %b} {today.day} (RDPS {run_tag(data['meta']['run'])})"
        if first and first["label"]:
            subject += f": {first['label']}" + (f" (FWI {first['fwi']:.0f})" if first["fwi"] is not None else "")
        if al:
            subject = "⚠ " + subject + f" – {len(al)} ECCC alert{'s' if len(al) > 1 else ''}"

        q = {"aoi": a["id"], "fbp": "1" if fbp else "0", "scale": scale}
        live = f"{config.PUBLIC_URL.rstrip('/')}/static/firewx.html?{urlencode(q)}" if config.PUBLIC_URL else None
        local = f"{config.SELF_URL}/static/firewx.html?{urlencode(q)}"
        return a, data, rows, al, subject, scale_name, live, local

    @staticmethod
    def _n(v, d=0):
        return "—" if v is None else f"{v:.{d}f}"

    def body(self, a, data, rows, al, scale_name, live, pdf_ok):
        n, esc = self._n, html.escape
        card = report.compass
        m = data["meta"]
        run = f"{m['run'][8:]}Z {m['run'][:4]}-{m['run'][4:6]}-{m['run'][6:8]}"
        # plain text
        t = [f"Fire Weather Forecast – {a['name']}", f"Issued {datetime.fromisoformat(m['issued']):%Y-%m-%d %H:%M} "
             f"({m['timezone']}), RDPS {run}, CWFIS grids through {m['cwfis_last']}", ""]
        if al:
            t += ["ECCC ALERTS IN EFFECT:"] + [f"  - {x['name']}: {x.get('text', '')[:300]}" for x in al] + [""]
        t += [f"{'Day':<11}{'Danger':<10}{'FWI':>5}{'ISI':>6}{'BUI':>6}  {'Wind':<9}{'RH':>4}{'Rain':>7}"]
        for r in rows:
            t.append(f"{r['date']:%a %b %d}  {(r['label'] or '—'):<10}{n(r['fwi'], 1):>5}{n(r['isi'], 1):>6}"
                     f"{n(r['bui'], 0):>6}  {(card(r['wdir']) or '') + ' ' + n(r['ws']):<9}{n(r['rh']):>4}"
                     f"{n(r['precip'], 1):>6}mm")
        t += ["", f"Danger: {scale_name}. Values for 1200 LST; winds km/h. Days without a CWFIS forecast use RDPS."]
        if pdf_ok:
            t.append("The full report is attached as a PDF.")
        if live:
            t.append(f"Live report: {live}")
        t += ["", "Automated guidance from CWFIS and ECCC model output, for review by a qualified fire weather forecaster."]

        # HTML (inline styles: email clients ignore stylesheets)
        td = "padding:5px 8px;border-bottom:1px solid #d5dae0;text-align:right;white-space:nowrap"
        trs = "".join(
            f"<tr><td style='{td};text-align:left'>{r['date']:%a %b} {r['date'].day}"
            f"{'' if r['src'] == 'CWFIS' else ' <small style=color:#5c6670>(RDPS)</small>'}</td>"
            f"<td style='{td};text-align:left;font-weight:600;background:{r['colour'] or '#fff'}'>{esc(r['label'] or '—')}</td>"
            f"<td style='{td};font-weight:600'>{n(r['fwi'], 1)}</td><td style='{td}'>{n(r['isi'], 1)}</td>"
            f"<td style='{td}'>{n(r['bui'], 0)}</td><td style='{td}'>{card(r['wdir']) or ''} {n(r['ws'])}</td>"
            f"<td style='{td}'>{n(r['rh'])}</td><td style='{td}'>{n(r['temp'], 1)}</td><td style='{td}'>{n(r['precip'], 1)}</td></tr>"
            for r in rows)
        th = "padding:5px 8px;border-bottom:2px solid #16191d;text-align:right;font-size:12px;color:#5c6670"
        heads = ["Day", "Danger", "FWI", "ISI", "BUI", "Wind km/h", "RH %", "Temp °C", "Rain mm"]
        alerts_html = "".join(
            f"<div style='margin:6px 0;padding:8px 10px;border-left:4px solid #b45309;background:{ALERT_BG.get(x['level'], '#fff4e5')}'>"
            f"<b>{esc(x['name'])}</b><div style='color:#39424b;margin-top:3px'>{esc(x.get('text', '')[:500])}</div>"
            + (f"<a href='{esc(x['link'])}'>weather.gc.ca</a>" if x.get("link") else "") + "</div>" for x in al)
        h = f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#16191d;max-width:760px">
<h2 style="margin:0 0 2px">Fire Weather Forecast – {esc(a['name'])}</h2>
<div style="color:#5c6670;font-size:12px">Issued {datetime.fromisoformat(m['issued']):%a %b %d, %H:%M} ({esc(m['timezone'])})
 · RDPS {run} · CWFIS grids through {esc(m['cwfis_last'] or '—')}</div>
{f'<h3 style="margin:14px 0 4px">ECCC alerts in effect</h3>{alerts_html}' if al else ''}
<table style="border-collapse:collapse;margin-top:12px;font-size:13px">
<tr>{''.join(f"<th style='{th}{';text-align:left' if i < 2 else ''}'>{x}</th>" for i, x in enumerate(heads))}</tr>{trs}</table>
<p style="color:#5c6670;font-size:12px">Danger: {scale_name}. Daily values for 1200 LST from the CWFIS daily grid;
days marked RDPS use the 10 km model. {'The full report is attached as a PDF. ' if pdf_ok else ''}
{f'<a href="{esc(live)}">Open the live report</a>.' if live else ''}</p>
<p style="color:#5c6670;font-size:11px">Automated guidance from CWFIS and ECCC model output, for review by a qualified
fire weather forecaster.</p></div>"""
        return "\n".join(t), h

    def compose(self, rep):
        a, data, rows, al, subject, scale_name, live, local = self.build(rep)
        pdf, pdf_err = None, None
        with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
            p = Path(tmp) / "report.pdf"
            try:
                print_pdf(local, p)
                pdf = p.read_bytes()
            except Exception as e:
                pdf_err = str(e)
                log.warning("mail %s: PDF unavailable: %s", rep["id"], e)
        text, htm = self.body(a, data, rows, al, scale_name, live, pdf is not None)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.MAIL_FROM or config.SMTP_USER or "fireweather@localhost"
        if rep.get("to"):
            msg["To"] = ", ".join(rep["to"])
        if rep.get("cc"):
            msg["Cc"] = ", ".join(rep["cc"])
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="fireweather")
        msg.set_content(text)
        msg.add_alternative(htm, subtype="html")
        if pdf:
            msg.add_attachment(pdf, maintype="application", subtype="pdf",
                               filename=f"fire-weather-{a['id']}-{run_tag(data['meta']['run']).replace(' ', '-')}.pdf")
        rcpt = [*rep.get("to", []), *rep.get("cc", []), *rep.get("bcc", [])]
        return msg, rcpt, pdf_err

    def deliver(self, msg, rcpt, rid):
        why = "no SMTP_HOST" if not config.SMTP_HOST else \
            "SMTP_PASSWORD not set" if config.SMTP_USER and not config.SMTP_PASSWORD else None
        if why:  # dry run
            out = config.DATA_DIR / "outbox"
            out.mkdir(parents=True, exist_ok=True)
            p = out / f"{datetime.now():%Y%m%d-%H%M%S}-{rid}.eml"
            p.write_bytes(bytes(msg))
            log.info("mail %s: %s, wrote %s (%d recipients)", rid, why, p.name, len(rcpt))
            return f"dry run ({why}): {p}"
        ctx = ssl.create_default_context()
        if config.SMTP_SECURITY == "ssl":
            s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=60)
        else:
            s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
        with s:
            if config.SMTP_SECURITY == "starttls":
                s.starttls(context=ctx)
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg, to_addrs=rcpt)
        log.info("mail %s: sent to %d recipients", rid, len(rcpt))
        return f"sent to {len(rcpt)} recipients"

    def send(self, rep):
        msg, rcpt, pdf_err = self.compose(rep)
        result = self.deliver(msg, rcpt, rep["id"])
        return result + (f" (no PDF: {pdf_err})" if pdf_err else "")

    # ---- schedule --------------------------------------------------------------------------------
    def due(self, rep, now_utc):
        a = self.area(rep["aoi"])
        tz = report.local_zone(a["lat"], a["lon"])
        now = now_utc.astimezone(tz)
        if DAYS[now.weekday()] not in [d.lower()[:3] for d in rep.get("days", DAYS)]:
            return None
        hh, mm = (int(x) for x in rep.get("time", "06:30").split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        st = self.state.get(rep["id"], {})
        if st.get("sent") == now.date().isoformat() or not (at <= now < at + timedelta(hours=config.MAIL_LATE_LIMIT_H)):
            return None
        if time.time() - st.get("tried", 0) < config.MAIL_RETRY_MIN * 60:
            return None
        return now.date().isoformat()

    def tick(self):
        now = datetime.now(timezone.utc)
        for rep in self.reports():
            try:
                day = self.due(rep, now)
            except Exception as e:
                log.warning("mail %s: bad entry: %s", rep.get("id"), e)
                continue
            if not day:
                continue
            with self.lock:
                st = self.state.setdefault(rep["id"], {})
                st["tried"] = time.time()
                try:
                    res = self.send(rep)
                    st.update(sent=day, at=now.isoformat(timespec="seconds"), error=None, result=res)
                except Exception as e:
                    log.exception("mail %s failed (retrying in %d min)", rep["id"], config.MAIL_RETRY_MIN)
                    st["error"] = str(e)
                self._save()

    def send_now(self, rid):
        rep = next((r for r in self.reports() if r["id"] == rid), None)
        if not rep:
            raise LookupError(f"no report {rid!r} in {config.MAIL_LIST_FILE.name}")
        with self.lock:
            return self.send(rep)

    def view(self):
        return [{"id": r["id"], "aoi": r["aoi"], "time": r.get("time", "06:30"), "days": r.get("days", DAYS),
                 "recipients": len(r.get("to", [])) + len(r.get("cc", [])) + len(r.get("bcc", [])),
                 **{k: v for k, v in self.state.get(r["id"], {}).items() if k != "tried"}} for r in self.reports()]

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("mail scheduler failed")
            stop.wait(60)
