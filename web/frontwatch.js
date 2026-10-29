/* Front watch text, shared by the weather map's point panel, the burn report and the national summary.
   frontWatchHtml(fw, { tz, compact }) -> HTML for one location's /api/frontwatch result. */
(function () {
  "use strict";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const KIND = { cold: "Cold front", warm: "Warm front", stationary: "Stationary front", occluded: "Occluded front", trough: "Trough" };

  window.frontWatchHtml = function (fw, opts = {}) {
    if (!fw) return "";
    const tz = opts.tz || fw.tz;
    const when = (iso) => new Date(iso).toLocaleString("en-CA", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    const hhmm = (iso) => new Date(iso).toLocaleTimeString("en-CA", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
    const lines = [];

    // WPC: nearest analysed front and how it is moving
    const w = fw.wpc;
    if (w) {
      let s = `<b>${KIND[w.kind] || "Front"}</b> ${w.distance_km} km ${esc(w.lies)}`;
      const m = w.motion;
      if (!m) s += ", motion not measured";
      else if (m.speed_kmh < 3) s += ", nearly stationary";
      else {
        s += `, moving ${esc(m.toward)} at ${Math.round(m.speed_kmh)} km/h`;
        if (w.eta) s += ` → <b>arrives ~${when(w.eta)}</b>`;
        else if (w.closing_kmh != null && w.closing_kmh < 3) s += " (not coming this way)";
        else if (w.eta_hours) s += ` (not within 36 h at this speed, ~${Math.round(w.eta_hours)} h)`;
      }
      s += ` <span class="fw-src">WPC analysis ${hhmm(w.analysis_valid)}${m ? `, motion from ${hhmm(m.from_analysis)}` : ""}</span>`;
      lines.push(s);
    } else {
      lines.push(`No WPC front within 800 km`);
    }

    // RDPS: frontal wind shifts forecast at this point
    const ev = (fw.rdps || []).slice(0, opts.compact ? 1 : 3);
    if (ev.length) {
      for (const e of ev) {
        lines.push(`<span class="${e.strong ? "fw-strong" : ""}">${e.strong ? "⚠ " : ""}<b>${when(e.time)}</b>: wind ${esc(e.from_card)} ${e.from_speed} → `
          + `<b>${esc(e.to_card)} ${e.to_speed} km/h</b>, max ${e.max_wind}${e.max_gust != null ? `, gusts ${e.max_gust}` : ""}`
          + `${opts.compact ? "" : `; temp ${e.temp_change > 0 ? "+" : ""}${e.temp_change} °C, RH ${e.rh_change > 0 ? "+" : ""}${e.rh_change}%${e.pressure_trough ? ", pressure trough" : ""}`}</span>`);
      }
    } else {
      lines.push(`RDPS: no frontal wind shift forecast here in the next ${opts.compact ? "84" : "84"} h`);
    }
    const head = fw.level === "warning" ? `<div class="fw-head warning">Frontal wind shift forecast · strong</div>`
      : fw.level === "watch" ? `<div class="fw-head watch">Frontal wind shift forecast</div>` : `<div class="fw-head">Fronts</div>`;
    return `<div class="fw ${fw.level || ""}">${head}${lines.map((l) => `<div>${l}</div>`).join("")}</div>`;
  };
})();
