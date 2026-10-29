"""Download RDPS GRIB2 files, convert to display frames, track per-run availability."""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

from . import config, grid

log = logging.getLogger("ingest")

FILE_RE = re.compile(
    r"(?P<date>\d{8})T(?P<hh>\d{2})Z_MSC_RDPS_(?P<token>.+?)_RLatLon0\.09_PT(?P<fh>\d{3})H\.grib2$")


def relpath(run, var, fh):
    date, hh = run[:8], run[8:]
    token = config.DOWNLOAD_VARS[var]["token"]
    return (f"{date}/WXO-DD/model_rdps/10km/{hh}/{fh:03d}/"
            f"{date}T{hh}Z_MSC_RDPS_{token}_RLatLon0.09_PT{fh:03d}H.grib2")


def parse_filename(name):
    """-> (run, var, fh) or None if not a file we want."""
    m = FILE_RE.search(name)
    if not m or m["token"] not in config.TOKEN_TO_VAR:
        return None
    return m["date"] + m["hh"], config.TOKEN_TO_VAR[m["token"]], int(m["fh"])


def frame_path(run, var, fh):
    return config.RUNS_DIR / run / var / f"{fh:03d}.u16.gz"


def frame_ok(path):
    """A frame starts with the gzip magic number. A crash or power loss mid-write can leave a full-size file of
    zeros (the rename reached the disk before the data); those fail this check."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def write_durably(path, data):
    """Write to a temp file, flush it to disk, then rename into place: a crash leaves either the old file or the
    complete new one, never a zero-filled one."""
    part = path.with_suffix(f".{threading.get_ident()}.part")
    with open(part, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    part.replace(path)


class EventBus:
    """Fan out events from worker threads to asyncio subscribers (SSE clients)."""

    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()
        self.loop = None

    def subscribe(self):
        q = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event):
        if self.loop is None:
            return
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            self.loop.call_soon_threadsafe(_put_nowait, q, event)


def _put_nowait(q, event):
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass


class Store:
    """Per-run manifest of which (var, hour) frames exist on disk."""

    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self.runs = {}  # run -> {var: set(hours)}
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        bad = 0
        for d in config.RUNS_DIR.iterdir():
            if d.is_dir() and re.fullmatch(r"\d{10}", d.name):
                self.runs[d.name] = {v: set() for v in config.ALL_VARS}
                for v in config.ALL_VARS:
                    for p in (d / v).glob("*.u16.gz") if (d / v).is_dir() else ():
                        if frame_ok(p):
                            self.runs[d.name][v].add(int(p.name[:3]))
                        else:  # damaged (e.g. by a crash): delete so it is downloaded / derived again
                            log.warning("deleting damaged frame %s", p.relative_to(config.RUNS_DIR))
                            p.unlink(missing_ok=True)
                            bad += 1
                    for p in (d / v).glob("*.part") if (d / v).is_dir() else ():
                        try:  # leftovers from an interrupted write (skip one another process is still writing)
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass
        if bad:
            log.warning("deleted %d damaged frames; they will be fetched again", bad)
        self.listeners = []  # called as fn(run, var, fh) after each new frame

    def has(self, run, var, fh):
        with self._lock:
            return fh in self.runs.get(run, {}).get(var, ())

    def remove(self, run, var, fh):
        """Delete an intermediate frame (aux inputs once their derived products exist)."""
        with self._lock:
            self.runs.get(run, {}).get(var, set()).discard(fh)
        frame_path(run, var, fh).unlink(missing_ok=True)

    def add(self, run, var, fh):
        with self._lock:
            r = self.runs.setdefault(run, {v: set() for v in config.ALL_VARS})
            r[var].add(fh)
            complete = self._complete(run)
        self.bus.publish({"type": "frame", "run": run, "var": var, "hour": fh, "complete": complete})
        for fn in self.listeners:
            fn(run, var, fh)
        if var not in config.VARIABLES:
            return
        if complete:
            log.info("run %s complete", run)
            self.prune()

    def _complete(self, run):
        r = self.runs.get(run, {})
        return all(len(r.get(v, ())) == len(config.FORECAST_HOURS) for v in config.VARIABLES)

    def summary(self):
        with self._lock:
            out = []
            for run in sorted(self.runs, reverse=True):
                r = self.runs[run]
                # hours where every variable is present
                hours = sorted(set.intersection(*(r[v] for v in config.VARIABLES))) if r else []
                index_hours = sorted(set.intersection(*(r[v] for v in config.INDICES))) if r else []
                smoke_hours = sorted(set.intersection(*(r[v] for v in config.SMOKE))) if r else []
                precip_hours = sorted(set.intersection(*(r[v] for v in config.PRECIP))) if r else []
                mslp_hours = sorted(r.get("mslp", ())) if r else []
                cloud_hours = sorted(r.get("cloud", ())) if r else []
                out.append({"run": run, "hours": hours, "index_hours": index_hours, "smoke_hours": smoke_hours,
                            "precip_hours": precip_hours, "mslp_hours": mslp_hours, "cloud_hours": cloud_hours,
                            "complete": self._complete(run),
                            "count": sum(len(r[v]) for v in config.VARIABLES),
                            "total": len(config.VARIABLES) * len(config.FORECAST_HOURS)})
            return out

    def prune(self):
        """Keep the newest KEEP_RUNS complete runs plus any newer incomplete ones."""
        with self._lock:
            complete = sorted((r for r in self.runs if self._complete(r)), reverse=True)
            if len(complete) <= config.KEEP_RUNS:
                return
            cutoff = complete[config.KEEP_RUNS - 1]
            doomed = [r for r in self.runs if r < cutoff]
            for r in doomed:
                del self.runs[r]
        for r in doomed:
            log.info("pruning run %s", r)
            shutil.rmtree(config.RUNS_DIR / r, ignore_errors=True)
            self.bus.publish({"type": "pruned", "run": r})


class Ingestor:
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(config.DOWNLOAD_WORKERS, thread_name_prefix="dl")
        self.session = requests.Session()
        self._inflight = set()
        self._lock = threading.Lock()

    def needed(self, run, var, fh):
        """Whether (run, var, fh) should be downloaded."""
        if fh not in config.var_hours(var) or self.store.has(run, var, fh):
            return False
        # aux inputs are deleted after use; don't fetch them again once derived products exist
        return not (var in config.AUX_VARS and self.store.has(run, "vi", fh))

    def submit(self, run, var, fh, url=None):
        """Queue a download unless we already have it or it's in progress."""
        key = (run, var, fh)
        if not self.needed(*key):
            return False
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
        self.pool.submit(self._work, run, var, fh, url or f"{config.DATAMART}/{relpath(run, var, fh)}")
        return True

    def _work(self, run, var, fh, url):
        try:
            for attempt in range(4):
                try:
                    self._fetch_and_convert(run, var, fh, url)
                    return
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        log.debug("not yet available: %s", url)
                        return  # reconcile will retry later
                    log.warning("download failed (%s) %s", e, url)
                except requests.RequestException as e:
                    log.warning("download failed (%s: %s), retrying %s", type(e).__name__, e, url)
                except Exception:
                    log.exception("processing failed %s", url)
                time.sleep(5 * (attempt + 1))
        finally:
            with self._lock:
                self._inflight.discard((run, var, fh))

    def _fetch_and_convert(self, run, var, fh, url):
        t0 = time.time()
        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tmp:
            try:
                with self.session.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(1 << 20):
                        tmp.write(chunk)
                tmp.close()
                raster = grid.grib_to_raster(tmp.name, var)
            finally:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
        self._write(run, var, fh, raster)
        log.info("%s %s f%03d  %.1fs", run, var, fh, time.time() - t0)
        # The datamart doesn't publish accumulated precip at T+0 (it's zero by definition).
        if fh == 0 and var != "precip" and not self.store.has(run, "precip", 0):
            self._write(run, "precip", 0, np.where(np.isnan(raster), np.nan, 0).astype(np.float32))

    def _write(self, run, var, fh, raster):
        write_frame(self.store, run, var, fh, raster)


