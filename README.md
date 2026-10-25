# RDPS Fire Weather Viewer

A live map viewer for ECCC's Regional Deterministic Prediction System (RDPS, 10 km, runs at 00/06/12/18Z, forecasts out to T+84h). It shows the fields used for fire weather:

| Layer | Datamart file | Units |
|---|---|---|
| Temperature (2 m) | `AirTemp_AGL-2m` | °C |
| Relative humidity (2 m) | `RelativeHumidity_AGL-2m` | % |
| Accumulated precip | `Precip-Accum_Sfc` | mm since run start |
| Wind speed (10 m) | `WindSpeed_AGL-10m` | km/h |
| Wind direction (10 m) | `WindDir_AGL-10m` | ° true, direction the wind blows from |

## Run

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m fireweather
```

Then open http://127.0.0.1:8000.

## How it works

- **AMQP trigger** (`fireweather/amqp_listener.py`): subscribes to `amqps://anonymous@dd.weather.gc.ca`, exchange `xpublic`, topic `v02.post.*.WXO-DD.model_rdps.10km.#`. Each notification names a newly published file. Notifications for the 5 fields above queue a download; everything else is ignored. The queue name is stable per machine and has a 3 h expiry, so notifications sent during a short restart are still delivered.
- **Reconcile** (`ingest.reconcile`): runs at startup and every 10 min. It scans the datamart over HTTP for the newest complete run, plus any newer run still arriving, and fills gaps. This handles backfill on first start and any missed messages.
- **Processing** (`fireweather/grid.py`): decodes each GRIB2 with eccodes. The rotated lat-lon grid is regridded once, by nearest neighbour, onto a Web Mercator raster over North America, and the lookup is cached in `data/grid_map.npz`. Each frame is then quantised to uint16 and gzipped to about 0.8 MB, so a full run is about 350 MB. The 2 newest complete runs are kept (`KEEP_RUNS`).
- **Viewer** (`web/`): Leaflet map with the field coloured in the browser and wind arrows. Hovering shows all 5 values. Clicking opens a point meteogram for the nearest native RDPS grid point. The browser gets pushed updates over Server-Sent Events: a banner shows the new run filling in, and the view switches to it once it's complete.

Keys: ← / → step hours, space plays/pauses.

## Areas of interest

Both the map and the report have an area-of-interest dropdown. It defaults to CFB Shilo RTA, and each browser remembers its last choice. The list lives in [fireweather/aoi.json](fireweather/aoi.json): the first `default` entry is the default, and edits show up on the next page load without a restart. The coordinates are approximate base/training-area points; replace them with range-control or burn-unit coordinates. Report links use `?aoi=<id>`, so a bookmarked report always follows that area.

## Burn weather reports

Click a location, then **Burn report ↗** in the point panel. You can also go straight to `/static/report.html?lat=49.79&lon=-99.63&name=CFB%20Shilo%20RTA`. The report follows a prescribed-burn met support plan:

| Product | Contents |
|---|---|
| 0800 Burn-day forecast | Current obs plus forecasts for 1000/1200/1500/1800 (items a–i), and a Day 2 / Day 3 outlook |
| 1200 Midday update | Current obs plus forecasts for 1500/1800 |
| 1500 Afternoon update | Forecast for 1800, overnight (min temp, RH recovery, winds, inversion, smoke settling) and the next day |
| Spot forecast | The next 6 hours at any point (e.g. a burn unit) |

Every product also includes:
- the FWI noon (1200 LST) report: nearby CWFIS stations alongside the model's FWI2025;
- amendment-criteria checks, with editable thresholds;
- ECCC alerts, including Special Air Quality Statements;
- AQHI;
- an hourly table;
- a box for forecaster remarks.

**Prescription (GO / NO-GO).** A panel above the report holds editable prescription limits, defaulting to the Annex A values and saved per area of interest. You can set:
- the ignition period;
- temperature, RH, wind, gusts and a wind-direction arc (which may wrap past north);
- the allowed stability classes;
- ventilation index (minimum and "rising");
- AQHI / no SAQS;
- FFMC, ISI, DMC, DC, BUI and FWI.

Most limits are checked every hour. DMC, DC and BUI use the 1200 LST value, and AQHI uses the day's maximum forecast. The report shows:
- a GO/NO-GO verdict for the ignition period, with the GO windows and the limiting parameters;
- an hour-by-hour grid (green within limits, red outside, — no data);
- the limits table with a burn-day result per parameter;
- GO windows for the next 3 days.

