"""FastAPI app: serves the viewer, frames, point time series and live update events."""
import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts, amqp_listener, aoi, config, goes, firealerts, firelog, frontalerts, fronts, frontwatch, mailer, mbwx, precip, firemodel, firewx, fwi_engine, grid, hotspots, ingest, national, openmeteo, report, smoke, cwfis

log = logging.getLogger("server")

bus = ingest.EventBus()
store = ingest.Store(bus)
ingestor = ingest.Ingestor(store)
engine = fwi_engine.FWIEngine(store)
smoke_engine = smoke.SmokeEngine(store)
precip_engine = precip.PrecipEngine(store)
wx_alerts = alerts.Alerts()
wpc_fronts = fronts.Fronts()
front_watch = frontwatch.FrontWatch(store, wpc_fronts)
mail = mailer.Mailer(store, wx_alerts)
front_alerts = frontalerts.FrontAlerts(mail)
fire_alerts = firealerts.FireAlerts(mail)


def _incident_sim(run, ignitions, start, hours, center):
    """Growth run for a detected fire: domain sized to the detections plus room to spread."""
    lats = [f[0] for f in ignitions]; lons = [f[1] for f in ignitions]
    extent_km = max(firemodel_extent_km(lats, lons), 1.0)
    radius = min(max(8.0, extent_km + 6.0), 25.0)
    return firemodel.simulate(store, run, center[0], center[1], start, hours, radius_km=radius, ignitions=ignitions)


def firemodel_extent_km(lats, lons):
    import math
    return max(max(lats) - min(lats), (max(lons) - min(lons)) * math.cos(math.radians(sum(lats) / len(lats)))) * 111.32 / 2


fires = hotspots.Hotspots(store, _incident_sim)
_stop = threading.Event()
_point_pool = ThreadPoolExecutor(8)


@asynccontextmanager
async def lifespan(app):
    bus.loop = asyncio.get_running_loop()
    threads = [
        threading.Thread(target=amqp_listener.listen, args=(ingestor, _stop), name="amqp", daemon=True),
        threading.Thread(target=ingest.reconcile_loop, args=(ingestor, _stop), name="reconcile", daemon=True),
        threading.Thread(target=engine.loop, args=(_stop,), name="fwi", daemon=True),
        threading.Thread(target=smoke_engine.loop, args=(_stop,), name="smoke", daemon=True),
        threading.Thread(target=precip_engine.loop, args=(_stop,), name="precip", daemon=True),
        threading.Thread(target=fires.loop, args=(_stop, lambda: bus.publish({"type": "incidents"}), _check_fire_alerts),
                         name="firms", daemon=True),
        threading.Thread(target=wx_alerts.loop, args=(_stop, lambda: bus.publish({"type": "alerts"})),
                         name="alerts", daemon=True),
        threading.Thread(target=mail.loop, args=(_stop,), name="mailer", daemon=True),
        threading.Thread(target=wpc_fronts.loop, args=(_stop,), name="fronts", daemon=True),
        threading.Thread(target=front_watch.loop, name="frontwatch", daemon=True,
                         args=(_stop, lambda b: front_alerts.check(b) and bus.publish({"type": "frontalerts"}))),
        threading.Thread(target=mbwx.loop, args=(_stop,), name="mbwx", daemon=True),
    ]
    for t in threads:
        t.start()
    yield
    _stop.set()
    engine.wake.set()
    smoke_engine.wake.set()
    precip_engine.wake.set()
    ingestor.pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def revalidate_assets(request: Request, call_next):
    # The page and its JS/CSS change with updates; make browsers revalidate (ETag) each load.
    resp = await call_next(request)
    if request.url.path in ("/", "/fire") or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/meta")
def meta():
    h, w = grid.raster_shape()
    return {
        "bounds": config.BOUNDS, "width": w, "height": h,
        "hours": config.FORECAST_HOURS,
        "ptype_names": config.PTYPE_NAMES,
        "variables": {k: {"label": v["label"], "units": v["units"], "scale": v["scale"], "offset": v["offset"],
                          "index": k in config.INDICES, "smoke": k in config.SMOKE or k in ("pbl", "cape")}
                      for k, v in config.ALL_VARS.items() if k not in config.AUX_VARS},
    }