def write_frame(store, run, var, fh, raster):
    out = frame_path(run, var, fh)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_durably(out, grid.encode(raster, var))
    store.add(run, var, fh)


def candidate_runs(now=None, back_hours=36):
    """Run ids (YYYYMMDDHH) from newest to oldest in the last `back_hours`."""
    now = now or datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0, hour=now.hour - now.hour % 6)
    out = []
    while t >= now - timedelta(hours=back_hours):
        out.append(t.strftime("%Y%m%d%H"))
        t -= timedelta(hours=6)
    return out


def list_hour_dir(session, run, fh):
    """Names of our files present in the datamart hour directory (empty if none)."""
    url = f"{config.DATAMART}/{run[:8]}/WXO-DD/model_rdps/10km/{run[8:]}/{fh:03d}/"
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return [n for n in re.findall(r'href="([^"]+\.grib2)"', r.text) if parse_filename(n)]


def reconcile(ingestor):
    """Catch up from the datamart over HTTP: covers startup and any missed AMQP messages.

    Picks the newest run that is fully published (f084 present) plus any newer runs
    that are still arriving, and queues whatever we don't have yet.
    """
    s = ingestor.session
    wanted = []
    for run in candidate_runs():
        if not list_hour_dir(s, run, 0):
            continue
        wanted.append(run)
        last = {parse_filename(n)[1] for n in list_hour_dir(s, run, config.FORECAST_HOURS[-1])}
        if set(config.VARIABLES) <= last:
            break  # newest complete run found; nothing older needed
    queued = 0
    for run in wanted:
        if ingestor.store.has(run, "temp", 0) and not ingestor.store.has(run, "precip", 0):
            h, w = grid.raster_shape()
            temp0 = grid.decode(frame_path(run, "temp", 0).read_bytes(), "temp").reshape(h, w)
            ingestor._write(run, "precip", 0, np.where(np.isnan(temp0), np.nan, 0).astype(np.float32))
        for fh in config.FORECAST_HOURS:
            if not any(ingestor.needed(run, v, fh) for v in config.DOWNLOAD_VARS if (v, fh) != ("precip", 0)):
                continue
            for name in list_hour_dir(s, run, fh):
                _, var, _ = parse_filename(name)
                queued += ingestor.submit(run, var, fh)
    log.info("reconcile: runs %s, queued %d downloads", wanted, queued)


def reconcile_loop(ingestor, stop):
    while not stop.is_set():
        try:
            reconcile(ingestor)
        except Exception:
            log.exception("reconcile failed")
        stop.wait(config.RECONCILE_EVERY_S)
