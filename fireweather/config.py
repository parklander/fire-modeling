"""Settings for the RDPS fire-weather viewer."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal .env reader (KEY=value lines); real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
GRID_CACHE = DATA_DIR / "grid_map.npz"
WEB_DIR = ROOT / "web"
AOI_FILE = Path(__file__).resolve().parent / "aoi.json"  # areas of interest for the dropdowns

# MSC Datamart
DATAMART = "https://dd.weather.gc.ca"
AMQP_HOST = "dd.weather.gc.ca"
AMQP_PORT = 5671
AMQP_USER = AMQP_PASS = "anonymous"
AMQP_EXCHANGE = "xpublic"
# Routing keys look like v02.post.20261022.WXO-DD.model_rdps.10km.12.024
AMQP_TOPIC = "v02.post.*.WXO-DD.model_rdps.10km.#"
# Queue name must start with q_anonymous. x-expires keeps it alive across short restarts
# so messages published while we're down are still delivered.
AMQP_QUEUE_EXPIRE_MS = 3 * 3600 * 1000

RUN_HOURS = ("00", "06", "12", "18")
FORECAST_HOURS = list(range(0, 85))  # 000..084
KEEP_RUNS = 2  # complete runs kept on disk (plus any run still arriving)
DOWNLOAD_WORKERS = 6
RECONCILE_EVERY_S = 10 * 60

# Variables: datamart file token -> display/encoding info.
# Encoded as uint16: 0 = no data, else round((v - offset) * scale) + 1.
VARIABLES = {
    "temp": {
        "token": "AirTemp_AGL-2m",
        "label": "Temperature (2 m)", "units": "°C",
        "convert": lambda k: k - 273.15,
        "offset": -80.0, "scale": 10.0,
    },
    "rh": {
        "token": "RelativeHumidity_AGL-2m",
        "label": "Relative humidity (2 m)", "units": "%",
        "convert": None,
        "offset": 0.0, "scale": 10.0,
    },
    "precip": {
        "token": "Precip-Accum_Sfc",
        "label": "Accumulated precip", "units": "mm",
        "convert": None,  # kg m-2 == mm
        "offset": 0.0, "scale": 100.0,
    },
    "wspd": {
        "token": "WindSpeed_AGL-10m",
        "label": "Wind speed (10 m)", "units": "km/h",
        "convert": lambda ms: ms * 3.6,
        "offset": 0.0, "scale": 10.0,
    },
    "wdir": {
        "token": "WindDir_AGL-10m",
        "label": "Wind direction (10 m)", "units": "°",
        "convert": None,  # degrees true, meteorological (direction wind blows from)
        "offset": 0.0, "scale": 1.0,
    },
}

# Extra fields for burn / smoke-management reports. Hourly to T+48 (burn day and day 2),
# 3-hourly after (day-3 outlook) to keep downloads down. "aux" fields only feed derived
# products (smoke.py) and are deleted once those are computed.
BURN_HOURS = list(range(0, 49)) + list(range(51, 85, 3))
_ms_kmh = lambda ms: ms * 3.6
BURN_INPUTS = {
    "gust": {"token": "WindGust_AGL-10m", "label": "Wind gust (10 m)", "units": "km/h",
             "convert": _ms_kmh, "offset": 0.0, "scale": 10.0},
    "pbl": {"token": "PlanetaryBoundaryLayerHeight_Sfc", "label": "Mixing height", "units": "m AGL",
            "convert": None, "offset": 0.0, "scale": 1.0},
    # masked (missing) where no parcel is buoyant, i.e. no CAPE
    "cape": {"token": "CAPE_Sfc", "label": "Surface CAPE", "units": "J/kg",
             "convert": None, "offset": 0.0, "scale": 1.0, "missing_as": 0.0},
    "t120": {"token": "AirTemp_AGL-120m", "aux": True, "units": "°C",
             "convert": lambda k: k - 273.15, "offset": -80.0, "scale": 10.0},
    "psfc": {"token": "Pressure_Sfc", "aux": True, "units": "hPa",
             "convert": lambda pa: pa / 100.0, "offset": 0.0, "scale": 10.0},
}
# Winds aloft for the mixed-layer (transport) wind: AGL heights and pressure levels (hPa).
TRANSPORT_AGL = (40, 120)
TRANSPORT_HPA = (950, 925, 900, 850, 800, 750, 700)
for _h in TRANSPORT_AGL:
    BURN_INPUTS[f"ws_{_h}m"] = {"token": f"WindSpeed_AGL-{_h}m", "aux": True, "convert": _ms_kmh,
                                "offset": 0.0, "scale": 10.0}
    BURN_INPUTS[f"wd_{_h}m"] = {"token": f"WindDir_AGL-{_h}m", "aux": True, "convert": None,
                                "offset": 0.0, "scale": 1.0}
for _p in TRANSPORT_HPA:
    BURN_INPUTS[f"ws_{_p}"] = {"token": f"WindSpeed_IsbL-{_p:04d}", "aux": True, "convert": _ms_kmh,
                               "offset": 0.0, "scale": 10.0}
    BURN_INPUTS[f"wd_{_p}"] = {"token": f"WindDir_IsbL-{_p:04d}", "aux": True, "convert": None,
                               "offset": 0.0, "scale": 1.0}
AUX_VARS = {k for k, v in BURN_INPUTS.items() if v.get("aux")}

# Derived from the burn inputs (smoke.py)
SMOKE = {
    "lapse": {"label": "2–120 m temperature difference", "units": "°C", "offset": -30.0, "scale": 10.0},
    "twspd": {"label": "Transport wind speed", "units": "km/h", "offset": 0.0, "scale": 10.0},
    "twdir": {"label": "Transport wind direction", "units": "°", "offset": 0.0, "scale": 1.0},
    "vi": {"label": "Ventilation index", "units": "m²/s", "offset": 0.0, "scale": 0.5},
}
# Ventilation index ratings (BC Open Burning Smoke Control Regulation categories), m²/s
VI_POOR_BELOW = 2350
VI_GOOD_ABOVE = 4700

DOWNLOAD_VARS = {**VARIABLES, **BURN_INPUTS}
TOKEN_TO_VAR = {v["token"]: k for k, v in DOWNLOAD_VARS.items()}


def var_hours(var):
    """Forecast hours a downloaded variable is fetched for."""
    return BURN_HOURS if var in BURN_INPUTS else FORECAST_HOURS

# Hourly FWI2025 outputs computed from the fields above (see fwi_engine.py).
_idx = lambda label: {"label": label, "units": "", "offset": 0.0, "scale": 10.0}
INDICES = {
    "fwi": _idx("Fire Weather Index (FWI)"),
    "isi": _idx("Initial Spread Index (ISI)"),
    "ffmc": _idx("Fine Fuel Moisture Code (FFMC)"),
    "bui": _idx("Buildup Index (BUI)"),
    "dmc": _idx("Duff Moisture Code (DMC)"),
    "dc": _idx("Drought Code (DC)"),
    "gfwi": _idx("Grassland Fire Weather Index (GFWI)"),
}
ALL_VARS = {**VARIABLES, **BURN_INPUTS, **SMOKE, **INDICES}

FWI_STATE_DIR = DATA_DIR / "fwi_state"
# Forecast hours at which a run's moisture state is saved to start later runs from.
# Hour 6 is normally used (the next run's analysis time); later ones cover a missed run.
FWI_STATE_HOURS = (6, 12, 18, 24)
# Cold-start seed: CWFIS current (daily) FFMC/DMC/DC grids via WCS. Where these have no
# data, the FWI2025 startup defaults (FFMC 85, DMC 6, DC 15) are used.
CWFIS_WCS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/wcs"
CWFIS_SEED_RES = 0.1  # degrees
# Where the CWFIS grids have no data, seed from CWFIS station codes (IDW of the 4 nearest)
# within this distance; beyond it the startup defaults are used.
CWFIS_STATION_MAX_KM = 150

# NASA FIRMS active fire detections (LANCE NRT). The MAP_KEY comes from .env, never the repo.
FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT")
FIRMS_BBOX = (-141.0, 41.5, -52.0, 70.0)  # west, south, east, north: Canada (plus border strip)
FIRMS_POLL_MIN = 15                       # minutes between polls
FIRMS_DAYS = 2                            # day range per poll (catches overnight passes)
HOTSPOT_DIR = DATA_DIR / "hotspots"
# Persistent heat sources (refineries, flares, smelters): 375 m cells with detections on
# at least this many distinct days in the last 10 are excluded from incidents
# (the detected pixel shifts between passes, hence the buffer).
PERSISTENT_MIN_DAYS = 3
PERSISTENT_BUFFER_KM = 1.0  # detections this close to a persistent cell are also excluded
CLUSTER_KM = 1.5        # detections closer than this (and within CLUSTER_HOURS) form one incident
CLUSTER_HOURS = 48
INCIDENT_BASE_KM = 50   # auto-simulate incidents within this distance of an area of interest
AUTO_SIM_HOURS_AHEAD = 12
AUTO_SIM_MAX_AGE_H = 12  # only auto-simulate incidents detected within this many hours (older = likely out)

# Display raster: Web Mercator, clipped to North America.
BOUNDS = {"west": -170.0, "east": -50.0, "south": 24.0, "north": 83.0}
RASTER_WIDTH = 1100