Changes re-evaluate instantly without refetching. An hour with missing data shows "?" rather than GO.

**Print / PDF** produces a clean letter-size copy. The provider, distribution and threshold settings are saved in the browser.

Sources:
- **RDPS burn fields** (`config.BURN_INPUTS`): 10 m gusts, boundary-layer (mixing) height, CAPE, 120 m temperature, surface pressure, and winds at 40/120 m and 950–700 hPa.
  - These are fetched hourly to T+48 and 3-hourly to T+84, which adds about 1.1 GB per run.
  - `smoke.py` derives stability, transport wind and ventilation index from them, then deletes the winds aloft.
- **Observations:** nearest MSC SWOB station within 100 km (`api.weather.gc.ca`).
- **AQHI and alerts:** MSC AQHI observations/forecasts and ECCC weather alerts (same API).
- **Station FWI:** CWFIS daily station file (noon observations and FWI1987 codes), joined to CWFIS station coordinates.

Limitations, which are also printed on the report:
- RDPS is deterministic, so precipitation is an amount, not a probability.
- No lightning data is used; CAPE is the convective proxy.
- Stability, ventilation rating and smoke-settling categories are rule-of-thumb classifications from model fields (see `report.METHODS`).
- The report is automated guidance for forecaster review.

## National weekly summary

Open `/static/national.html`, or **National ↗** in the weather map header. It's a one-page situational-awareness summary of every area in `aoi.json` for today through the last date CWFIS has published (about a week). It's built for a national reader such as the Canadian Forces Fire Marshal (CFFM). The page has:
- status counts and the highest FWI;
- key findings in words, e.g. "CFB Suffield: DC 425 or more Sun–Sat; Fire danger High Wed–Sat";
- a map of Canada with each base coloured by status;
- a base-by-day matrix grouped by province, with cells coloured by CWFIS fire danger rating and showing FWI plus noon wind and RH; BUI and DC are shown for today;
- the criteria, CSV and print.

Status for the period ([fireweather/national.py](fireweather/national.py) `CRITERIA`):
- **Concern:** CWFIS danger Very High or Extreme, or FWI 30 or more, on any day.
- **Watch:** danger High, FWI 20 or more, ISI 10 or more, BUI 60 or more, DC 425 or more, or wind 30 km/h or more with RH 30% or less.

Where a base's point falls in a cell with no data (e.g. water), the nearest CWFIS cell within 5 km is used and its distance shown. The first build makes about 1,500 CWFIS requests, taking about 15 s; results are cached for 3 hours.

## Fire weather forecast report

Open `/static/firewx.html?aoi=shilo`, or **Fire wx forecast ↗** from a point on the map or from the burn report. It's a printable daily outlook with a CSV export of every number (one row per day, `cwfis_*` and `rdps_*` columns). It draws on two independent sources ([fireweather/firewx.py](fireweather/firewx.py)):

- **CWFIS daily grids** (2 km): CWFIS's time-enabled WCS coverages run from 2018 to about 8 days ahead. The report shows the 2 past days and every date up to the last one CWFIS has published.
  - Values: 1200 LST temperature, RH, wind, 24 h precipitation; FFMC, DMC, DC, ISI, BUI, FWI, DSR; FBP head ROS, HFI, CFB, fire type, fuel consumption and foliar moisture for that cell's fuel; and the CWFIS fire danger rating.
  - Data quirks: wind direction is stored as −180…180° and normalised. Non-fuel cells carry −101 in the FBP grids and are shown as no data.
  - CWFIS doesn't mark which dates are observations and which are forecast; from today on is inferred to be forecast.
- **RDPS** (10 km, about 3.5 days):
  - FWI1987 recomputed daily from the RDPS 1200 LST weather with the cffdrs equations, starting from CWFIS's codes for the day before.
  - Daytime extremes, gusts, mixing height and ventilation.
  - The hourly FWI2025 afternoon peaks.

**Fire danger rating:** the calendar and tables show Manitoba's scale, applied to FWI (Low < 14, Moderate < 24, High < 33, Extreme ≥ 33), alongside CWFIS's own rating; a selector switches the calendar between the two.

