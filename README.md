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

**Areas editor** (`/static/aoi.html`, or **Edit areas…** at the bottom of the weather map's area dropdown; [web/aoi.js](web/aoi.js), [fireweather/aoi.py](fireweather/aoi.py)): a map on satellite or topographic imagery for maintaining the list. You can:
- add, delete, rename and reorder areas, and choose the default;
- drag each reference point, the one used for point forecasts, reports and alerts;
- set the ECCC alert feed and the Manitoba report stations;
- give each area a **boundary**: draw a box (click two opposite corners) or a polygon, edit its corners, or import one from a GeoJSON file in WGS84, which uses the largest polygon. The server stores it as `boundary` ([[lat, lon], ...]) and derives `bbox` [west, south, east, north].

Saving validates the list (unique IDs, points inside the map, a valid default) and writes `aoi.json`. The previous version is kept in `data/aoi_backups/` and can be restored from **Earlier versions**. Saving is only allowed from the server PC or a Tailscale device. Other devices on the local network can view but not edit. The weather map outlines the selected area's boundary. `aoi.json` is tracked in git, so boundaries are pushed to GitHub with everything else.

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

## Precipitation rate and type, synoptic overlay

**Precip rate** (weather map layer, [fireweather/precip.py](fireweather/precip.py)): each hour's rate is the difference between consecutive accumulated-precipitation frames, i.e. the amount in the hour ending at that time. The colours show the type, from RDPS's instantaneous precipitation type field (`PrecipType-Instant`). ECCC codes 1–5 are rain, rain/snow mix, freezing rain, ice pellets and snow; 6 means none. They were matched against the model's separate 1 h rain, snow, freezing rain and ice pellet amounts. Where the hour had precipitation but the type at the end of the hour is "none", the 2 m temperature decides: 0 °C or below is snow, under 2 °C is mix, otherwise rain. The hover readout and the point panel's bars show the type too.

**Synoptic** (header checkbox, [web/synoptic.js](web/synoptic.js), [fireweather/fronts.py](fireweather/fronts.py)) draws a surface chart over any layer:
- isobars every 4 hPa (thicker every 20) and H/L centres with their pressure, from RDPS mean sea-level pressure (`Pressure_MSL`), for every forecast hour;
- surface fronts from the NOAA Weather Prediction Center, in the standard symbols: the 3-hourly surface analysis (coded surface bulletin, positions to 0.1°) and the day 1–3 forecast fronts (National Forecast Chart service, days 2–3 valid 12 UTC; day 1 gives only a date). Fronts appear when the displayed time is within 3 h of a WPC valid time, and the legend says which set is showing or lists the times available.

Fronts are drawn by WPC forecasters from their own analysis, so they won't always sit exactly on an RDPS isobar kink. WPC's forecast fronts reach to about 56°N. `GET /api/fronts` returns the parsed data.

## Satellite layers (GOES-East)

The weather map's **Satellite (GOES-East)** group shows two ECCC GeoMet WMS layers, 1 km, a new image every 10 minutes with about 2 days kept ([fireweather/goes.py](fireweather/goes.py)):

- **Fire temperature** (`GOES-East_1km_FireTemperature-SWIR`): an RGB of shortwave-infrared bands by day, shortwave infrared at night. Active fires show as red (small or cooler) through orange and yellow to near-white (large, very hot) on a dark background. The fire channels are about 2 km per pixel, so **only large or hot fires show, as a dot a pixel or two across: zoom in**. For example, a ~110 MW fire in southern Oregon showed as one dull red pixel, and a 187 MW detection in Idaho didn't show at that hour. GOES's strength is timing (a new image every 10 minutes, between the polar passes that FIRMS uses); FIRMS VIIRS (375 m) catches smaller fires.
- **Smoke** (`GOES-East_1km_SmokeABIband1-SWIR`): the blue band by day, which smoke scatters strongly, so smoke shows as grey-white haze. Cloud is also bright but sharper-edged and textured. Smoke isn't visible at night.

**FIRMS hotspots** (the **Overlays** checkbox at the bottom of the layer list) shows the last 48 h of NASA FIRMS detections over any layer, satellite views included, above the wind and synoptic overlays. The styling matches the fire growth page: red under 6 h, orange under 24 h, yellow older, and small grey dots for persistent industrial heat sources, which are excluded from incidents. Hover a dot for its sensor, time and FRP. The overlay refreshes after each FIRMS poll.

The image follows the timeline: each hour shows the image from that time (rounded to the 10-minute grid). Forecast hours show the latest image, labelled as such. The timeline starts at the selected run's start, so earlier hours of the 2-day archive aren't reachable from the map. `GET /api/goes` gives the layers' current time ranges.

## Front watch

For every base, and any point clicked on the map ([fireweather/frontwatch.py](fireweather/frontwatch.py)), two independent views of approaching fronts:

- **WPC front motion:** the nearest analysed front within 800 km is compared with the analyses 3 and 6 h earlier. Past bulletins come from the Iowa Environmental Mesonet text archive, since WPC keeps only the latest. Points along the front are matched to the nearest point of an earlier front of a compatible kind (cold with cold, stationary or occluded, and so on). That measures motion across the front: the median displacement over the gap gives the speed, the mean direction gives the heading. The arrival time is the distance divided by the closing speed (the part of the motion toward the base). It's a straight-line extrapolation, given up to 36 h ahead.
- **RDPS frontal passages:** hours in the forecast at the point where the wind turns at least 45° (3 h mean before against 3 h after) together with a pressure trough (lowest of the ±3 h, then rising at least 0.8 hPa), a temperature drop of 3 °C or more, or an RH rise of 15% or more. A shift only counts with at least 15 km/h after it, or gusts of 30. It's "strong" at 30 km/h, or gusts of 50. Each one is reported with the wind before and after, the peak wind and gust over the next 6 h, and the temperature and RH change.

A base is on **watch** with a frontal wind shift in the next 48 h, and on **warning** when it's strong. This shows in the weather map's point panel, at the top of the burn report, and in the national summary's key findings. `GET /api/frontwatch` (all bases, recomputed with each run and analysis) and `GET /api/frontwatch/point?lat=&lon=` return the data.

**Front alerts** ([fireweather/frontalerts.py](fireweather/frontalerts.py)): set these up in `mailing_list.json` under `front_alerts`. Each entry gives the base, `within_hours`, `strength` (`strong` or `any`) and the recipients. When RDPS forecasts a qualifying shift at the base within that time, the server emails once per event. A shift that moves by up to 4 h between runs counts as the same event. The weather map shows active front alerts in the alert chip and panel, marked as a model forecast rather than an ECCC alert, and raises a desktop notification for new ones once **Notify me** is allowed. `GET /api/frontalerts` lists active and recently emailed alerts.

## Manitoba fire weather reports

The Manitoba Wildfire Service publishes daily observed and forecast noon weather and FWI codes for about 100 stations (Wildfire Service, Manitoba Agriculture and ECCC), back to at least 2008 ([fireweather/mbwx.py](fireweather/mbwx.py)):

- `https://www.gov.mb.ca/conservation_fire/Wx-Report/<YYYY>/<YYYYMMDD>-obs-wxrep.html` (observed)
- `…-fcst-wxrep.html` (forecast for that date)

Columns are TEMP, RH, WD, WS, RAIN, FFMC, DMC, DC, ISI, BUI, FWI and DSR. The server checks the last few days and tomorrow every hour and keeps every row in `data/mbwx.sqlite`, keyed by date, report type and station. Each forecast can then be compared with what was observed. Fill past seasons with:

```
.venv\Scripts\python -m fireweather.mbwx backfill 2016-04-01 2016-10-31
```

It skips days already stored and waits 0.5 s between requests. `GET /api/mbwx/stations` lists the stations with their date ranges. `GET /api/mbwx/series?station=SHILO AG&start=2026-04-01&end=2026-10-31` returns observed and forecast values by day. `mb_stations` in `aoi.json` names each area's stations, nearest first: CFB Shilo uses SHILO AG, then CARBERRY (WZE).

## Emailed forecast

The server can email the fire weather forecast on a schedule ([fireweather/mailer.py](fireweather/mailer.py)). Each email has:

- a subject line with today's danger, e.g. *Fire Weather Forecast – CFB Shilo RTA – Mon Oct 26: Low (FWI 12)*. It starts with ⚠ when ECCC alerts are in effect;
- a colour-coded daily table (danger, FWI, ISI, BUI, wind, RH, temperature, rain) and any alerts in effect;
- the full report as a PDF. The server prints it with a headless Edge or Chrome, or the browser set in `PDF_BROWSER`.

**Recipients and schedule** go in `mailing_list.json` in the project root. The file is git-ignored, so addresses never reach GitHub. Copy [mailing_list.example.json](mailing_list.example.json) to start. Each entry is one email: the area, local send time, days of the week, danger scale, an optional fire behaviour section, and `to` / `cc` / `bcc` lists. Use `bcc` for distribution lists so recipients don't see each other. Edits take effect within a minute.

**Mail server** settings go in `.env`:

```
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USER=fireweather@yourdomain.ca
SMTP_PASSWORD=...
MAIL_FROM=Fire Weather <fireweather@yourdomain.ca>
PUBLIC_URL=https://fireweather.yourdomain.ca   # optional: adds a "live report" link
```

Port 587 uses STARTTLS and port 465 uses SSL. Set `SMTP_SECURITY=none` for an internal relay. With Gmail or Microsoft 365, use an app password or a dedicated sending account, not a personal password.

Without `SMTP_HOST`, nothing is sent. Each email is written to `data/outbox/` as a `.eml` file instead, which you can open in Outlook or Thunderbird to check it. Each report goes out once a day at or after its time. If the server was down at that time, it still sends up to 6 h late. `GET /api/mail` shows the schedule and last result. `POST /api/mail/<id>/send`, run on the server machine, sends one now for testing.

## ECCC weather alerts

The server checks ECCC public alerts (warnings, watches, statements, including Special Air Quality Statements) for every area in `aoi.json` every 5 minutes ([fireweather/alerts.py](fireweather/alerts.py)):

- An area with an `alert_feed` code reads that ECCC alert region's battleboard feed, `https://weather.gc.ca/rss/battleboard/<code>_e.xml`. CFB Shilo uses `mbrm67` (R.M. of Cornwallis west of Shilo incl. Chater). To find a code, open the region on weather.gc.ca/warnings and take the code from its RSS link.
- Other areas use the MSC GeoMet `weather-alerts` API at the area's point.

New and ended alerts are written to the log, and the pages update over the live feed. The weather map has an **Alerts** chip in the header, showing how many bases have alerts. Its panel lists the selected base's alerts with links to weather.gc.ca and other bases with alerts. **Notify me** turns on desktop notifications for new alerts while the page is open. The fire weather forecast and the national summary list alerts in effect, and the burn report adds the battleboard alerts to its GeoMet ones. `GET /api/alerts` returns everything.

## Satellite-detected fires (NASA FIRMS)

The server polls NASA FIRMS (LANCE near-real-time) every 15 minutes for VIIRS (SNPP, NOAA-20, NOAA-21) and MODIS detections over Canada. It turns them into incidents and runs fire growth automatically for any incident near a base ([fireweather/hotspots.py](fireweather/hotspots.py)):

1. **Persistent heat sources are excluded.** Once a day, 10 days of VIIRS detections are binned into ~375 m cells. Cells seen on 3 or more days, plus a 1 km buffer, are treated as industrial (refineries, oil sands, flares). That's typically about 45% of detections in October.
2. **Low-confidence detections are dropped.** The rest are clustered into incidents: detections within 1.5 km and 48 h of each other.
3. **Incidents within 50 km of an area in `aoi.json` are simulated.** The burned area at the latest pass is the detection footprints (VIIRS 375 m, MODIS about 1 km). Growth is forecast to 12 h past now with the latest complete RDPS run, as an established fire with no point-ignition build-up. A run is repeated when new detections join or a newer run is available. Detections on cropland or other non-fuel report "no burnable fuel" instead.
4. **On the fire page:** the "Detected fires" list shows each incident's distance and base, number of detections, last pass, fire radiative power and forecast area. Click one to play its simulation, or ↻ to re-run it. The map shows 48 h of detections coloured by age (grey = persistent and excluded).
   **Satellite progression:** clicking an incident also opens its pass-by-pass history. Detections are grouped into satellite passes (same satellite, within 10 minutes), and each incident keeps its full detection history, so a fire lasting longer than the 2-day FIRMS window keeps its early passes (`GET /api/incidents/{id}/progression`). The panel shows a bar chart of total FRP per pass; click a bar or use the slider or ▶ to step through. The map draws each pass's detection pixels at their true size, with the selected pass outlined and earlier passes fading. Cards give detections and FRP for the pass, time since the previous pass, and the union of pixel area so far plus what's new. Pixel area overstates small fires, so read it as where heat has been seen, not a perimeter.

5. **Fire alerts for detections inside an area boundary** ([fireweather/firealerts.py](fireweather/firealerts.py)). After every poll, any detection from the last 24 h that falls inside an area's drawn boundary puts that area on alert. Persistent industrial sources are left out; low-confidence detections are shown but marked. Areas without a drawn boundary are skipped, so draw one in the area editor to cover them. The weather map shows a **🔥 Fire** alert chip, and the alert panel lists each area with its detection count, latest pass and max FRP. Click an area to zoom to it with the FIRMS overlay turned on. New detections raise a desktop notification once **Notify me** is allowed. To get email, add entries under `fire_alerts` in `mailing_list.json`, with `aois` (a list of area ids, or `"all"`), an optional `include_low_conf`, and the recipients. Each detection is emailed once, and a later pass with new detections sends a follow-up. `GET /api/firealerts` lists the areas on alert and recently emailed alerts.

6. **Fire log for post-fire review** ([fireweather/firelog.py](fireweather/firelog.py)). FIRMS only returns the last couple of days, so after every poll the server permanently logs each detection at an area of interest in `data/fire_log.sqlite`. That covers detections inside the drawn boundary or within 10 km of it; areas without a boundary log detections within 25 km of their point. Persistent and low-confidence detections are kept but flagged, along with whether each one is inside the boundary and its distance from the area. On the fire page, open **Fire log** under Detected fires to see the selected area's episodes, which are runs of detections no more than 2 days apart. Click one to replay it pass by pass in the progression player. You can tick **Inside the boundary only**, and **CSV ↓** exports every logged detection for GIS. The API is `GET /api/firelog/{area}/episodes`, `/passes?start=&end=` and `/detections.csv`. Live logging started on 2026-10-29. `data/` isn't in git, so back up `fire_log.sqlite` with the other data.

   **Backfilling past years** ([fireweather/firebackfill.py](fireweather/firebackfill.py)): this fills an area's log from the FIRMS archive:

   ```
   .venv\Scripts\python -m fireweather.firebackfill shilo
   ```

   It covers the area's boundary box plus 10 km, for each sensor:
   - **Landsat 8/9:** 30 m pixels, from June 2022. No FRP, but it catches small range fires the others miss.
   - **VIIRS S-NPP** from 2012, **NOAA-20** from 2018, **NOAA-21** from 2023.
   - **MODIS:** from 2000.

   The archived (standard-processing) data is used up to its latest date, then near-real-time data after that. Industrial heat sources are flagged using FIRMS's own `type` field in the archive. Requests cover 5 days each (the API maximum) and are throttled to stay under the MAP_KEY limit alongside the live poller. A full area takes about 4,000 requests, and progress is saved in `data/firelog_backfill_<area>.json`, so re-running resumes. `--from 2015-01-01` limits the years, and `--sources LANDSAT,VIIRS_SNPP` limits the sensors. Output goes to `logs/firelog-backfill.log`.

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
