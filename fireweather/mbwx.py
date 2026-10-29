"""Manitoba Wildfire Service daily fire weather reports: observed and forecast noon weather and FWI codes.

Each day has two HTML reports, one table row per station (about 100: Wildfire Service, Manitoba Agriculture and
ECCC stations), columns TEMP RH WD WS RAIN FFMC DMC DC ISI BUI FWI DSR:
  https://www.gov.mb.ca/conservation_fire/Wx-Report/<YYYY>/<YYYYMMDD>-obs-wxrep.html   (observed)
  https://www.gov.mb.ca/conservation_fire/Wx-Report/<YYYY>/<YYYYMMDD>-fcst-wxrep.html  (forecast for that date)
Days without a report redirect (302). Reports go back to at least 2016.

Rows are kept in data/mbwx.sqlite, keyed by (date, kind, station), so a forecast can be compared with what was
then observed. The server re-checks the last few days every hour; `python -m fireweather.mbwx backfill FROM TO`
fills past seasons.
"""
import argparse
import html
import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger("mbwx")

URL = "https://www.gov.mb.ca/conservation_fire/Wx-Report/{y}/{d}-{kind}-wxrep.html"
KINDS = {"obs": "obs", "fcst": "fcst"}
COLS = ["temp", "rh", "wd", "ws", "rain", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr"]
DB = config.DATA_DIR / "mbwx.sqlite"
TZ = ZoneInfo("America/Winnipeg")
_session = requests.Session()
_session.headers["User-Agent"] = "fireweather (CFB Shilo fire weather; contact via the site owner)"
_db_lock = threading.Lock()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute(f"""CREATE TABLE IF NOT EXISTS report (
        date TEXT, kind TEXT, station TEXT, {", ".join(f"{c} REAL" for c in COLS)},
        PRIMARY KEY (date, kind, station))""")
    con.execute("""CREATE TABLE IF NOT EXISTS fetched (
        date TEXT, kind TEXT, status INTEGER, rows INTEGER, at TEXT, PRIMARY KEY (date, kind))""")
    return con


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse(text):
    """Station rows from a report page: {station: {col: value}}. Pages up to 2015 nest the data table inside
    layout tables, so start from the table that holds the STATION header."""
    i = text.find(">STATION<")
    if i < 0:
        i = text.find("STATION")
    if i < 0:
        return {}
    t = text[text.rfind("<table", 0, i):text.find("</table>", i)]
    out = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
        if len(cells) < 1 + len(COLS) or cells[0].upper() == "STATION" or not cells[0]:
            continue
        out[cells[0]] = dict(zip(COLS, map(_num, cells[1:1 + len(COLS)])))
    return out


def fetch(day, kind):
    """(status, rows) for one report; status 200 with rows, or 404 when the day has no report."""
    url = URL.format(y=day.year, d=day.strftime("%Y%m%d"), kind=KINDS[kind])
    r = _session.get(url, timeout=30, allow_redirects=False)
    if r.status_code in (301, 302, 404):
        return 404, {}
    r.raise_for_status()
    return 200, parse(r.text)


def store(day, kind, status, rows):
    with _db_lock:
        con = connect()
        with con:
            con.execute("DELETE FROM report WHERE date=? AND kind=?", (day.isoformat(), kind))
            con.executemany(f"INSERT INTO report VALUES (?,?,?,{','.join('?' * len(COLS))})",
                            [(day.isoformat(), kind, st, *(v[c] for c in COLS)) for st, v in rows.items()])
            con.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?,?,?)",
                        (day.isoformat(), kind, status, len(rows), datetime.now(TZ).isoformat(timespec="seconds")))
        con.close()


def fetched(day, kind):
    with _db_lock:
        con = connect()
        row = con.execute("SELECT status, rows, at FROM fetched WHERE date=? AND kind=?", (day.isoformat(), kind)).fetchone()
        con.close()
    return row


def update(days_back=3):
    """Fetch the recent days' reports (they appear during the day and can be revised)."""
    today = datetime.now(TZ).date()
    n = 0
    for k in range(days_back, -2, -1):  # a few days back, today, and tomorrow's forecast if out
        day = today - timedelta(days=k)
        for kind in KINDS:
            try:
                status, rows = fetch(day, kind)
            except Exception as e:
                log.warning("Manitoba %s report %s unavailable: %s", kind, day, e)
                continue
            if status == 200 or not fetched(day, kind):
                store(day, kind, status, rows)
                n += len(rows)
    return n


def backfill(start, end, delay=0.5, skip_done=True):
    """Fetch every day's reports in [start, end], politely; days already fetched are skipped."""
    day, got, missing = start, 0, 0
    while day <= end:
        for kind in KINDS:
            done = fetched(day, kind)
            if skip_done and done and (done[0] != 200 or done[1] > 0):  # re-fetch reports stored with no rows
                continue
            try:
                status, rows = fetch(day, kind)
            except Exception as e:
                log.warning("Manitoba %s report %s failed: %s", kind, day, e)
                time.sleep(delay * 4)
                continue
            store(day, kind, status, rows)
            got += status == 200
            missing += status != 200
            time.sleep(delay)
        if day.day == 1:
            log.info("Manitoba reports backfill: at %s (%d reports, %d days without)", day, got, missing)
        day += timedelta(days=1)
    log.info("Manitoba reports backfill done %s..%s: %d reports, %d without", start, end, got, missing)


def stations():
    with _db_lock:
        con = connect()
        rows = con.execute("""SELECT station, MIN(date), MAX(date), COUNT(*) FROM report WHERE kind='obs'
                              GROUP BY station ORDER BY station""").fetchall()
        con.close()
    return [{"station": s, "first": a, "last": b, "days": n} for s, a, b, n in rows]


def series(station, start, end):
    """Observed and forecast rows for a station between two dates (inclusive), by date."""
    with _db_lock:
        con = connect()
        rows = con.execute(f"SELECT date, kind, {', '.join(COLS)} FROM report WHERE station=? AND date BETWEEN ? AND ? "
                           "ORDER BY date", (station, start, end)).fetchall()
        con.close()
    out = {}
    for d, kind, *vals in rows:
        out.setdefault(d, {"date": d, "obs": None, "fcst": None})[kind] = dict(zip(COLS, vals))
    return list(out.values())


def loop(stop):
    while not stop.is_set():
        try:
            n = update()
            log.info("Manitoba fire weather reports: %d station rows refreshed", n)
        except Exception:
            log.exception("Manitoba reports update failed")
        stop.wait(config.MBWX_POLL_MIN * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Manitoba Wildfire Service daily fire weather reports")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="fetch past reports, e.g. backfill 2016-04-01 2026-10-31")
    b.add_argument("start", type=date.fromisoformat)
    b.add_argument("end", type=date.fromisoformat)
    b.add_argument("--delay", type=float, default=0.5, help="seconds between requests (be polite)")
    b.add_argument("--months", default="", help="only these months, e.g. 4-10 for fire seasons")
    a = ap.parse_args()
    if a.cmd == "backfill":
        if a.months:
            lo, hi = (int(x) for x in a.months.split("-"))
            for y in range(a.start.year, a.end.year + 1):
                month_end = (date(y + 1, 1, 1) if hi == 12 else date(y, hi + 1, 1)) - timedelta(days=1)
                s, e = max(a.start, date(y, lo, 1)), min(a.end, month_end)
                if s <= e:
                    backfill(s, e, a.delay)
        else:
            backfill(a.start, a.end, a.delay)