**Fire behaviour potential** (optional; tick *Fire behaviour*): the report doesn't rely on CWFIS's single 2 km cell, which is often non-fuel at a base. Instead it takes every FBP fuel type covering at least 1% of the burnable land within a radius of the location (default 10 km; CWFIS 30 m fuel grid). It runs FBP for each fuel and each day, from both the CWFIS and the RDPS daily values. Terrain is assumed flat, grass curing follows the seasonal default (or a value you set), and aspen/mixedwood leaf state is set by date.
- The worst case (the highest head fire intensity) is headlined with its ROS, intensity class, fire type and CFB.
- A fuel-by-day table follows, and every value is in the CSV.
- Training-area boundaries aren't available as open data, so the area is a circle.

The two sources can disagree, sometimes a lot (e.g. rain amounts on a frontal day), and seeing both is the point. A trend chart overlays FWI from both, FWI2025 peaks, BUI and precipitation. Cells are tinted by CWFIS danger class.

## Satellite-detected fires (NASA FIRMS)

The server polls NASA FIRMS (LANCE near-real-time) every 15 minutes for VIIRS (SNPP, NOAA-20, NOAA-21) and MODIS detections over Canada. It turns them into incidents and runs fire growth automatically for any incident near a base ([fireweather/hotspots.py](fireweather/hotspots.py)):

1. **Persistent heat sources are excluded.** Once a day, 10 days of VIIRS detections are binned into ~375 m cells. Cells seen on 3 or more days, plus a 1 km buffer, are treated as industrial (refineries, oil sands, flares). That's typically about 45% of detections in October.
2. **Low-confidence detections are dropped.** The rest are clustered into incidents: detections within 1.5 km and 48 h of each other.
3. **Incidents within 50 km of an area in `aoi.json` are simulated.** The burned area at the latest pass is the detection footprints (VIIRS 375 m, MODIS about 1 km). Growth is forecast to 12 h past now with the latest complete RDPS run, as an established fire with no point-ignition build-up. A run is repeated when new detections join or a newer run is available. Detections on cropland or other non-fuel report "no burnable fuel" instead.
4. **On the fire page:** the "Detected fires" list shows each incident's distance and base, number of detections, last pass, fire radiative power and forecast area. Click one to play its simulation, or ↻ to re-run it. The map shows 48 h of detections coloured by age (grey = persistent and excluded).

Settings (sources, area, poll interval, clustering, base distance, forecast length) are `FIRMS_*` and related constants in [fireweather/config.py](fireweather/config.py).

**MAP_KEY:** FIRMS requires a key (free, 5,000 transactions per 10 minutes; a poll uses 4, the daily persistence pull about 6). Put it in a `.env` file in the project root. `.env` is git-ignored, so the key never reaches GitHub:

```
FIRMS_MAP_KEY=your-key-here
```

## Fire growth (`/fire`)

The fire growth page is a separate page on the same server, using the same RDPS data. Open it at http://127.0.0.1:8000/fire, or with **Fire growth ↗** from a point in the weather viewer. Pick an area or click the map to place an ignition, choose a start time and duration, and run. The page shows:
- hourly fire perimeters, or a head-fire-intensity class map;
- an hourly table: area, growth, perimeter, wind, FFMC/BUI/ISI, head ROS, max intensity and fire type;
- the inputs and assumptions used.

**Playback:** the timeline plays the fire's growth minute by minute (▶ or space; ← / → step 10 min) at 1 h per 3, 1.5 or 0.7 s.
- The burned area is coloured by arrival hour or intensity class, with cells ignited in the last 20 minutes highlighted as the active front.
- Completed hourly perimeters are drawn as lines and the current front as a white outline.
- The label shows clock time, elapsed hours, area, and that hour's wind, FFMC and ISI.
- Wind particles (or arrows) use the RDPS 10 m wind for the hour being played. They come from the same code as the weather viewer ([web/windlayer.js](web/windlayer.js)).

How it works ([fireweather/firemodel.py](fireweather/firemodel.py)):
- **Fuels:** CWFIS FBP Fuel Types 2024 (30 m), clipped by WCS and cached in `data/fuel_cache/`.
  - The CWFIS "D-1/D-2" and "M-1/M-2" classes become leafless (D-1/M-1) or green (D-2 = 20% of D-1, M-2) by date, or as you choose.
  - Unclassified land (−9999, mostly agriculture) is non-fuel by default and can be switched to grass.
