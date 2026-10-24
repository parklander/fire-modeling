"""FastAPI app: serves the viewer, frames, point time series and live update events."""
import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import amqp_listener, config, firemodel, firewx, fwi_engine, grid, ingest, report, smoke

log = logging.getLogger("server")

bus = ingest.EventBus()
store = ingest.Store(bus)
ingestor = ingest.Ingestor(store)
engine = fwi_engine.FWIEngine(store)
smoke_engine = smoke.SmokeEngine(store)
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
    ]
    for t in threads:
        t.start()
    yield
    _stop.set()
    engine.wake.set()
    smoke_engine.wake.set()
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
        "variables": {k: {"label": v["label"], "units": v["units"], "scale": v["scale"], "offset": v["offset"],
                          "index": k in config.INDICES, "smoke": k in config.SMOKE or k in ("pbl", "cape")}
                      for k, v in config.ALL_VARS.items() if k not in config.AUX_VARS},
    }


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


POINT_VARS = [*config.VARIABLES, "gust", "pbl", "cape", *config.SMOKE, *config.INDICES]


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
        return await asyncio.to_thread(report.build, store, lat, lon, run, product, date, th, name)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))


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