@app.post("/api/aoi")
async def save_areas(request: Request):
    """Replace the area list (Areas editor). Only from this machine or a Tailscale device; keeps a backup."""
    if request.client is None or not aoi.may_edit(request.client.host):
        raise HTTPException(403, "editing areas is only allowed from the server itself or a Tailscale device")
    try:
        doc = await request.json()
        saved = await asyncio.to_thread(aoi.save, doc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("areas of interest saved from %s: %d areas", request.client.host, len(saved["areas"]))
    await asyncio.to_thread(_check_fire_alerts)  # boundaries changed
    return saved


@app.get("/api/aoi/backups")
def area_backups(request: Request):
    return {"backups": aoi.backups(), "can_edit": request.client is not None and aoi.may_edit(request.client.host)}


@app.post("/api/aoi/restore/{name}")
async def restore_areas(name: str, request: Request):
    if request.client is None or not aoi.may_edit(request.client.host):
        raise HTTPException(403, "editing areas is only allowed from the server itself or a Tailscale device")
    try:
        restored = await asyncio.to_thread(aoi.restore, name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(_check_fire_alerts)
    return restored


@app.get("/api/aoi")
def areas_of_interest():
    # Re-read each time so edits to aoi.json show up without a restart.
    return json.loads(config.AOI_FILE.read_text(encoding="utf-8"))


@app.get("/api/runs")
def runs():
    return store.summary()


@app.get("/api/frame/{run}/{var}/{fh}")
def frame(run: str, var: str, fh: int):
    if var not in config.ALL_VARS or not run.isdigit():
        raise HTTPException(404)
    p = ingest.frame_path(run, var, fh)
    if not p.exists():
        raise HTTPException(404)
    # Stored gzipped; let the browser inflate it.
    return Response(p.read_bytes(), media_type="application/octet-stream",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=31536000, immutable"})


def _pixel_value(run, var, fh, idx):
    p = ingest.frame_path(run, var, fh)
    if not p.exists():
        return None
    v = grid.decode_pixel(p.read_bytes(), var, idx)
    return None if v is None else round(v, 2)


POINT_VARS = [*config.VARIABLES, "gust", "pbl", "cape", *config.SMOKE, *config.INDICES, *config.PRECIP, "cloud"]


@app.get("/api/point")
async def point(run: str, lat: float, lon: float):
    found = await asyncio.to_thread(grid.point_pixel, lat, lon)
    if found is None:
        raise HTTPException(400, "outside display area")
    idx, cell_lat, cell_lon = found
    summary = next((r for r in store.summary() if r["run"] == run), None)
    if not summary:
        raise HTTPException(404)
    hours = summary["hours"]
    t0 = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    loop = asyncio.get_running_loop()
    series = {}
    for var in POINT_VARS:
        series[var] = await asyncio.gather(
            *(loop.run_in_executor(_point_pool, _pixel_value, run, var, fh, idx) for fh in hours))
    return {"run": run, "lat": lat, "lon": lon, "cell_lat": round(cell_lat, 3), "cell_lon": round(cell_lon, 3),
            "hours": hours,
            "valid": [(t0 + timedelta(hours=fh)).isoformat() for fh in hours], "series": series}


@app.get("/api/report")
async def burn_report(lat: float, lon: float, run: str | None = None, product: str = "morning",
                      date: str | None = None, name: str | None = None, wind: float | None = None,
                      gust: float | None = None, rh_min: float | None = None, cape: float | None = None,
                      shift_deg: float | None = None):
    th = {"wind": wind, "gust": gust, "rh_min": rh_min, "cape": cape, "shift_deg": shift_deg}
    try:
        r = await asyncio.to_thread(report.build, store, lat, lon, run, product, date, th, name)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    try:  # fronts approaching the burn location (best-effort: the report stands without it)
        r["frontwatch"] = await asyncio.to_thread(front_watch.point, lat, lon)
    except Exception as e:
        log.warning("front watch for report unavailable: %s", e)
        r["frontwatch"] = None
    # add the alert region's own feed (battleboard) when the point is at a configured area
    area = wx_alerts.near(lat, lon)
    if area:
        have = {a["name"].lower() for a in r["alerts"]}
        r["alerts"] += [a for a in area["alerts"] if a["name"].lower() not in have]
    return r


@app.get("/api/mail")
def mail_reports():
    """Scheduled forecast emails: schedule and last result (recipient counts only, not addresses)."""
    return {"smtp": bool(config.SMTP_HOST and (config.SMTP_PASSWORD or not config.SMTP_USER)), "reports": mail.view()}


@app.post("/api/mail/{rid}/send")
async def mail_send_now(rid: str, request: Request):
    """Send one scheduled report now (test). Only from the machine running the server."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "only from the server itself")
    try:
        return {"result": await asyncio.to_thread(mail.send_now, rid)}
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"send failed: {e}")


@app.get("/api/mbwx/stations")
def manitoba_stations():
    """Manitoba Wildfire Service report stations with the date range stored for each."""
    return {"stations": mbwx.stations()}


@app.get("/api/mbwx/series")
def manitoba_series(station: str, start: str, end: str):
    """Daily observed and forecast noon weather + FWI codes for one station, start..end (YYYY-MM-DD)."""
    try:
        date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "start and end must be YYYY-MM-DD")
    return {"station": station, "days": mbwx.series(station, start, end)}


@app.get("/api/frontwatch")
async def front_watch_bases():
    """Front watch for every area of interest: nearest WPC front and its motion, RDPS frontal passages."""
    return {"bases": await asyncio.to_thread(front_watch.bases)}


@app.get("/api/frontalerts")
def front_alert_list():
    """Front watch alerts now in effect (from mailing_list.json front_alerts) and recently emailed events."""
    v = front_alerts.view()
    return {"active": v["active"], "recent": [{k: s[k] for k in ("id", "aoi", "event_time", "sent_at")} for s in v["recent"]]}


@app.get("/api/frontwatch/point")
async def front_watch_point(lat: float, lon: float):
    """Front watch at any point."""
    r = await asyncio.to_thread(front_watch.point, lat, lon)
    if r is None:
        raise HTTPException(404, "no complete model run with sea-level pressure yet, or point outside the domain")
    return r


@app.get("/api/daily")
async def daily_forecast(lat: float, lon: float):
    """7-day daily forecast (Open-Meteo) for the point panel."""
    try:
        return await asyncio.to_thread(openmeteo.daily, lat, lon)
    except Exception as e:
        raise HTTPException(502, f"Open-Meteo unavailable: {e}")


@app.get("/api/cwfis")
async def cwfis_layers():
    """Official CWFIS daily fire weather layers (NRCan WMS): date range and legend classes per layer."""
    return {"wms": cwfis.WMS, "layers": await asyncio.to_thread(cwfis.info)}


@app.get("/api/goes")
async def goes_times():
    """GOES-East satellite layers (GeoMet WMS) and their current time range."""
    return {"wms": goes.WMS, "layers": await asyncio.to_thread(goes.times)}


@app.get("/api/fronts")
def surface_fronts():
    """WPC analysed and day 1-3 forecast fronts and highs/lows (fronts.py)."""
    return wpc_fronts.view()


@app.get("/api/alerts")
def weather_alerts():
    return {"poll_min": config.ALERT_POLL_MIN, "areas": wx_alerts.view()}


class FireRequest(BaseModel):
    lat: float
    lon: float
    start: str                      # ISO 8601 with offset, e.g. 2026-10-22T11:00:00-05:00
    duration_h: float = Field(6, ge=0.5, le=24)
    run: str | None = None
    radius_km: float = Field(8, ge=2, le=25)
    res_m: float | None = Field(None, ge=30, le=250)
    curing: float | None = Field(None, ge=0, le=100)
    leaf: str = "auto"              # auto | leafless | green
    unclassified: str = "NF"        # NF | O1A | O1B
    gfl: float = Field(0.35, gt=0, le=5)
    ffmc: float | None = Field(None, ge=0, le=101)
    dmc: float | None = Field(None, ge=0)
    dc: float | None = Field(None, ge=0)


@app.post("/api/fire/simulate")
async def fire_simulate(req: FireRequest):
    run = req.run or next((r["run"] for r in store.summary() if len(r["hours"]) >= 49), None)
    if run is None:
        raise HTTPException(503, "no model run available yet")
    try:
        start = datetime.fromisoformat(req.start)
        if start.tzinfo is None:
            raise ValueError("start needs a UTC offset")
        start = start.astimezone(timezone.utc)
        if req.unclassified not in ("NF", "O1A", "O1B"):
            raise ValueError("unclassified must be NF, O1A or O1B")
        return await asyncio.to_thread(
            firemodel.simulate, store, run, req.lat, req.lon, start, req.duration_h, req.radius_km, req.res_m,
            req.curing, req.leaf, req.unclassified, req.gfl, req.ffmc, req.dmc, req.dc)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/firewx")
async def fire_weather_forecast(lat: float, lon: float, run: str | None = None,
                                radius_km: float = Query(10, ge=1, le=30), curing: float | None = Query(None, ge=0, le=100),
                                fbp: bool = False):
    try:
        return await asyncio.to_thread(firewx.build, store, lat, lon, run, 2, radius_km, curing, fbp)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/national")
async def national_summary():
    return await asyncio.to_thread(national.build)


@app.get("/api/hotspots")
def hotspot_detections(hours: int = Query(48, ge=1, le=120)):
    return {"status": fires.status, "detections": fires.detections_view(hours)}


def _check_fire_alerts():
    """After each FIRMS poll (and when areas change): log detections at the areas, then check the alerts."""
    dets = fires.snapshot()
    try:
        firelog.record(dets)
    except Exception:
        logging.getLogger("firelog").exception("fire log failed")
    try:
        fire_alerts.check(dets)
    except Exception:
        logging.getLogger("firealerts").exception("fire alert check failed")
    bus.publish({"type": "firealerts"})


@app.get("/api/firelog/{aoi_id}/episodes")
def fire_log_episodes(aoi_id: str, inside_only: bool = False):
    """Logged fire episodes at an area (post-fire review), newest first."""
    return {"aoi": aoi_id, "buffer_km": firelog.LOG_BUFFER_KM, "radius_km": firelog.LOG_RADIUS_KM,
            "episodes": firelog.episodes(aoi_id, inside_only)}


@app.get("/api/firelog/{aoi_id}/passes")
def fire_log_passes(aoi_id: str, start: str, end: str, inside_only: bool = False, include_low_conf: bool = True):
    """Logged detections at an area between start and end (ISO times), grouped into satellite passes."""
    return {"aoi": aoi_id, "passes": firelog.passes(aoi_id, start, end, inside_only, include_low_conf=include_low_conf)}


@app.get("/api/firelog/{aoi_id}/detections.csv")
def fire_log_csv(aoi_id: str, start: str = "", end: str = "9999"):
    """Every logged detection at an area as CSV (for GIS), optionally between start and end."""
    import csv, io
    db = firelog.connect()
    try:
        rows = db.execute("SELECT * FROM detections WHERE aoi = ? AND time >= ? AND time <= ? ORDER BY time",
                          (aoi_id, start, end)).fetchall()
    finally:
        db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["time", "lat", "lon", "frp", "sensor", "source", "confidence", "daynight", "footprint_m", "low_conf",
            "persistent", "inside", "dist_km", "id"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="fire-detections-{aoi_id}.csv"'})


@app.get("/api/firealerts")
def fire_alert_list():
    """Areas with satellite fire detections inside their boundary (last 24 h), and recently emailed alerts."""
    return fire_alerts.view()


@app.get("/api/incidents")
def incident_list():
    return {"status": fires.status, "base_km": config.INCIDENT_BASE_KM, "max_age_h": config.AUTO_SIM_MAX_AGE_H,
            "incidents": fires.incidents_view()}


@app.get("/api/incidents/{iid}/sim")
def incident_sim(iid: str):
    p = config.HOTSPOT_DIR / f"sim_{iid}.json"
    if not iid.isalnum() or not p.exists():
        raise HTTPException(404, "no simulation for this incident yet")
    return Response(p.read_bytes(), media_type="application/json")


@app.get("/api/incidents/{iid}/progression")
def incident_progression(iid: str):
    """An incident's satellite detections grouped by pass, for the progression view on the fire page."""
    try:
        return fires.progression(iid)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.post("/api/incidents/{iid}/simulate")
async def incident_rerun(iid: str):
    try:
        summary, err = await asyncio.to_thread(fires.run_incident, iid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    if err:
        raise HTTPException(400, err)
    bus.publish({"type": "incidents"})
    return summary


@app.get("/api/tz")
async def time_zone(lat: float, lon: float):
    tz = await asyncio.to_thread(report.local_zone, lat, lon)
    return {"timezone": str(tz)}


@app.get("/fire")
def fire_page():
    return FileResponse(config.WEB_DIR / "fire.html")


@app.get("/api/events")
async def events(request: Request):
    q = bus.subscribe()

    async def stream():
        try:
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