- **Terrain:** CWFIS 250 m elevation (EPSG:3978), resampled to compute slope and aspect.
- **Weather and moisture codes:** the RDPS hourly forecast at the ignition, converted to FWI1987 inputs (hourly FFMC, plus DMC/DC/BUI updated at 1200 LST). These start from CWFIS FWI1987 codes. CFS advises that FWI2025 should not drive FBP1992, so the fire model uses FWI1987.
- **Fire behaviour:** [fireweather/fbp.py](fireweather/fbp.py) is a numpy port of cffdrs `fbp()`. It's evaluated once per unique fuel/slope/aspect combination for each hour, with point-ignition acceleration.
- **Growth:** minimum travel time on the grid with 32 spread directions. Each direction uses the FBP elliptical spread rate (head, back, length-to-breadth and net spread direction). Weather changes hourly.
  - On uniform grass, head travel is within 2–5% of FBP and area within about 5% after the first hour.
  - Spotting, suppression and sub-hourly wind changes are not modelled.

Validation against the cffdrs R package's own fixtures:

```
git clone https://github.com/cffdrs/cffdrs_r
.venv\Scripts\python -m fireweather.validate_fbp cffdrs_r
.venv\Scripts\python -m fireweather.validate_fwi87 cffdrs_r
```

All 42 FBP outputs on the 20 `test_fbp` rows match to 4 significant digits. Hourly FFMC, FFMC, DMC, DC and BUI match on about 14,900 fixture rows.

WISE (the successor to Prometheus) remains an option later for operational-grade perimeters. Its inputs (fuel grid, weather stream, ignition) are the same ones built here.

## FWI2025 hourly fire weather indices

`fireweather/cffdrs.py` is a vectorised numpy port of the hourly FWI2025 equations from [nrcan-cfs-fire/cffdrs-ng](https://github.com/nrcan-cfs-fire/cffdrs-ng) (`FWI/Python/NG_FWI.py` and `util.py`, version 2026-08-13, MPL-2.0). It computes FFMC, DMC, DC, ISI, BUI, FWI, DSR, GFMC, GSI and GFWI for every pixel each hour. The viewer shows FWI, ISI, FFMC, BUI, DMC, DC and Grass FWI, coloured by the CWFIS danger classes.

To check the port against the reference test stream:

```
git clone https://github.com/nrcan-cfs-fire/cffdrs-ng
.venv\Scripts\python -m fireweather.validate_cffdrs cffdrs-ng
```

All 19 output columns match `PRF2007_standard_hourly_FWI.csv` within its 4-decimal rounding over all 2,623 hours.

**Chaining** (`fireweather/fwi_engine.py`): the moisture codes carry memory, so each run starts from the state that the previous run produced at its T+6, saved in `data/fwi_state/`. States are also saved at T+12/18/24 to cover a missed run. The engine then steps T+1…T+84 as each hour's weather frames land, so FWI frames appear live alongside the weather.

**Cold start**: with no earlier state (first launch, or a gap of more than 24 h), FFMC/DMC/DC are seeded in three steps:
1. From the CWFIS `*_current` daily grids (WCS). Pixels are located with the GeoTIFF's own transform, because GeoServer returns the full coverage extent whatever bbox is requested.
2. Where the grids have no data, by inverse-distance weighting of the 4 nearest stations in the CWFIS daily station file, within `CWFIS_STATION_MAX_KM` (150 km).
3. Elsewhere (mostly the US, the oceans and the high Arctic), from the FWI2025 startup values (FFMC 85, DMC 6, DC 15). DMC and DC take weeks to months to spin up from these, so treat BUI/DMC/DC (and so FWI) as low-confidence there.

Where both exist, the grid and stations agree closely (median difference: DMC about 1, DC about 14).

**Assumptions** to be aware of:
- Hourly precip is the difference between consecutive `Precip-Accum` hours.
- Time of day uses local *solar* time (UTC offset = lon/15). This only affects the sunrise/sunset day/night switch for DMC/DC drying.
- Grass uses the NG_FWI defaults: fuel load 0.35 kg/m², Boreal Plains seasonal curing, matted before July 1 and standing after.
- Per CFS guidance, FWI2025 values should not be fed into the FBP System (FBP1992).

Settings (bounds, retention, worker count) are in `fireweather/config.py`.
