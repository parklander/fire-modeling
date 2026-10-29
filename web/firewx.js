/* Daily fire weather forecast report: CWFIS daily grids + RDPS-derived daily values. */
(function () {
  "use strict";
  const form = document.getElementById("controls");
  const doc = document.getElementById("doc");
  const params = new URLSearchParams(location.search);
  let R = null, chart = null, AOIS = [], ALERTS = null;
  // forecast version for titles and file names: the RDPS run, e.g. "2026-10-26 12Z"
  const runTag = (r) => `${r.slice(0, 4)}-${r.slice(4, 6)}-${r.slice(6, 8)} ${r.slice(8, 10)}Z`;
  const slug = (s) => s.replace(/\W+/g, "-").replace(/^-|-$/g, "");

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const n = (v, d = 0) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const card = (d) => (d == null ? "" : COMPASS[Math.round(d / 22.5) % 16]);
  const dayLabel = (iso) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-CA", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });

  // CWFIS danger classes (upper bounds), as used on the weather map
  const CLASSES = {
    ffmc: [74, 84, 88, 91], dmc: [21, 27, 40, 60], dc: [80, 190, 300, 425],
    isi: [2, 5, 10, 15], fwi: [5, 10, 20, 30], bui: [20, 30, 40, 60, 90],
  };
  function cls(k, v) {
    if (v == null || !CLASSES[k]) return "";
    const b = CLASSES[k], i = b.findIndex((u) => v <= u), idx = i < 0 ? b.length : i;
    if (k === "bui") return ["c0", "c1b", "c1", "c2", "c3", "c4"][idx];
    return `c${idx}`;
  }
  const FDR_CLASS = { Low: "c0", Moderate: "c1", High: "c2", "Very High": "c3", Extreme: "c4" };
  // Manitoba fire danger rating from FWI: < 14 Low, < 24 Moderate, < 33 High, >= 33 Extreme
  const MB_BOUNDS = [14, 24, 33], MB_LABELS = ["Low", "Moderate", "High", "Extreme"], MB_CLS = ["c0", "c1", "c2", "c4"];
  const mbRating = (fwi) => {
    if (fwi == null) return null;
    const i = MB_BOUNDS.findIndex((b) => fwi < b), k = i < 0 ? MB_BOUNDS.length : i;
    return { label: MB_LABELS[k], cls: MB_CLS[k] };
  };
  // the Manitoba scale applies only in Manitoba; elsewhere the report uses CWFIS's rating
  const mbOk = () => !!R?.meta.in_manitoba;
  let wantScale = "mb";  // the reader's choice, kept while viewing places outside Manitoba
  const scale = () => (mbOk() ? form.elements.scale.value : "cwfis");
  function syncScale() {
    const sel = form.elements.scale;
    sel.querySelector('option[value="mb"]').disabled = !mbOk();
    sel.value = mbOk() ? wantScale : "cwfis";
    sel.title = mbOk() ? "" : "The Manitoba scale applies only to locations in Manitoba";
  }
  const noMb = (rows) => rows.filter((r) => mbOk() || r[3] !== "mb");
  // the rating shown in the calendar: Manitoba (from FWI) or CWFIS's own
  const dayRating = (c) => (scale() === "mb" ? mbRating(c.fwi) : c.fdr_label ? { label: c.fdr_label, cls: FDR_CLASS[c.fdr_label] } : null);

  // ---- rows: [label, getter(day) -> value, decimals, class key | formatter] ----
  const CW = [
    ["sect", "Weather at 1200 LST"],
    ["Temperature °C", (c) => c.temp, 1],
    ["Relative humidity %", (c) => c.rh, 0],
    ["Wind km/h", (c) => c.ws, 0, null, (c) => (c.ws == null ? "—" : `${card(c.wdir)} ${n(c.ws)}`)],
    ["Precipitation 24 h mm", (c) => c.precip, 1],
    ["sect", "FWI System (FWI1987)"],
    ["FFMC", (c) => c.ffmc, 1, "ffmc"], ["DMC", (c) => c.dmc, 1, "dmc"], ["DC", (c) => c.dc, 0, "dc"],
    ["ISI", (c) => c.isi, 1, "isi"], ["BUI", (c) => c.bui, 1, "bui"], ["FWI", (c) => c.fwi, 1, "fwi"],
    ["DSR", (c) => c.dsr, 2],
    ["sect", "Danger"],
    ["Fire danger, Manitoba (FWI)", (c) => c.fwi, 0, "mb", (c) => esc(mbRating(c.fwi)?.label || "—")],
    ["Fire danger, CWFIS rating", (c) => c.fdr, 0, "fdr", (c) => esc(c.fdr_label || "—")],
  ];
  const RD = [
    ["sect", "Weather at 1200 LST"],
    ["Temperature °C", (m) => m.temp, 1], ["Relative humidity %", (m) => m.rh, 0],
    ["Wind km/h", (m) => m.ws, 0, null, (m) => `${card(m.wdir)} ${n(m.ws)}`],
    ["Rain, noon to noon mm", (m) => m.rain24, 1, null, (m) => `${n(m.rain24, 1)}${m.rain24_partial ? "*" : ""}`],
    ["sect", "Daytime (local)"],
    ["Max temperature °C", (m) => m.max_temp, 1], ["Min RH %", (m) => m.min_rh, 0],
    ["Max wind / gust km/h", (m) => m.max_wind, 0, null, (m) => `${n(m.max_wind)} / ${n(m.max_gust)}`],
    ["Precipitation, calendar day mm", (m) => m.precip_day, 1, null, (m) => `${n(m.precip_day, 1)}${m.day_complete ? "" : "*"}`],
    ["Max mixing height m", (m) => m.max_mixing, 0], ["Peak ventilation m²/s", (m) => m.max_vi, 0],
    ["sect", "FWI System (FWI1987, daily from RDPS)"],
    ["FFMC", (m) => m.ffmc, 1, "ffmc"], ["DMC", (m) => m.dmc, 1, "dmc"], ["DC", (m) => m.dc, 0, "dc"],
    ["ISI", (m) => m.isi, 1, "isi"], ["BUI", (m) => m.bui, 1, "bui"], ["FWI", (m) => m.fwi, 1, "fwi"],
    ["DSR", (m) => m.dsr, 2],
    ["Fire danger, Manitoba (FWI)", (m) => m.fwi, 0, "mb", (m) => esc(mbRating(m.fwi)?.label || "—")],
    ["sect", "FWI2025 hourly (afternoon peak)"],
    ["Peak FFMC", (m) => m.ffmc2025_max, 1, "ffmc"], ["Peak ISI", (m) => m.isi2025_max, 1, "isi"],
    ["Peak FWI", (m) => m.fwi2025_max, 1, "fwi"],
  ];

  // ---- fire behaviour potential for the area's fuels ----
  const FD_LABEL = { S: "Surface", I: "Intermittent crown", C: "Crown" };
  const HFI_CLS = ["", "c0", "c1", "c2", "c3", "c4", "c5"];  // FBP intensity classes 1-6
  const shortFuel = (label) => label.replace(/ Mixedwood/, " Mixed").replace(/Leafless /, "").replace(/ \(.*\)/, "");
  function fbpTable(days, src) {
    const fuels = R.meta.area.fuels;
    const rows = [
      ["sect", "Worst case (highest intensity of the fuels present)"],
      ["Fuel", (w) => `<span class="fuel">${esc(shortFuel(w.label))}</span>`],
      ["Head ROS m/min", (w) => n(w.ros, 1)],
      ["Head fire intensity kW/m", (w) => n(w.hfi), (w) => HFI_CLS[w.hfi_class]],
      ["Intensity class", (w) => w.hfi_class, (w) => HFI_CLS[w.hfi_class]],
      ["Fire type", (w) => FD_LABEL[w.fd] || w.fd],
      ["Crown fraction burned %", (w) => n(w.cfb * 100)],
      ["sect", "By fuel type: head fire intensity kW/m (ROS m/min)"],
      ...fuels.map((f) => [`${shortFuel(f.label)} <small>${n(f.share * 100)}%</small>`, null, null, f.code]),
    ];
    const head = days.map((d) => `<th class="${d.kind}">${dayLabel(d.date)}</th>`).join("");
    const body = rows.map(([label, fmt, clsFn, code]) => {
      if (label === "sect") return `<tr class="sect"><td colspan="${days.length + 1}">${esc(fmt)}</td></tr>`;
      return `<tr><td>${label}</td>${days.map((d) => {
        const b = d[src];
        if (!b) return `<td class="${d.kind}">—</td>`;
        if (code != null) {
          const p = b.by_fuel[code];
          return p ? `<td class="${d.kind} ${HFI_CLS[p.hfi_class]}">${n(p.hfi)} <small>(${n(p.ros, 1)})</small></td>` : `<td class="${d.kind}">—</td>`;
        }
        return `<td class="${d.kind} ${clsFn ? clsFn(b.worst) : ""}">${fmt(b.worst)}</td>`;
      }).join("")}</tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="fx"><thead><tr><th>1200 LST</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function table(days, rows, pick) {
    const head = days.map((d) => `<th class="${d.kind}">${dayLabel(d.date)}${d.kind === "today" ? "<br><small>today</small>" : d.kind === "past" ? "<br><small>observed</small>" : ""}</th>`).join("");
    const body = rows.map((r) => {
      if (r[0] === "sect") return `<tr class="sect"><td colspan="${days.length + 1}">${esc(r[1])}</td></tr>`;
      const [label, get, dec, ck, fmt] = r;
      return `<tr><td>${esc(label)}</td>${days.map((d) => {
        const src = pick(d);
        if (!src) return `<td class="${d.kind}">—</td>`;
        const v = get(src);
        const c = ck === "fdr" ? FDR_CLASS[src.fdr_label] || "" : ck === "mb" ? mbRating(v)?.cls || "" : cls(ck, v);
        return `<td class="${d.kind} ${c}">${fmt ? fmt(src) : n(v, dec)}</td>`;
      }).join("")}</tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="fx"><thead><tr><th>1200 LST</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  // Calendar of the CWFIS days: Sunday-first weeks, each date in its weekday column.
  function calendar(days) {
    if (!days.length) return "";
    const byDate = new Map(days.map((d) => [d.date, d]));
    const utc = (iso) => new Date(iso + "T12:00:00Z");
    const iso = (dt) => dt.toISOString().slice(0, 10);
    const first = utc(days[0].date), last = utc(days[days.length - 1].date);
    const start = new Date(first); start.setUTCDate(first.getUTCDate() - first.getUTCDay());
    const end = new Date(last); end.setUTCDate(last.getUTCDate() + (6 - last.getUTCDay()));
    const cells = [];
    for (let dt = new Date(start); dt <= end; dt.setUTCDate(dt.getUTCDate() + 1)) {
      const d = byDate.get(iso(dt));
      const dayNum = dt.getUTCDate();
      const monthTag = dayNum === 1 || iso(dt) === days[0].date ? dt.toLocaleDateString("en-CA", { month: "short", timeZone: "UTC" }) + " " : "";
      if (!d) { cells.push(`<div class="cal-day empty"><div class="dn">${monthTag}${dayNum}</div></div>`); continue; }
      const c = d.cwfis, rt = dayRating(c);
      cells.push(`<div class="cal-day ${rt?.cls || ""} ${d.kind}">
        <div class="dn">${monthTag}${dayNum}${d.kind === "today" ? '<span class="tag-today">today</span>' : d.kind === "past" ? '<span class="tag-obs">obs</span>' : ""}</div>
        <div class="r">${esc(rt?.label || "—")}</div>
        <div class="s"><b>FWI ${n(c.fwi, 1)}</b> · ISI ${n(c.isi, 1)}</div>
        <div class="s">${card(c.wdir)} ${n(c.ws)} km/h</div>
        <div class="s">${n(c.precip, 1)} mm · ${n(c.temp, 0)}°C</div></div>`);
    }
    const head = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((w) => `<div class="cal-head">${w}</div>`).join("");
    return `<div class="cal">${head}${cells.join("")}</div>`;
  }

  // ECCC public alerts (server polls the battleboard feed or GeoMet for each area of interest)
  const alertTime = (iso) => new Date(iso).toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" });
  function alertList(area, full = true) {
    return `<div class="wx-alerts">${area.alerts.map((x) => `<div class="wx-alert lvl-${x.level}"><b>${esc(x.name)}</b>
      <small>${x.issued ? `issued ${alertTime(x.issued)}` : ""}</small>${full && x.text ? `<p>${esc(x.text.slice(0, 600))}${x.text.length > 600 ? "…" : ""}</p>` : ""}
      ${x.link || area.page ? `<small><a href="${esc(x.link || area.page)}" target="_blank" rel="noopener">weather.gc.ca ↗</a></small>` : ""}</div>`).join("")}</div>`;
  }
  async function fetchAlerts() {
    try { return await (await fetch("/api/alerts")).json(); } catch (_) { return null; }
  }
  function alertsSection() {
    const area = ALERTS?.areas.find((a) => a.id === form.elements.aoi.value);
    if (!area) return "";
    if (!area.alerts.length) return `<p class="wx-none">No ECCC weather alerts in effect${area.region ? ` for ${esc(area.region)}` : ""} (checked ${alertTime(area.checked)}).</p>`;
    return `<h3>ECCC alerts in effect</h3>${alertList(area)}`;
  }

  function render() {
    syncScale();
    const m = R.meta, cwDays = R.days.filter((d) => d.cwfis), rdDays = R.days.filter((d) => d.rdps);
    const name = form.elements.name.value;
    const runName = `${m.run.slice(8)}Z ${m.run.slice(0, 4)}-${m.run.slice(4, 6)}-${m.run.slice(6, 8)}`;
    const tiles = calendar(cwDays);
    let sec = 1;  // section 1 is the CWFIS table; later ones number themselves (fire behaviour is optional)
    doc.innerHTML = `
      <h1>Fire Weather Forecast</h1>
      <div class="sub">${esc(name || `${n(m.lat, 3)}°N, ${n(Math.abs(m.lon), 3)}°W`)}</div>
      <div class="meta">
        <div><span>Issued</span>${new Date(m.issued).toLocaleString("en-CA", { timeZone: m.timezone, dateStyle: "medium", timeStyle: "short" })}</div>
        <div><span>Location</span>${n(m.lat, 3)}°N, ${n(Math.abs(m.lon), 3)}°W</div>
        <div><span>Time zone</span>${esc(m.timezone)}</div>
        <div><span>CWFIS daily grids</span>through ${dayLabel(m.cwfis_last)}</div>
        <div><span>RDPS run</span>${runName}${m.run_complete ? "" : " (partial)"}</div>
        <div><span>RDPS FWI1987 start</span>${esc(m.seed.source)}, ${dayLabel(m.seed.date)}</div>
      </div>
      <div class="units">Daily values are for 1200 local standard time unless noted. Winds 10 m, km/h, direction the wind blows from.</div>
      ${alertsSection()}
      ${tiles}

      <h2><span class="num">1</span>CWFIS daily forecast <small class="sub">2 km grid cell, FWI1987</small></h2>
      ${table(cwDays, noMb(CW), (d) => d.cwfis)}

      ${!m.area.included ? "" : `
      <h2><span class="num">${++sec}</span>Fire behaviour potential <small class="sub">fuels within ${n(m.area.radius_km)} km, CWFIS daily values</small></h2>
      ${m.area.fuels.length ? fbpTable(cwDays.filter((d) => d.fbp_cwfis), "fbp_cwfis") + `
      <p class="methods">FBP System (cffdrs) for each fuel type covering at least 1% of the ${n(m.area.burnable_km2)} km² of burnable land within
        ${n(m.area.radius_km)} km of the location (CWFIS 30 m fuel grid; a circle, since training-area boundaries aren't available as open data),
        using each day's CWFIS 1200 LST FFMC, BUI and wind on flat ground. Grass curing follows the seasonal default
        (${n(cwDays.find((d) => d.fbp_cwfis)?.fbp_cwfis.worst.curing)}% now); aspen and mixedwood are ${esc(cwDays.find((d) => d.fbp_cwfis)?.fbp_cwfis.worst.leaf || "")}
        for the date. Worst case is the fuel with the highest head fire intensity, even if it covers only a small part of the area.</p>`
        : `<p class="methods">No burnable FBP fuel types found within ${n(m.area.radius_km)} km.</p>`}
      ${rdDays.some((d) => d.fbp_rdps) ? `<h3>Worst case from the RDPS values</h3>${fbpTable(rdDays.filter((d) => d.fbp_rdps), "fbp_rdps")}` : ""}`}

      <h2><span class="num">${++sec}</span>RDPS daily detail <small class="sub">10 km model, ${rdDays.length} days</small></h2>
      ${table(rdDays, noMb(RD), (d) => d.rdps)}
      <p class="methods">* Partial: less than 24 h of forecast precedes noon (the run starts after the previous noon), or the calendar day isn't fully covered by the run.</p>

      <h2><span class="num">${++sec}</span>Trend</h2>
      <div class="chart-box"><canvas id="trend"></canvas></div>

      <h3>Sources and methods</h3>
      <ul class="methods">
        <li>CWFIS: Canadian Wildland Fire Information System daily grids (cwfis.cfs.nrcan.gc.ca, WCS), sampled at the 2 km cell containing the location. Past dates are interpolated observations. From today on the series behaves as CWFIS's forecast; CWFIS doesn't mark the boundary, so this is inferred. (CWFIS's own fire behaviour for the single 2 km cell is in the CSV; section 2 uses all fuels in the area instead.)</li>
        <li>RDPS: ECCC Regional Deterministic Prediction System, nearest 10 km grid point (${n(m.grid_lat, 2)}, ${n(m.grid_lon, 2)}). FWI1987 codes are recomputed daily from the RDPS 1200 LST weather with the cffdrs equations, starting from the CWFIS codes for ${dayLabel(m.seed.date)} (FFMC ${n(m.seed.ffmc, 1)}, DMC ${n(m.seed.dmc, 1)}, DC ${n(m.seed.dc, 0)}).</li>
        <li>FWI2025 values are the hourly next-generation indices (cffdrs-ng) computed for the weather map; they are close to but not the same as FWI1987.</li>
        <li>${mbOk() ? `Fire danger, Manitoba: from FWI, Low &lt; 14, Moderate &lt; 24, High &lt; 33, Extreme ≥ 33. ` : ""}CWFIS rating: CWFIS's own fire danger class for the 2 km cell. The calendar shows the ${scale() === "mb" ? "Manitoba" : "CWFIS"} scale.${mbOk() ? "" : " (The Manitoba scale is shown only for locations in Manitoba.)"}</li>
        <li>Code and index cells are coloured by the CWFIS danger classes for each code or index.</li>
      </ul>
      <div class="disclaimer">Automated guidance from CWFIS and ECCC model output, for review by a qualified fire weather forecaster.</div>`;
    drawChart(cwDays, rdDays);
  }

  function drawChart(cwDays, rdDays) {
    if (chart) chart.destroy();
    const labels = R.days.map((d) => dayLabel(d.date));
    const at = (get) => R.days.map((d) => get(d) ?? null);
    chart = new Chart(document.getElementById("trend"), {
      type: "line",
      data: { labels, datasets: [
        { label: "FWI (CWFIS)", data: at((d) => d.cwfis?.fwi), borderColor: "#c2410c", backgroundColor: "#c2410c", borderWidth: 2, tension: 0.25 },
        { label: "FWI (RDPS, FWI1987)", data: at((d) => d.rdps?.fwi), borderColor: "#c2410c", backgroundColor: "#c2410c", borderDash: [6, 4], borderWidth: 2, tension: 0.25 },
        { label: "Peak FWI2025 (RDPS)", data: at((d) => d.rdps?.fwi2025_max), borderColor: "#e8935a", backgroundColor: "#e8935a", borderDash: [2, 3], borderWidth: 1.5, tension: 0.25 },
        { label: "BUI (CWFIS)", data: at((d) => d.cwfis?.bui), borderColor: "#2f6db5", backgroundColor: "#2f6db5", borderWidth: 1.5, tension: 0.25, yAxisID: "y1" },
        { label: "Precip mm (CWFIS)", type: "bar", data: at((d) => d.cwfis?.precip), backgroundColor: "rgba(63,174,85,.45)", yAxisID: "y2" },
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10, font: { size: 11 } } } },
        scales: {
          y: { min: 0, suggestedMax: 15, title: { display: true, text: "FWI" } },
          y1: { position: "right", min: 0, suggestedMax: 40, grid: { display: false }, title: { display: true, text: "BUI" } },
          y2: { display: false, min: 0, suggestedMax: 20 },
        },
      },
    });
  }

  // ---- CSV: one row per day, every value from both sources ----
  function csv() {
    if (!R) return;
    const cwKeys = ["temp", "rh", "ws", "wdir", "precip", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr", "ros", "hfi", "cfb", "ft_label", "tfc", "sfc", "fmc", "fdr", "fdr_label"];
    const rdKeys = ["temp", "rh", "ws", "wdir", "rain24", "rain24_partial", "max_temp", "min_rh", "max_wind", "max_gust", "precip_day",
      "max_mixing", "max_vi", "ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr", "ffmc2025_max", "isi2025_max", "fwi2025_max"];
    const fuels = R.meta.area.fuels, wKeys = ["label", "ros", "hfi", "hfi_class", "fd", "cfb"];
    const withFbp = R.meta.area.included;
    const mb = mbOk() ? (v) => [mbRating(v)?.label] : () => [];  // Manitoba-scale columns only in Manitoba
    const header = ["date", "kind", ...cwKeys.map((k) => `cwfis_${k}`), ...(mbOk() ? ["cwfis_mb_rating"] : []),
      ...rdKeys.map((k) => `rdps_${k}`), ...(mbOk() ? ["rdps_mb_rating"] : []),
      ...(withFbp ? [...wKeys.map((k) => `worst_cwfis_${k}`), ...wKeys.map((k) => `worst_rdps_${k}`),
        ...fuels.flatMap((f) => [`hfi_cwfis_${f.fuel}_${f.code}`, `ros_cwfis_${f.fuel}_${f.code}`])] : [])];
    const cell = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    const lines = [header.join(",")].concat(R.days.map((d) =>
      [d.date, d.kind, ...cwKeys.map((k) => d.cwfis?.[k]), ...mb(d.cwfis?.fwi),
        ...rdKeys.map((k) => d.rdps?.[k]), ...mb(d.rdps?.fwi),
        ...(withFbp ? [...wKeys.map((k) => d.fbp_cwfis?.worst[k]), ...wKeys.map((k) => d.fbp_rdps?.worst[k]),
          ...fuels.flatMap((f) => [d.fbp_cwfis?.by_fuel[f.code]?.hfi, d.fbp_cwfis?.by_fuel[f.code]?.ros])] : [])].map(cell).join(",")));
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(blob),
      download: `fire-weather-${slug(form.elements.name.value || "point")}-${runTag(R.meta.run).replace(" ", "-")}.csv`,
    });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---- load ----
  function applyAOI(id) {
    const a = AOIS.find((x) => x.id === id);
    if (!a) return false;
    Object.assign(form.elements.aoi, { value: id });
    form.elements.lat.value = a.lat; form.elements.lon.value = a.lon; form.elements.name.value = a.name;
    return true;
  }
  async function load() {
    const f = form.elements;
    if (!f.lat.value || !f.lon.value) { doc.innerHTML = `<p class="error">Choose an area or enter a latitude and longitude.</p>`; return; }
    const q = new URLSearchParams({ lat: f.lat.value, lon: f.lon.value, fbp: f.fbp.checked, radius_km: f.radius_km.value || 10 });
    if (f.curing.value !== "") q.set("curing", f.curing.value);
    history.replaceState(null, "", "?" + (f.aoi.value ? new URLSearchParams({ aoi: f.aoi.value }) : new URLSearchParams({ lat: f.lat.value, lon: f.lon.value, name: f.name.value })));
    doc.innerHTML = `<p class="loading">Building forecast… (CWFIS grids take a few seconds)</p>`;
    try {
      const [r, al] = await Promise.all([fetch(`/api/firewx?${q}`), fetchAlerts()]);
      ALERTS = al;
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      R = d;
      // the title is the browser's default file name for Print → Save as PDF
      document.title = `${f.name.value || "Fire weather"} – Fire Weather Forecast – ${runTag(R.meta.run)}`;
      render();
    } catch (e) {
      doc.innerHTML = `<p class="error">Couldn't build the forecast: ${esc(e.message)}</p>`;
    }
  }
  async function init() {
    try {
      const d = await (await fetch("/api/aoi")).json();
      AOIS = d.areas;
      form.elements.aoi.innerHTML = `<option value="">Custom point</option>` +
        AOIS.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} (${esc(a.prov)})</option>`).join("");
      if (params.get("lat")) {
        form.elements.lat.value = params.get("lat"); form.elements.lon.value = params.get("lon");
        form.elements.name.value = params.get("name") || "";
      } else {
        let saved = null;
        try { saved = localStorage.getItem("fw-aoi"); } catch (_) {}
        applyAOI(params.get("aoi") || (AOIS.some((a) => a.id === saved) ? saved : d.default));
      }
    } catch (_) {}
    load();
  }
  // saved per browser: danger scale and whether to include fire behaviour
  try {
    const prefs = JSON.parse(localStorage.getItem("fw-firewx") || "{}");
    if (prefs.scale) form.elements.scale.value = wantScale = prefs.scale;
    form.elements.fbp.checked = !!prefs.fbp;
  } catch (_) {}
  // ?fbp=1&scale=cwfis override the saved choices (used by the emailed PDF)
  if (params.has("fbp")) form.elements.fbp.checked = params.get("fbp") === "1";
  if (["mb", "cwfis"].includes(params.get("scale"))) form.elements.scale.value = wantScale = params.get("scale");
  const savePrefs = () => {
    try { localStorage.setItem("fw-firewx", JSON.stringify({ scale: form.elements.scale.value, fbp: form.elements.fbp.checked })); } catch (_) {}
  };
  const syncFbpOpts = () => document.querySelectorAll(".fbp-opt").forEach((el) => {
    el.classList.toggle("off", !form.elements.fbp.checked);
    el.querySelector("input").disabled = !form.elements.fbp.checked;
  });
  syncFbpOpts();
  form.elements.scale.addEventListener("change", () => { wantScale = form.elements.scale.value; savePrefs(); if (R) render(); });  // no refetch needed
  form.elements.fbp.addEventListener("change", () => { savePrefs(); syncFbpOpts(); load(); });
  form.elements.aoi.addEventListener("change", (e) => { if (applyAOI(e.target.value)) load(); });
  for (const k of ["lat", "lon"]) form.elements[k].addEventListener("input", () => { form.elements.aoi.value = ""; });
  form.addEventListener("submit", (e) => { e.preventDefault(); load(); });
  document.getElementById("csv").onclick = csv;
  document.getElementById("print").onclick = () => window.print();
  init();
})();
