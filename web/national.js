/* Weekly national fire weather summary across DND establishments (CWFIS daily grids). */
(function () {
  "use strict";
  const form = document.getElementById("controls");
  const doc = document.getElementById("doc");
  let R = null, map = null, ALERTS = null, FW = null;
  // version for titles and file names: issue time in central time, e.g. "2026-10-26 0937 CT"
  const issueTag = () => {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "America/Winnipeg", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(R.issued)).map((x) => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}${p.minute} CT`;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const n = (v, d = 0) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const card = (d) => (d == null ? "" : COMPASS[Math.round(d / 22.5) % 16]);
  const dayLabel = (iso, opts = { weekday: "short", month: "short", day: "numeric" }) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-CA", { ...opts, timeZone: "UTC" });
  const FDR_CLS = ["c0", "c1", "c2", "c3", "c4"];
  const PROV_NAME = { BC: "British Columbia", AB: "Alberta", SK: "Saskatchewan", MB: "Manitoba", ON: "Ontario", QC: "Quebec", NB: "New Brunswick", NS: "Nova Scotia", PE: "Prince Edward Island", NL: "Newfoundland and Labrador", YT: "Yukon", NT: "Northwest Territories", NU: "Nunavut" };
  const PROV_ORDER = ["BC", "YT", "NT", "AB", "SK", "MB", "NU", "ON", "QC", "NB", "PE", "NS", "NL"];

  try {
    const s = JSON.parse(localStorage.getItem("fw-national") || "{}");
    if (s.for) form.elements.for.value = s.for;
    if (s.by) form.elements.by.value = s.by;
  } catch (_) {}
  const save = () => { try { localStorage.setItem("fw-national", JSON.stringify({ for: form.elements.for.value, by: form.elements.by.value })); } catch (_) {} };

  // describe a base's flagged days in words, e.g. "Fire danger High Mon–Tue; BUI 60 or more Sun"
  function ranges(dates) {
    const out = [];
    for (const d of dates) {
      const last = out[out.length - 1], t = Date.parse(d + "T12:00:00Z");
      if (last && t - last.end === 864e5) last.end = t; else out.push({ start: t, end: t });
    }
    const w = (t) => new Date(t).toLocaleDateString("en-CA", { weekday: "short", timeZone: "UTC" });
    return out.map((r) => (r.start === r.end ? w(r.start) : `${w(r.start)}–${w(r.end)}`)).join(", ");
  }
  const critLabel = (k) => R.criteria.find((c) => c.key === k)?.label || k;

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
  function alertFindings() {
    if (!ALERTS) return "";
    const active = ALERTS.areas.filter((a) => a.alerts.length);
    const checked = ALERTS.areas[0]?.checked;
    if (!active.length) return `<p class="wx-none">No ECCC weather alerts in effect at any establishment${checked ? ` (checked ${alertTime(checked)})` : ""}.</p>`;
    return `<h3>ECCC alerts in effect now</h3>${active.map((a) => `<div><b>${esc(a.name)}</b> (${esc(a.prov)})${alertList(a, false)}</div>`).join("")}`;
  }

  // frontal wind shifts forecast at any establishment in the next 48 h (RDPS), with the WPC front's approach
  function frontFindings() {
    if (!FW) return "";
    const hit = FW.bases.filter((b) => b.level);
    if (!hit.length) return `<p class="wx-none">No frontal wind shift forecast at any establishment in the next 48 h (RDPS).</p>`;
    return `<h3>Frontal wind shifts, next 48 h</h3>${hit.sort((a, b) => (a.level === "warning" ? -1 : 1) - (b.level === "warning" ? -1 : 1))
      .map((b) => `<div class="fw-base"><b>${esc(b.name)}</b> (${esc(b.prov)})${frontWatchHtml(b, { compact: true })}</div>`).join("")}`;
  }

  function findings() {
    const flagged = R.bases.filter((b) => b.severity > 0).sort((a, b) => b.severity - a.severity || (b.max_fwi ?? 0) - (a.max_fwi ?? 0));
    if (!flagged.length) return `<p>No base meets any concern criterion this period. Fire weather is at or below Moderate everywhere.</p>`;
    return `<ul class="findings">${flagged.map((b) => `<li><span class="status ${b.status}">${b.status}</span>
      <b>${esc(b.name)}</b> (${esc(b.prov)}): ${Object.entries(b.flags).map(([k, ds]) => `${esc(critLabel(k))} ${ranges(ds)}`).join("; ")}.
      Peak FWI ${n(b.max_fwi, 1)} on ${dayLabel(b.max_fwi_date, { weekday: "short" })}.</li>`).join("")}</ul>`;
  }

  function matrix() {
    const head = R.dates.map((d) => `<th>${dayLabel(d, { weekday: "short" })}<br><small>${dayLabel(d, { month: "short", day: "numeric" })}</small></th>`).join("");
    const byProv = {};
    R.bases.forEach((b) => (byProv[b.prov] = byProv[b.prov] || []).push(b));
    const provs = Object.keys(byProv).sort((a, b) => (PROV_ORDER.indexOf(a) - PROV_ORDER.indexOf(b)));
    const rows = provs.map((p) => `<tr class="prov"><td colspan="${R.dates.length + 4}">${esc(PROV_NAME[p] || p)}</td></tr>` +
      byProv[p].map((b) => {
        const last = b.days[b.days.length - 1], first = b.days[0];
        return `<tr><td>${esc(b.name)}${b.cell_km > 0 ? ` <small title="Nearest CWFIS cell with data">(${n(b.cell_km, 1)} km)</small>` : ""}</td>
          <td><span class="status ${b.status}">${b.status}</span></td>
          ${b.days.map((d) => d.missing ? `<td class="day cna">—</td>` : `<td class="day ${FDR_CLS[d.fdr] ?? "cna"}" title="${esc(d.fdr_label || "")}: FWI ${n(d.fwi, 1)}, ISI ${n(d.isi, 1)}, BUI ${n(d.bui, 0)}, ${n(d.temp, 0)}°C, RH ${n(d.rh)}%, wind ${card(d.wdir)} ${n(d.ws)} km/h, ${n(d.precip, 1)} mm${d.flags.length ? "\n" + d.flags.map(critLabel).join("\n") : ""}">
            ${n(d.fwi, 0)}${d.flags.length ? '<span class="fl">*</span>' : ""}<small>${card(d.wdir)} ${n(d.ws)} · ${n(d.rh)}%</small></td>`).join("")}
          <td>${n(first.bui, 0)}</td><td>${n(first.dc, 0)}</td></tr>`;
      }).join("")).join("");
    return `<div class="table-wrap"><table class="mx"><thead><tr><th>Establishment</th><th>Status</th>${head}<th>BUI<br><small>today</small></th><th>DC<br><small>today</small></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="legend-row">Cell colour = CWFIS fire danger rating:
        ${["Low", "Moderate", "High", "Very High", "Extreme"].map((l, i) => `<span><i class="${FDR_CLS[i]}"></i> ${l}</span>`).join("")}
        · number = FWI · <span class="fl" style="color:var(--bad);font-weight:700">*</span> = a concern criterion met · small text = noon wind (km/h) and RH</div>`;
  }

  function render() {
    const c = R.counts, peak = R.bases.reduce((a, b) => ((b.max_fwi ?? -1) > (a?.max_fwi ?? -1) ? b : a), null);
    const period = `${dayLabel(R.dates[0])} – ${dayLabel(R.dates[R.dates.length - 1])}`;
    doc.innerHTML = `
      <h1>National Fire Weather Summary</h1>
      <div class="sub">DND establishments · ${period}</div>
      <div class="meta">
        <div><span>Issued</span>${new Date(R.issued).toLocaleString("en-CA", { timeZone: "America/Winnipeg", dateStyle: "medium", timeStyle: "short" })} CT</div>
        <div><span>Prepared for</span>${esc(form.elements.for.value || "—")}</div>
        <div><span>Prepared by</span>${esc(form.elements.by.value || "—")}</div>
        <div><span>Period</span>${period}</div>
        <div><span>Source</span>CWFIS daily grids (2 km), FWI1987</div>
        <div><span>Establishments</span>${R.bases.length}</div>
      </div>
      <div class="cards">
        <div class="card concern"><div class="v">${c.Concern}</div><div class="k">Concern</div></div>
        <div class="card watch"><div class="v">${c.Watch}</div><div class="k">Watch</div></div>
        <div class="card normal"><div class="v">${c.Normal}</div><div class="k">Normal</div></div>
        <div class="card"><div class="v">${n(peak?.max_fwi, 1)}</div><div class="k">Highest FWI: ${esc(peak?.name || "—")}, ${peak?.max_fwi_date ? dayLabel(peak.max_fwi_date, { weekday: "short" }) : ""}</div></div>
      </div>

      <h2><span class="num">1</span>Key findings</h2>
      ${findings()}
      ${alertFindings()}
      ${frontFindings()}

      <h2><span class="num">2</span>Overview map <small class="sub">status for the period</small></h2>
      <div id="map"></div>

      <h2><span class="num">3</span>Daily outlook by establishment</h2>
      ${matrix()}

      <h3>Status criteria</h3>
      <table class="rx-limits"><thead><tr><th>Criterion (any day in the period)</th><th>Status</th></tr></thead><tbody>
        ${R.criteria.map((k) => `<tr><td>${esc(k.label)}</td><td><span class="status ${k.severity}">${k.severity}</span></td></tr>`).join("")}
      </tbody></table>

      <h3>Sources and methods</h3>
      <ul class="methods">
        <li>CWFIS (Canadian Wildland Fire Information System) daily FWI1987 grids at 2 km, sampled at each establishment's reference point. Values are for 1200 local standard time. Dates from today on are CWFIS's forecast (inferred; CWFIS doesn't mark the boundary). The period runs to the last date CWFIS has published${R.cwfis_last ? ` (${dayLabel(R.cwfis_last)})` : ""}.</li>
        <li>Fire danger rating is CWFIS's national class (Low to Extreme). Provincial agencies use their own scales, so local ratings can differ.</li>
        <li>Thresholds for FWI, ISI and BUI follow the CWFIS map class boundaries. Critical fire weather is wind of 30 km/h or more with RH of 30% or less at noon.</li>
        <li>Reference points are approximate base locations, not training-area boundaries. Where a point's own cell has no data (e.g. water), the nearest CWFIS cell with data within 5 km is used and its distance shown.</li>
        <li>Daily values at a point; they don't capture conditions across a whole training area, overnight burning or local effects.</li>
      </ul>
      <div class="disclaimer">Automated summary of CWFIS products for situational awareness. Confirm conditions with the provincial or territorial fire management agency and a qualified fire weather forecaster before acting on them.</div>`;
    drawMap();
  }

  function drawMap() {
    if (map) { map.remove(); map = null; }
    map = L.map("map", { zoomSnap: 0.25, scrollWheelZoom: false, attributionControl: true });
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { attribution: "Tiles &copy; Esri | Data: CWFIS", maxNativeZoom: 16 }).addTo(map);
    map.fitBounds(L.latLngBounds(R.bases.map((b) => [b.lat, b.lon])).pad(0.08));
    // Declutter labels: flagged bases claim space first; a label that would overlap one already
    // placed shows on hover instead. Labels near the right edge go on the left of the pin.
    const placed = [], width = map.getSize().x;
    const order = [...R.bases].sort((a, b) => b.severity - a.severity || a.lon - b.lon);
    for (const b of order) {
      const label = b.name.replace(/^(CFB|BFC|Garrison) /, "");
      const p = map.latLngToContainerPoint([b.lat, b.lon]);
      const w = label.length * 6.2 + 10, left = p.x + 10 + w > width - 4;
      const box = { x0: left ? p.x - 10 - w : p.x + 8, x1: left ? p.x - 8 : p.x + 10 + w, y0: p.y - 8, y1: p.y + 8 };
      const clash = placed.some((q) => box.x0 < q.x1 && box.x1 > q.x0 && box.y0 < q.y1 && box.y1 > q.y0);
      if (!clash) placed.push(box);
      L.marker([b.lat, b.lon], { icon: L.divIcon({ className: "", html: `<div class="pin ${b.status}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7] }) })
        .bindTooltip(label, { permanent: !clash, direction: left ? "left" : "right", offset: [left ? -6 : 6, 0], className: "pinlabel" })
        .bindPopup(`<b>${esc(b.name)}</b><br>${b.status}${b.max_fwi != null ? ` · peak FWI ${n(b.max_fwi, 1)}` : ""}`)
        .addTo(map);
    }
  }

  function csv() {
    if (!R) return;
    const keys = ["fdr_label", "fwi", "isi", "bui", "ffmc", "dmc", "dc", "temp", "rh", "ws", "wdir", "precip", "cell_km"];
    const header = ["establishment", "province", "lat", "lon", "status", "date", ...keys, "flags"];
    const cell = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    const lines = [header.join(",")];
    for (const b of R.bases) {
      for (const d of b.days) lines.push([b.name, b.prov, b.lat, b.lon, b.status, d.date, ...keys.map((k) => d[k]), d.flags.join(" ")].map(cell).join(","));
    }
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv" })),
      download: `national-fire-weather-${issueTag().replace(" ", "-").replace(" CT", "")}.csv`,
    });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async function load() {
    doc.innerHTML = `<p class="loading">Building the national summary… (CWFIS grids for every base; about 15 s the first time)</p>`;
    try {
      const [r, al, fw] = await Promise.all([fetch("/api/national"), fetchAlerts(),
        fetch("/api/frontwatch").then((x) => (x.ok ? x.json() : null)).catch(() => null)]);
      ALERTS = al; FW = fw;
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      R = d;
      document.title = `National Fire Weather Summary – ${issueTag()}`;
      render();
    } catch (e) {
      doc.innerHTML = `<p class="error">Couldn't build the summary: ${esc(e.message)}</p>`;
    }
  }
  form.elements.for.addEventListener("change", () => { save(); if (R) render(); });
  form.elements.by.addEventListener("change", () => { save(); if (R) render(); });
  document.getElementById("refresh").onclick = load;
  document.getElementById("csv").onclick = csv;
  document.getElementById("print").onclick = () => window.print();
  load();
})();
