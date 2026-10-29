/* Burn-support forecast report: renders /api/report as a printable document. */
(function () {
  "use strict";
  const form = document.getElementById("controls");
  const doc = document.getElementById("doc");
  const params = new URLSearchParams(location.search);
  const SAVED = ["provider", "distribution", "wind", "gust", "rh_min", "cape", "shift_deg"];
  let remarks = "";
  let LAST = null;  // last report JSON, re-painted when the prescription changes

  // ---- form state: URL params for the location/run, localStorage for org settings ----
  try {
    const saved = JSON.parse(localStorage.getItem("fw-report") || "{}");
    for (const k of SAVED) if (saved[k] != null && form.elements[k]) form.elements[k].value = saved[k];
  } catch (_) {}
  for (const k of ["lat", "lon", "name", "product", "date"]) if (params.get(k)) form.elements[k].value = params.get(k);
  const run = params.get("run");

  function save() {
    try { localStorage.setItem("fw-report", JSON.stringify(Object.fromEntries(SAVED.map((k) => [k, form.elements[k].value])))); } catch (_) {}
  }

  // ---- formatting ---------------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const n = (v, d = 0) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
  const hhmm = (iso) => (iso ? iso.slice(11, 13) + iso.slice(14, 16) : "—");
  const tag = (t) => (t ? `<span class="tag ${esc(String(t).replace(/\s+/g, "-"))}">${esc(t)}</span>` : "—");
  let TZ = undefined;
  const day = (iso) => new Date(iso).toLocaleDateString("en-CA", { weekday: "long", month: "long", day: "numeric", timeZone: TZ });
  const dayShort = (iso) => new Date(iso.length === 10 ? iso + "T12:00:00Z" : iso).toLocaleDateString("en-CA", { weekday: "short", month: "short", day: "numeric", timeZone: iso.length === 10 ? "UTC" : TZ });
  const dt = (iso) => (iso ? `${new Date(iso).toLocaleString("en-CA", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ })}` : "—");
  const compass = (d) => (d == null ? "" : ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][Math.round(d / 22.5) % 16]);
  const ext = (e, d = 0, unit = "") => (e ? `${n(e.value, d)}${unit} <small>at ${hhmm(e.time)}</small>` : "—");
  const runName = (r) => `${r.slice(8, 10)}Z ${r.slice(0, 4)}-${r.slice(4, 6)}-${r.slice(6, 8)}`;

  // ---- sections -----------------------------------------------------------------------
  function header(R) {
    const m = R.meta, f = form.elements;
    return `
      <h1>Meteorological Support to Prescribed Burn Operations</h1>
      <div class="sub">${esc(m.name || "Forecast point")} — ${esc(m.title)}</div>
      <div class="meta">
        <div><span>Issued</span>${dt(m.issued)} ${esc(m.tz_abbr)}</div>
        <div><span>Burn date</span>${day(m.burn_date + "T12:00:00")}</div>
        <div><span>Model guidance</span>RDPS ${runName(m.run)}${m.run_complete ? "" : " (partial)"}</div>
        <div><span>Forecast point</span>${n(m.lat, 3)}°N, ${n(Math.abs(m.lon), 3)}°W <small>(grid ${n(m.grid_lat, 2)}, ${n(m.grid_lon, 2)})</small></div>
        <div><span>Provider</span>${esc(f.provider.value || "—")}</div>
        <div><span>Distribution</span>${esc(f.distribution.value || "—")}</div>
      </div>
      <div class="units">Times ${esc(m.tz_abbr)} (${esc(m.timezone)}). Winds 10 m open exposure, km/h; direction degrees true and cardinal.</div>`;
  }

  function banner(R) {
    const hits = R.triggers.filter((t) => t.hits.length);
    if (!hits.length) return `<div class="banner ok"><b>No amendment criteria forecast during the burn period.</b>Thresholds are listed in section 6.</div>`;
    return `<div class="banner warn"><b>Amendment criteria forecast during the burn period</b>${hits.map((t) => `${esc(t.label)}: ${t.hits.slice(0, 6).map(hhmm).join(", ")}${t.hits.length > 6 ? "…" : ""}`).join("<br>")}</div>`;
  }

  function burnDay(R) {
    const o = R.observation, cols = R.columns;
    const v = (c, f) => (c.values ? f(c.values) : "—");
    const obs = (f) => (o ? f(o) : "—");
    const rows = [
      ["a", "Temperature (°C)", (x) => n(x.temp, 1), obs((x) => n(x.temp, 1))],
      ["b", "Relative humidity (%)", (x) => n(x.rh), obs((x) => n(x.rh))],
      ["c", "Wind / gust (km/h)", (x) => `${n(x.wspd)} / ${n(x.gust)}`, obs((x) => `${n(x.wspd)} / ${n(x.gust)}`)],
      ["d", "Wind direction", (x) => `${n(x.wdir)}° ${esc(x.wdir_card || "")}`, obs((x) => `${n(x.wdir)}° ${compass(x.wdir)}`)],
      ["e", "Stability", (x) => tag(x.stability), "—"],
      ["f", "Mixing height (m AGL)", (x) => n(x.pbl, 0), "—"],
      ["f", "Transport wind (km/h)", (x) => (x.twspd == null ? "—" : `${n(x.twspd)} ${esc(x.twdir_card || "")}`), "—"],
      ["g", "Ventilation index (m²/s)", (x) => (x.vi == null ? "—" : `${n(x.vi)} ${tag(x.vi_rating)}`), "—"],
      ["h", "Precip, past hour (mm)", (x) => n(x.precip_1h, 1), obs((x) => n(x.precip_1h, 1))],
      ["h", "Thunderstorm risk (CAPE)", (x) => (x.cape == null ? "—" : `${tag(x.thunder)} <small>${n(x.cape)}</small>`), "—"],
      ["", "FFMC / ISI / FWI", (x) => `${n(x.ffmc, 1)} / ${n(x.isi, 1)} / ${n(x.fwi, 1)}`, "—"],
    ];
    const P = R.burn_period || {}, inv = R.inversion || {};
    const shifts = (R.wind_shifts || []);
    const aq = R.aqhi, aqAlerts = R.alerts.filter((a) => a.air_quality);
    const aqMax = aq && aq.forecast.length ? Math.max(...aq.forecast.map((f) => f.aqhi)) : null;
    const pts = [
      `<b>Wind shifts:</b> ${shifts.length ? shifts.map((s) => `${s.from_card} ${n(s.from_speed)} → ${s.to_card} ${n(s.to_speed)} km/h between ${hhmm(s.start)} and ${hhmm(s.end)} ${dayShort(s.start) !== dayShort(R.meta.burn_date + "T12:00:00") ? `(${dayShort(s.start)})` : ""}${s.frontal ? " — <b>frontal signature</b>" : ""} (Δ${n(Math.abs(((s.to_dir - s.from_dir + 540) % 360) - 180))}°)`).join("; ") : `no shifts over ${n(R.meta.thresholds.shift_deg)}° forecast through the burn period and the following night.`}`,
      `<b>Inversion:</b> ${inv.status === "breaks" ? `surface inversion forecast to break around <b>${hhmm(inv.time)}</b>.` : esc(inv.note || "unknown")}`,
      `<b>Mixing:</b> max mixing height ${ext(P.max_mixing_height, 0, " m")}; peak ventilation ${P.peak_vi ? `${n(P.peak_vi.value)} m²/s at ${hhmm(P.peak_vi.time)} ${tag(P.peak_vi.rating)}` : "—"}.`,
      `<b>Precipitation:</b> ${n(P.precip_total, 1)} mm forecast during the burn period (deterministic model; no probability available). Thunderstorm risk ${tag(P.thunder)}${P.max_cape ? ` <small>(max CAPE ${n(P.max_cape.value)} J/kg at ${hhmm(P.max_cape.time)})</small>` : ""}.`,
      `<b>Air quality:</b> ${aq ? `AQHI ${esc(aq.location)} ${n(aq.obs)} (${aqhiRisk(aq.obs)}) at ${hhmm(new Date(aq.obs_time).toLocaleString("sv-SE", { timeZone: TZ }).replace(" ", "T"))}; forecast max ${n(aqMax)} (${aqhiRisk(aqMax)}).` : "AQHI unavailable."} ${aqAlerts.length ? `<b>Active: ${aqAlerts.map((a) => esc(a.name)).join(", ")}</b>` : "No Special Air Quality Statement or smoke advisory in effect for this location."}`,
    ];
    return `
      <h2><span class="num">1</span>Burn-day forecast <small class="sub">${day(R.meta.burn_date + "T12:00:00")}</small></h2>
      <div class="table-wrap"><table>
        <thead><tr><th class="item"></th><th>Parameter</th><th class="obs">Current obs${o ? `<br><small>${esc(o.station)} (${n(o.distance_km)} km), ${hhmm(new Date(o.time).toLocaleString("sv-SE", { timeZone: TZ }).replace(" ", "T"))}</small>` : "<br><small>unavailable</small>"}</th>
          ${cols.map((c) => `<th>${esc(c.label)}${c.values ? "" : "<br><small>n/a</small>"}</th>`).join("")}</tr></thead>
        <tbody>${rows.map(([id, label, f, ob]) => `<tr><td class="item">${id}</td><td>${label}</td><td class="obs">${ob}</td>${cols.map((c) => `<td>${v(c, f)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table></div>
      <ul class="points">${pts.map((p) => `<li>${p}</li>`).join("")}</ul>
      <div class="remarks" contenteditable="true" id="remarks" data-placeholder="Forecaster remarks (prints with the report)…">${remarks}</div>`;
  }

  function aqhiRisk(v) {
    if (v == null) return "n/a";
    return v <= 3 ? "Low risk" : v <= 6 ? "Moderate risk" : v <= 10 ? "High risk" : "Very high risk";
  }

  function summaryTable(days) {
    const rows = [
      ["Max temperature", (d) => ext(d.max_temp, 1, " °C")],
      ["Min RH", (d) => ext(d.min_rh, 0, "%")],
      ["Max wind / gust", (d) => `${ext(d.max_wind, 0, " km/h")} / ${n(d.max_gust?.value)}`],
      ["Prevailing wind", (d) => esc(d.prevailing_dir || "—")],
      ["Max mixing height", (d) => ext(d.max_mixing_height, 0, " m")],
      ["Peak ventilation", (d) => (d.peak_vi ? `${n(d.peak_vi.value)} ${tag(d.peak_vi.rating)} <small>at ${hhmm(d.peak_vi.time)}</small>` : "—")],
      ["Precipitation", (d) => `${n(d.precip_total, 1)} mm`],
      ["Thunderstorm risk", (d) => tag(d.thunder)],
      ["Wind shifts / fronts", (d) => (d.wind_shifts?.length ? d.wind_shifts.map((s) => `${s.from_card}→${s.to_card} ${hhmm(s.start)}–${hhmm(s.end)}${s.frontal ? " <b>(front)</b>" : ""}`).join("<br>") : "None forecast")],
      ["Peak FWI / ISI", (d) => `${ext(d.max_fwi, 1)} / ${n(d.max_isi?.value, 1)}`],
    ];
    return `<div class="table-wrap"><table>
      <thead><tr><th>08–20 local</th>${days.map((d) => `<th>${dayShort(d.date)}${d.cadence_h === 3 ? "<br><small>3-hourly data</small>" : ""}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(([label, f]) => `<tr><td>${label}</td>${days.map((d) => `<td>${d.missing ? "—" : f(d)}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>`;
  }

  function outlook(R) {
    if (!R.outlook) return "";
    return `<h2><span class="num">2</span>Two-day outlook</h2>${summaryTable(R.outlook)}`;
  }

  function overnight(R) {
    if (!R.overnight) return "";
    const o = R.overnight;
    return `
      <h2><span class="num">4</span>Overnight and next day</h2>
      <div class="two">
        <table><tbody>
          <tr><td>Minimum temperature</td><td>${ext(o.min_temp, 1, " °C")}</td></tr>
          <tr><td>Maximum RH recovery</td><td>${ext(o.max_rh, 0, "%")}</td></tr>
          <tr><td>Overnight winds</td><td>${esc(o.prevailing_dir || "")} ${n(o.wind_range[0])}–${n(o.wind_range[1])} km/h</td></tr>
          <tr><td>Inversion</td><td>Stable ${n(o.stable_fraction * 100)}% of hours; min mixing height ${ext(o.min_mixing_height, 0, " m")}</td></tr>
          <tr><td>Smoke settling potential</td><td>${tag(o.smoke_settling)} <small>mean transport wind ${n(o.mean_transport_wind)} km/h</small></td></tr>
        </tbody></table>
        <div>${R.next_day ? summaryTable([R.next_day]) : ""}</div>
      </div>`;
  }

  function fwi(R) {
    const st = R.fwi.stations, model = R.fwi.model;
    const sRows = st && st.stations.length ? st.stations.map((s) => `<tr><td>${esc(s.station)} <small>${esc(s.agency || "")}, ${n(s.distance_km)} km</small></td><td>${s.date ? `${s.date.slice(0, 4)}-${s.date.slice(4, 6)}-${s.date.slice(6)}` : "—"}</td>
        <td>${n(s.temp, 1)}</td><td>${n(s.rh)}</td><td>${n(s.ws)}</td><td>${n(s.precip, 1)}</td>
        <td>${n(s.ffmc, 1)}</td><td>${n(s.dmc, 1)}</td><td>${n(s.dc, 1)}</td><td>${n(s.isi, 1)}</td><td>${n(s.bui, 1)}</td><td><b>${n(s.fwi, 1)}</b></td></tr>`).join("")
      : `<tr><td colspan="12">No CWFIS station within 120 km has reported${st ? ` (latest file ${esc(st.file_date)})` : " (CWFIS unavailable)"}.</td></tr>`;
    const mRows = model.map((m) => m.available ? `<tr><td>RDPS FWI2025 <small>forecast</small></td><td>${dayShort(m.date)}</td>
        <td>${n(m.temp, 1)}</td><td>${n(m.rh)}</td><td>${n(m.wspd)}</td><td>${n(m.rain24, 1)}${m.rain24_partial ? "<small>*</small>" : ""}</td>
        <td>${n(m.ffmc, 1)}</td><td>${n(m.dmc, 1)}</td><td>${n(m.dc, 1)}</td><td>${n(m.isi, 1)}</td><td>${n(m.bui, 1)}</td><td><b>${n(m.fwi, 1)}</b></td></tr>`
      : `<tr><td>RDPS FWI2025</td><td>${dayShort(m.date)}</td><td colspan="10">beyond this run</td></tr>`).join("");
    const partial = model.some((m) => m.rain24_partial);
    return `
      <h2><span class="num">5</span>Fire Weather Index report <small class="sub">noon LST (1200 LST)</small></h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Source</th><th>Date</th><th>Temp</th><th>RH</th><th>Wind</th><th>Rain 24h</th><th>FFMC</th><th>DMC</th><th>DC</th><th>ISI</th><th>BUI</th><th>FWI</th></tr></thead>
        <tbody>${sRows}${mRows}</tbody>
      </table></div>
      <p class="methods">Station rows: CWFIS daily noon observations and FWI1987 codes (cwfis.cfs.nrcan.gc.ca${st ? `, file ${esc(st.file_date)}` : ""}). Model rows: hourly FWI2025 (cffdrs-ng) at the forecast point, chained run to run from CWFIS-seeded startup values; ${partial ? "* rain since model start where under 24 h of forecast precedes noon; " : ""}compare trends rather than absolute values while the model chain spins up.</p>`;
  }

  function triggers(R) {
    return `
      <h2><span class="num">6</span>Amendment criteria <small class="sub">burn period 08–20 local, model guidance</small></h2>
      <table><thead><tr><th class="item"></th><th>Criterion</th><th>Forecast</th></tr></thead><tbody>
      ${R.triggers.map((t) => `<tr><td class="item">${t.id}</td><td>${esc(t.label)}</td><td>${t.hits.length ? `<span class="tag hit">Forecast</span> ${t.hits.slice(0, 8).map(hhmm).join(", ")}${t.hits.length > 8 ? "…" : ""}` : `<span class="tag clear">Not forecast</span>`}</td></tr>`).join("")}
      </tbody></table>
      ${alertsBlock(R)}`;
  }

  function alertsBlock(R) {
    if (!R.alerts.length) return `<p class="sub">No ECCC alerts in effect for this location.</p>`;
    return `<h3>Alerts in effect</h3>${R.alerts.map((a) => `<div class="alert-box ${a.air_quality ? "aq" : ""}"><b>${esc(a.name)}</b> <small>issued ${dt(a.issued)}, expires ${dt(a.expires)}${a.link ? ` · <a href="${esc(a.link)}" target="_blank" rel="noopener">source: weather.gc.ca ↗</a>` : ""}</small><pre>${esc(a.text.slice(0, 700))}${a.text.length > 700 ? "…" : ""}</pre></div>`).join("")}`;
  }

  function hourly(R) {
    if (!R.hourly.length) return "";
    return `
      <h2><span class="num">A</span>Hourly detail <small class="sub">${day(R.meta.burn_date + "T12:00:00")}</small></h2>
      <div class="table-wrap"><table class="hourly">
        <thead><tr><th>Time</th><th>T °C</th><th>RH %</th><th>Wind</th><th>Gust</th><th>Stability</th><th>Mix ht m</th><th>Transport</th><th>VI m²/s</th><th>Precip mm</th><th>CAPE</th><th>FFMC</th><th>ISI</th><th>FWI</th></tr></thead>
        <tbody>${R.hourly.map((h) => `<tr><td>${hhmm(h.local)}</td><td>${n(h.temp, 1)}</td><td>${n(h.rh)}</td><td>${esc(h.wdir_card || "")} ${n(h.wspd)}</td><td>${n(h.gust)}</td><td>${tag(h.stability)}</td><td>${n(h.pbl)}</td><td>${h.twspd == null ? "—" : `${esc(h.twdir_card || "")} ${n(h.twspd)}`}</td><td>${h.vi == null ? "—" : `${n(h.vi)} ${tag(h.vi_rating)}`}</td><td>${n(h.precip_1h, 1)}</td><td>${n(h.cape)}</td><td>${n(h.ffmc, 1)}</td><td>${n(h.isi, 1)}</td><td>${n(h.fwi, 1)}</td></tr>`).join("")}</tbody>
      </table></div>`;
  }

  function methods(R) {
    return `<h3>Methods and limitations</h3><ul class="methods">${R.methods.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>
      <div class="disclaimer">Automated guidance generated from ECCC RDPS model output, MSC observations and CWFIS products. It must be reviewed by a qualified forecaster before it is issued. Notify the Burn Boss immediately by phone or radio of any amendment criterion that is forecast or observed.</div>`;
  }

  // ---- prescription (GO / NO-GO) --------------------------------------------------------
  // Defaults follow the Annex A prescription limits. Blank limits ([__] in the annex) start
  // disabled. Saved per area of interest, since prescriptions are burn-block specific.
  const RX_DEFAULT = {
    start: 11, end: 17,
    temp: { on: true, min: 10, max: 24 },
    rh: { on: true, min: 20, max: 45 },
    wspd: { on: true, max: 22 },
    gust: { on: false, max: 30 },
    wdir: { on: false, from: 180, to: 270 },
    stability: { on: true, allowed: ["Slightly unstable"] },
    vi: { on: true, min: 55, unit: "x100", rising: true },
    aq: { on: true, max: 6, noSaqs: true },
    ffmc: { on: true, min: 77, max: 90 },
    isi: { on: true, min: 4, max: 7 },
    dmc: { on: true, max: 25 },
    dc: { on: true, max: 210 },
    bui: { on: true, max: 30 },
    fwi: { on: true, max: 12 },
  };
  const STABILITY = ["Stable", "Neutral", "Slightly unstable", "Unstable"];
  const VI_UNITS = { m2s: [1, "m²/s"], x100: [100, "×100 m²/s"] };
  const rxKey = () => `fw-rx:${form.elements.aoi.value || "custom"}`;
  function loadRx() {
    try {
      const s = JSON.parse(localStorage.getItem(rxKey()) || "null");
      if (s) return Object.fromEntries(Object.entries(RX_DEFAULT).map(([k, v]) => [k, typeof v === "object" ? { ...v, ...(s[k] || {}) } : (s[k] ?? v)]));
    } catch (_) {}
    return structuredClone(RX_DEFAULT);
  }
  let RX = null;
  const saveRx = () => { try { localStorage.setItem(rxKey(), JSON.stringify(RX)); } catch (_) {} };

  const inDir = (d, from, to) => (from <= to ? d >= from && d <= to : d >= from || d <= to);  // allows wrap past north
  const between = (v, lo, hi, strictHi) => (lo == null || v >= lo) && (hi == null || (strictHi ? v < hi : v <= hi));
  const hourOf = (h) => +h.local.slice(11, 13);

  // Each parameter: how to read it (hourly record or daily context), test it and describe the limit.
  const RX_ROWS = [
    { k: "temp", label: "Temperature", unit: "°C", assessed: "Hourly", get: (h) => h.temp, d: 1,
      test: (v, c) => between(v, c.min, c.max), limit: (c) => `${c.min}–${c.max} °C` },
    { k: "rh", label: "Relative humidity", unit: "%", assessed: "Hourly", get: (h) => h.rh, d: 0,
      test: (v, c) => between(v, c.min, c.max), limit: (c) => `${c.min}–${c.max} %` },
    { k: "wspd", label: "Wind speed", unit: "km/h", assessed: "Hourly", get: (h) => h.wspd, d: 0,
      test: (v, c) => v <= c.max, limit: (c) => `≤ ${c.max} km/h` },
    { k: "gust", label: "Wind gusts", unit: "km/h", assessed: "Hourly", get: (h) => h.gust, d: 0,
      test: (v, c) => v <= c.max, limit: (c) => `≤ ${c.max} km/h` },
    { k: "wdir", label: "Wind direction", unit: "°", assessed: "Hourly", get: (h) => h.wdir, d: 0,
      fmt: (v) => `${compass(v)} ${n(v)}`,
      test: (v, c) => inDir(v, c.from, c.to), limit: (c) => `${c.from}° to ${c.to}° (${compass(c.from)}–${compass(c.to)})` },
    { k: "stability", label: "Atmospheric stability", assessed: "Hourly", get: (h) => h.stability,
      fmt: (v) => ({ "Slightly unstable": "Sl. unst.", Unstable: "Unst.", Neutral: "Neut.", Stable: "Stab." })[v] || v,
      test: (v, c) => c.allowed.includes(v), limit: (c) => c.allowed.join(" or ") || "none allowed" },
    { k: "vi", label: "Ventilation index", assessed: "Hourly", get: (h, ctx) => h.vi, d: 0,
      fmt: (v, c) => n(v / VI_UNITS[c.unit][0], c.unit === "x100" ? 0 : 0),
      test: (v, c, h, ctx) => v >= c.min * VI_UNITS[c.unit][0] && (!c.rising || ctx.prevVi == null || v >= ctx.prevVi),
      limit: (c) => `≥ ${c.min} ${VI_UNITS[c.unit][1]}${c.rising ? " and rising" : ""}` },
    { k: "aq", label: "Air quality", assessed: "Daily", daily: true,
      get: (h, ctx) => ctx.day.aqhi_max, fmt: (v) => `AQHI ${n(v)}`,
      test: (v, c, h, ctx) => v <= c.max && (!c.noSaqs || !ctx.saqs),
      limit: (c) => `AQHI forecast ≤ ${c.max}${c.noSaqs ? ", no SAQS / smoke advisory in effect" : ""}` },
    { k: "ffmc", label: "FFMC", assessed: "Hourly (model)", get: (h) => h.ffmc, d: 1,
      test: (v, c) => between(v, c.min, c.max), limit: (c) => `${c.min}–${c.max}` },
    { k: "isi", label: "ISI", assessed: "Hourly (model)", get: (h) => h.isi, d: 1,
      test: (v, c) => between(v, c.min, c.max), limit: (c) => `${c.min}–${c.max}` },
    { k: "dmc", label: "DMC", assessed: "Daily (1200 LST)", daily: true, get: (h, ctx) => ctx.day.noon?.dmc, d: 1,
      test: (v, c) => v < c.max, limit: (c) => `< ${c.max}` },
    { k: "dc", label: "DC", assessed: "Daily (1200 LST)", daily: true, get: (h, ctx) => ctx.day.noon?.dc, d: 0,
      test: (v, c) => v < c.max, limit: (c) => `< ${c.max}` },
    { k: "bui", label: "BUI", assessed: "Daily (1200 LST)", daily: true, get: (h, ctx) => ctx.day.noon?.bui, d: 1,
      test: (v, c) => v < c.max, limit: (c) => `< ${c.max}` },
    { k: "fwi", label: "FWI", assessed: "Hourly (model)", get: (h) => h.fwi, d: 1,
      test: (v, c) => v < c.max, limit: (c) => `< ${c.max}` },
  ];

  /** -> [{hour, rec, inWindow, cells: {k: {v, ok}}, status: "GO"|"NO-GO"|"?"}] for one day */
  function evaluateDay(day, saqs) {
    const rows = RX_ROWS.filter((r) => RX[r.k].on);
    let prevVi = null;
    return day.hours.filter((h) => hourOf(h) >= 6 && hourOf(h) <= 21).map((h) => {
      const ctx = { day, saqs, prevVi };
      const cells = {};
      for (const r of rows) {
        const v = r.get(h, ctx);
        cells[r.k] = v == null ? { v: null, ok: null } : { v, ok: !!r.test(v, RX[r.k], h, ctx) };
      }
      if (h.vi != null) prevVi = h.vi;
      const vals = Object.values(cells);
      const status = vals.some((c) => c.ok === false) ? "NO-GO" : vals.some((c) => c.ok === null) ? "?" : "GO";
      const hr = hourOf(h);
      return { hour: hr, rec: h, inWindow: hr >= RX.start && hr <= RX.end, cells, status };
    });
  }

  function windows(evals) {
    const out = [];
    for (const e of evals.filter((x) => x.inWindow)) {
      const last = out[out.length - 1];
      if (e.status === "GO" && last && last.end === e.hour - 1) last.end = e.hour;
      else if (e.status === "GO") out.push({ start: e.hour, end: e.hour });
    }
    return out;
  }
  const hh = (h) => `${String(h).padStart(2, "0")}00`;
  const fmtWindows = (w) => w.map((x) => (x.start === x.end ? hh(x.start) : `${hh(x.start)}–${hh(x.end)}`)).join(", ");

  function limiting(evals) {
    const counts = {};
    for (const e of evals.filter((x) => x.inWindow)) {
      for (const [k, c] of Object.entries(e.cells)) if (c.ok === false) counts[k] = (counts[k] || 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1])
      .map(([k, c]) => `${RX_ROWS.find((r) => r.k === k).label} (${c} h)`);
  }

  function prescription(R) {
    if (!R.days?.length) return "";
    const saqs = R.alerts.some((a) => a.air_quality);
    const all = R.days.map((d) => ({ day: d, evals: evaluateDay(d, saqs) }));
    const today = all[0], evals = today.evals.filter((e) => e.hour >= 8 && e.hour <= 20);
    const win = windows(today.evals), lim = limiting(today.evals);
    const ign = today.evals.filter((e) => e.inWindow);
    const goCount = ign.filter((e) => e.status === "GO").length, unknown = ign.filter((e) => e.status === "?").length;
    const verdict = goCount ? "GO" : unknown && !ign.some((e) => e.status === "NO-GO") ? "?" : "NO-GO";
    const rows = RX_ROWS.filter((r) => RX[r.k].on);
    const cell = (r, e) => {
      const c = e.cells[r.k];
      if (!c || c.v == null) return `<td class="rx-na">—</td>`;
      const txt = r.fmt ? r.fmt(c.v, RX[r.k]) : n(c.v, r.d ?? 0);
      return `<td class="${c.ok ? "rx-ok" : "rx-bad"}">${esc(txt)}</td>`;
    };
    const dayResult = (r, list) => {
      const ev = list.filter((e) => e.inWindow);
      const bad = ev.filter((e) => e.cells[r.k]?.ok === false).map((e) => e.hour);
      const na = ev.every((e) => e.cells[r.k]?.ok == null);
      if (na) return `<span class="tag">no data</span>`;
      if (!bad.length) return `<span class="tag clear">Within limits</span>`;
      if (r.daily) return `<span class="tag hit">Outside limits</span>`;
      return `<span class="tag hit">Outside</span> ${fmtWindows(windows(ev.map((e) => ({ ...e, status: bad.includes(e.hour) ? "GO" : "NO-GO" }))))}`;
    };
    return `
      <h2><span class="num">Rx</span>Prescription — GO / NO-GO <small class="sub">${day(R.meta.burn_date + "T12:00:00")}, ignition period ${hh(RX.start)}–${hh(RX.end)}</small></h2>
      <div class="rx-verdict ${verdict === "GO" ? "go" : verdict === "?" ? "unk" : "nogo"}">
        <div class="rx-big">${verdict === "GO" ? "GO" : verdict === "?" ? "INCOMPLETE" : "NO-GO"}</div>
        <div>${goCount ? `<b>${goCount} of ${ign.length} ignition hours within prescription:</b> ${fmtWindows(win)}` : `<b>No ignition hour is fully within prescription.</b>`}
          ${lim.length ? `<br>Limiting: ${esc(lim.join(", "))}` : ""}
          ${unknown ? `<br><small>${unknown} hour(s) can't be assessed (missing model or AQHI data).</small>` : ""}</div>
      </div>
      <div class="table-wrap"><table class="rx-grid">
        <thead><tr><th>Parameter</th>${evals.map((e) => `<th class="${e.inWindow ? "ign" : ""}">${hh(e.hour)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows.map((r) => `<tr><td>${esc(r.label)}${r.daily ? " <small>daily</small>" : ""}</td>${evals.map((e) => cell(r, e)).join("")}</tr>`).join("")}
          <tr class="rx-status"><td>Status</td>${evals.map((e) => `<td class="${e.status === "GO" ? "go" : e.status === "?" ? "unk" : "nogo"} ${e.inWindow ? "ign" : ""}">${e.status}</td>`).join("")}</tr>
        </tbody>
      </table></div>
      <p class="methods">Shaded columns are the ignition period. Green = within limits, red = outside, — = not available. A GO hour meets every enabled limit.</p>
      <div class="two">
        <div>
          <h3>Prescription limits</h3>
          <table class="rx-limits"><thead><tr><th>Parameter</th><th>Limit</th><th>Assessed</th><th>Burn day</th></tr></thead><tbody>
            <tr><td>Ignition period</td><td>${hh(RX.start)}–${hh(RX.end)}</td><td>—</td><td></td></tr>
            ${rows.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.limit(RX[r.k]))}</td><td>${esc(r.assessed)}</td><td>${dayResult(r, today.evals)}</td></tr>`).join("")}
          </tbody></table>
        </div>
        <div>
          <h3>GO windows, next 3 days</h3>
          <table><thead><tr><th>Day</th><th>GO hours</th><th>Windows</th></tr></thead><tbody>
            ${all.map(({ day: d, evals: ev }) => {
              const ig = ev.filter((e) => e.inWindow), g = ig.filter((e) => e.status === "GO").length;
              const u = ig.filter((e) => e.status === "?").length;
              const lm = limiting(ev);
              return `<tr><td>${dayShort(d.date)}</td><td>${g} / ${ig.length}${u ? ` <small>(${u} n/a)</small>` : ""}</td>
                <td>${g ? fmtWindows(windows(ev)) : `<small>${esc(lm.slice(0, 2).join(", ") || "insufficient data")}</small>`}</td></tr>`;
            }).join("")}
          </tbody></table>
          <p class="methods">Days 2–3 use the same limits. Beyond 48 h, mixing height, ventilation and stability are 3-hourly, so other hours show as n/a. AQHI forecasts usually cover only the next ~36 h.</p>
        </div>
      </div>`;
  }

  // ---- prescription editor (not printed) ------------------------------------------------
  const rxEl = document.getElementById("rx-editor");
  function rxEditor() {
    const num = (k, f, step = 1) => `<input type="number" step="${step}" data-k="${k}" data-f="${f}" value="${RX[k][f] ?? ""}">`;
    const on = (k) => `<input type="checkbox" data-k="${k}" data-f="on" ${RX[k].on ? "checked" : ""}>`;
    const row = (k, label, body) => `<label class="rx-row ${RX[k].on ? "" : "off"}">${on(k)}<span class="rx-name">${label}</span>${body}</label>`;
    rxEl.querySelector(".rx-body").innerHTML = `
      <div class="rx-row"><span class="rx-name">Ignition period</span>
        <input type="number" min="0" max="23" data-k="start" value="${RX.start}">:00 to
        <input type="number" min="0" max="23" data-k="end" value="${RX.end}">:00</div>
      ${row("temp", "Temperature", `${num("temp", "min")} to ${num("temp", "max")} °C`)}
      ${row("rh", "Relative humidity", `${num("rh", "min")} to ${num("rh", "max")} %`)}
      ${row("wspd", "Wind speed", `≤ ${num("wspd", "max")} km/h`)}
      ${row("gust", "Wind gusts", `≤ ${num("gust", "max")} km/h`)}
      ${row("wdir", "Wind direction", `${num("wdir", "from", 5)}° to ${num("wdir", "to", 5)}° <small>(clockwise; wraps past N)</small>`)}
      ${row("stability", "Stability", STABILITY.map((s) => `<span class="rx-chk"><input type="checkbox" data-k="stability" data-class="${s}" ${RX.stability.allowed.includes(s) ? "checked" : ""}>${s}</span>`).join(""))}
      ${row("vi", "Ventilation index", `≥ ${num("vi", "min")} <select data-k="vi" data-f="unit">${Object.entries(VI_UNITS).map(([u, [, l]]) => `<option value="${u}" ${RX.vi.unit === u ? "selected" : ""}>${l}</option>`).join("")}</select>
        <span class="rx-chk"><input type="checkbox" data-k="vi" data-f="rising" ${RX.vi.rising ? "checked" : ""}>and rising</span>`)}
      ${row("aq", "Air quality", `AQHI ≤ ${num("aq", "max")} <span class="rx-chk"><input type="checkbox" data-k="aq" data-f="noSaqs" ${RX.aq.noSaqs ? "checked" : ""}>no SAQS / smoke advisory</span>`)}
      ${row("ffmc", "FFMC", `${num("ffmc", "min", 0.5)} to ${num("ffmc", "max", 0.5)}`)}
      ${row("isi", "ISI", `${num("isi", "min", 0.5)} to ${num("isi", "max", 0.5)}`)}
      ${row("dmc", "DMC", `< ${num("dmc", "max")}`)}
      ${row("dc", "DC", `< ${num("dc", "max", 5)}`)}
      ${row("bui", "BUI", `< ${num("bui", "max")}`)}
      ${row("fwi", "FWI", `< ${num("fwi", "max", 0.5)}`)}`;
  }
  rxEl.addEventListener("input", (e) => {
    const t = e.target, k = t.dataset.k;
    if (!k) return;
    if (k === "start" || k === "end") RX[k] = Math.max(0, Math.min(23, +t.value || 0));
    else if (t.dataset.class) {
      const a = new Set(RX.stability.allowed);
      t.checked ? a.add(t.dataset.class) : a.delete(t.dataset.class);
      RX.stability.allowed = STABILITY.filter((s) => a.has(s));
    } else if (t.type === "checkbox") RX[k][t.dataset.f] = t.checked;
    else if (t.tagName === "SELECT") RX[k][t.dataset.f] = t.value;
    else RX[k][t.dataset.f] = t.value === "" ? null : +t.value;
    if (t.dataset.f === "on") t.closest(".rx-row").classList.toggle("off", !t.checked);
    saveRx();
    paint();
  });
  document.getElementById("rx-reset").onclick = () => { RX = structuredClone(RX_DEFAULT); saveRx(); rxEditor(); paint(); };

  // ---- load ---------------------------------------------------------------------------
  async function load() {
    const f = form.elements;
    if (!f.lat.value || !f.lon.value) { doc.innerHTML = `<p class="error">Pick a location on the map, or enter a latitude and longitude.</p>`; return; }
    const q = new URLSearchParams();
    for (const k of ["lat", "lon", "product", "date", "name", "wind", "gust", "rh_min", "cape", "shift_deg"]) if (f[k].value) q.set(k, f[k].value);
    if (run) q.set("run", run);
    const aoi = f.aoi.value;
    const keep = aoi ? ["product", "date"] : ["lat", "lon", "name", "product", "date"];
    const url = [...q].filter(([k]) => keep.includes(k));
    if (aoi) url.unshift(["aoi", aoi]);
    if (run) url.push(["run", run]);
    history.replaceState(null, "", `?${new URLSearchParams(url)}`);
    document.getElementById("firewx-link").href = "/static/firewx.html?" + (aoi
      ? new URLSearchParams({ aoi }) : new URLSearchParams({ lat: f.lat.value, lon: f.lon.value, name: f.name.value }));
    const r0 = document.getElementById("remarks");
    if (r0) remarks = r0.innerHTML;
    doc.innerHTML = `<p class="loading">Building report…</p>`;
    let R;
    try {
      const r = await fetch(`/api/report?${q}`);
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      R = await r.json();
    } catch (e) {
      doc.innerHTML = `<p class="error">Couldn't build the report: ${esc(e.message)}</p>`;
      return;
    }
    TZ = R.meta.timezone;
    LAST = R;
    if (!f.date.value) f.date.value = R.meta.burn_date;
    for (const k of ["wind", "gust", "rh_min", "cape", "shift_deg"]) if (!f[k].value) f[k].value = R.meta.thresholds[k];
    const mr = R.meta.run;  // forecast version in the title, which is the default Save as PDF file name
    document.title = `${R.meta.name || "Burn weather"} – ${R.meta.title} – burn ${R.meta.burn_date} – RDPS ${mr.slice(0, 4)}-${mr.slice(4, 6)}-${mr.slice(6, 8)} ${mr.slice(8, 10)}Z`;
    paint();
  }

  function paint() {
    const R = LAST;
    if (!R) return;
    const r0 = document.getElementById("remarks");
    if (r0) remarks = r0.innerHTML;
    try {
      doc.innerHTML = header(R) + banner(R) + (R.frontwatch ? frontWatchHtml(R.frontwatch, { tz: TZ }) : "") + prescription(R) + burnDay(R) + outlook(R) + overnight(R) + fwi(R) +
        triggers(R) + hourly(R) + methods(R);
    } catch (e) {
      console.error(e);
      doc.innerHTML = `<p class="error">Couldn't render the report: ${esc(e.message)}</p>`;
    }
  }

  // ---- areas of interest --------------------------------------------------------------
  let AOIS = [];
  function applyAOI(id) {
    const a = AOIS.find((x) => x.id === id);
    if (!a) return false;
    form.elements.aoi.value = id;
    form.elements.lat.value = a.lat;
    form.elements.lon.value = a.lon;
    form.elements.name.value = a.name;
    return true;
  }
  form.elements.aoi.addEventListener("change", (e) => {
    if (applyAOI(e.target.value)) { RX = loadRx(); rxEditor(); load(); }
  });
  for (const k of ["lat", "lon"]) form.elements[k].addEventListener("input", () => { form.elements.aoi.value = ""; });

  async function init() {
    try {
      const d = await (await fetch("/api/aoi")).json();
      AOIS = d.areas;
      form.elements.aoi.innerHTML = `<option value="">Custom point</option>` +
        AOIS.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}${a.prov ? ` (${esc(a.prov)})` : ""}</option>`).join("");
      // an explicit lat/lon in the URL wins; otherwise the requested or default area
      if (params.get("aoi") || !params.get("lat")) applyAOI(params.get("aoi") || d.default);
    } catch (_) {}
    RX = loadRx();
    rxEditor();
    load();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); save(); load(); });
  form.elements.product.addEventListener("change", () => { save(); load(); });
  form.elements.provider.addEventListener("change", () => { save(); load(); });
  form.elements.distribution.addEventListener("change", () => { save(); load(); });
  document.getElementById("print").onclick = () => window.print();
  init();
})();
