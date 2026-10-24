/* FBP fire growth: place an ignition, run the raster growth model, show isochrones. */
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const n = (v, d = 0) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const card = (d) => (d == null ? "" : COMPASS[Math.round(d / 22.5) % 16]);
  // FBP head fire intensity classes (kW/m)
  const HFI_CLASSES = [[10, "#2b83ba", "1  < 10"], [500, "#abdda4", "2  10–500"], [2000, "#ffffbf", "3  500–2,000"],
    [4000, "#fdae61", "4  2,000–4,000"], [10000, "#f46d43", "5  4,000–10,000"], [Infinity, "#d7191c", "6  > 10,000"]];

  // ---- map ------------------------------------------------------------------------
  const map = L.map("map", { minZoom: 4, maxZoom: 16 }).setView([49.79, -99.63], 11);
  const esri = (name) => `https://server.arcgisonline.com/ArcGIS/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`;
  const attr = 'Tiles &copy; Esri | Fuels: <a href="https://cwfis.cfs.nrcan.gc.ca/">CWFIS</a> FBP Fuel Types 2024 | Weather: ECCC RDPS';
  L.tileLayer(esri("World_Imagery"), { attribution: attr, maxNativeZoom: 17 }).addTo(map);
  map.createPane("labels").style.zIndex = 650;
  map.getPane("labels").style.pointerEvents = "none";
  L.tileLayer(esri("Reference/World_Boundaries_and_Places"), { pane: "labels", maxNativeZoom: 17 }).addTo(map);
  const fuelLayer = L.tileLayer.wms("https://cwfis.cfs.nrcan.gc.ca/geoserver/wms", {
    layers: "public:cffdrs_fbp_fuel_types", format: "image/png", transparent: true, opacity: 0.55, version: "1.1.1",
  }).addTo(map);
  let ignMarker = null, RESULT = null, ARR = null, HFI = null;

  // wind particles / arrows from the RDPS frames, shared with the weather viewer
  const meta = await (await fetch("/api/meta")).json();
  const windCanvas = document.createElement("canvas");
  windCanvas.id = "wind";
  map.getContainer().appendChild(windCanvas);
  const wind = WindLayer(map, windCanvas, meta);
  let windMode = "particles";
  try { windMode = JSON.parse(localStorage.getItem("fw-prefs") || "{}").wind || windMode; } catch (_) {}
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches && windMode === "particles") windMode = "arrows";
  $("wind-mode").value = windMode;
  wind.setMode(windMode);
  $("wind-mode").onchange = (e) => {
    wind.setMode(e.target.value);
    try { localStorage.setItem("fw-prefs", JSON.stringify({ ...JSON.parse(localStorage.getItem("fw-prefs") || "{}"), wind: e.target.value })); } catch (_) {}
    windHour = null;
    if (RESULT) setTime(T);
  };
  const frameCache = new Map();
  function getFrame(run, v, fh) {
    const key = `${run}/${v}/${fh}`;
    if (!frameCache.has(key)) {
      frameCache.set(key, fetch(`/api/frame/${run}/${v}/${fh}`).then(async (r) => {
        if (!r.ok) return null;
        const b = new Uint8Array(await r.arrayBuffer()), half = b.length >> 1, q = new Uint16Array(half);
        for (let i = 0; i < half; i++) q[i] = (b[i] << 8) | b[half + i];  // un-shuffle
        return q;
      }).catch(() => null));
    }
    return frameCache.get(key);
  }
  let windHour = null;
  async function showWindAt(utcMs) {
    if (!RESULT || wind.mode === "off") return;
    const run = RESULT.options.run;
    const r0 = Date.UTC(+run.slice(0, 4), +run.slice(4, 6) - 1, +run.slice(6, 8), +run.slice(8, 10));
    const fh = Math.max(0, Math.min(84, Math.floor((utcMs - r0) / 3600e3)));
    if (fh === windHour) return;
    windHour = fh;
    const [spd, dir] = await Promise.all([getFrame(run, "wspd", fh), getFrame(run, "wdir", fh)]);
    if (fh === windHour) wind.setFrames(spd, dir, "wspd", "wdir");
  }

  // ---- areas of interest, runs, time zone ------------------------------------------
  const aoiData = await (await fetch("/api/aoi")).json();
  $("aoi").innerHTML = aoiData.areas.map((a) => `<option value="${a.id}">${esc(a.name)} (${a.prov})</option>`).join("");
  let saved = null;
  try { saved = localStorage.getItem("fw-aoi"); } catch (_) {}
  $("aoi").value = aoiData.areas.some((a) => a.id === saved) ? saved : aoiData.default;
  const runs = await (await fetch("/api/runs")).json();
  const run = (runs.find((r) => r.hours.length >= 49) || runs[0])?.run;
  let TZ = "UTC";

  async function setTZ(lat, lon) {
    try { TZ = (await (await fetch(`/api/tz?lat=${lat}&lon=${lon}`)).json()).timezone; } catch (_) {}
    buildStartOptions();
  }
  const runStart = run ? Date.UTC(+run.slice(0, 4), +run.slice(4, 6) - 1, +run.slice(6, 8), +run.slice(8, 10)) : Date.now();
  const fmt = (ms, opts) => new Date(ms).toLocaleString("en-CA", { timeZone: TZ, hour12: false, ...opts });
  function buildStartOptions() {
    const prev = $("start").value, now = Date.now(), opts = [];
    let firstIgn = null;
    for (let h = 0; h <= 78; h++) {
      const ms = runStart + h * 3600e3;
      if (ms < now - 3600e3) continue;
      const hr = +fmt(ms, { hour: "2-digit" });
      if (firstIgn == null && hr === 11) firstIgn = ms;
      opts.push(`<option value="${new Date(ms).toISOString()}">${fmt(ms, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</option>`);
    }
    $("start").innerHTML = opts.join("");
    $("start").value = prev && opts.some((o) => o.includes(prev)) ? prev : new Date(firstIgn ?? Math.max(runStart, now)).toISOString();
    if (!$("start").value && $("start").options.length) $("start").selectedIndex = 0;
  }

  function setIgnition(latlng, fly = false) {
    if (ignMarker) ignMarker.setLatLng(latlng);
    else ignMarker = L.marker(latlng, { icon: L.divIcon({ className: "", html: '<div style="font-size:22px;line-height:22px;transform:translate(-11px,-20px)">🔥</div>' }) }).addTo(map);
    $("ign-box").classList.add("set");
    $("ign-pos").textContent = `${latlng.lat.toFixed(5)}°, ${latlng.lng.toFixed(5)}°`;
    $("ign-hint").textContent = "click the map to move it";
    $("run").disabled = !run;
    if (fly) map.setView(latlng, 12);
    setTZ(latlng.lat, latlng.lng);
  }
  function selectAOI(id) {
    const a = aoiData.areas.find((x) => x.id === id);
    if (!a) return;
    try { localStorage.setItem("fw-aoi", id); } catch (_) {}
    setIgnition(L.latLng(a.lat, a.lon), true);
  }
  $("aoi").onchange = (e) => selectAOI(e.target.value);
  map.on("click", (e) => setIgnition(e.latlng));
  const qp = new URLSearchParams(location.search);
  if (qp.get("lat") && qp.get("lon")) setIgnition(L.latLng(+qp.get("lat"), +qp.get("lon")), true);
  else selectAOI($("aoi").value);

  // ---- run ----------------------------------------------------------------------------
  const numOrNull = (id) => ($(id).value === "" ? null : +$(id).value);
  $("run").onclick = async () => {
    if (!ignMarker) return;
    const ll = ignMarker.getLatLng();
    const body = {
      lat: ll.lat, lon: ll.lng, start: $("start").value, duration_h: +$("duration").value, run,
      radius_km: +$("radius").value, curing: numOrNull("curing"), leaf: $("leaf").value,
      unclassified: $("unclassified").value, gfl: +$("gfl").value || 0.35,
      ffmc: numOrNull("ffmc"), dmc: numOrNull("dmc"), dc: numOrNull("dc"),
    };
    $("run").disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { $("status").textContent = `Simulating… ${((Date.now() - t0) / 1000).toFixed(0)} s`; }, 500);
    $("status").className = "status";
    try {
      const r = await fetch("/api/fire/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      await showResult(d);
      $("status").textContent = `Done in ${d.runtime_s} s.`;
    } catch (e) {
      console.error(e);
      $("status").textContent = e.message;
      $("status").className = "status err";
    } finally {
      clearInterval(tick);
      $("run").disabled = false;
    }
  };

  async function decode(b64, Type) {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const buf = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
    return new Type(buf);
  }

  // ---- results ------------------------------------------------------------------------
  const timeColour = (k, nH) => {
    const stops = [[255, 230, 102], [255, 146, 43], [232, 89, 12], [190, 30, 45], [120, 20, 90]];
    const t = nH <= 1 ? 1 : (k - 1) / (nH - 1), x = t * (stops.length - 1), i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
    const c = stops[i].map((v, j) => Math.round(v + (stops[i + 1][j] - v) * f));
    return `rgb(${c.join(",")})`;
  };

  // ---- playback ----------------------------------------------------------------------
  // The burned area is a raster redrawn each frame (cells with arrival <= t, the active
  // front brightest); completed hourly perimeters are lines and the current front a contour.
  let T = 0, playing = false, lastTs = null, raf = null, lastFrontT = null;
  let fireCanvas = null, fireCtx = null, fireImg = null, fireOverlay = null, frontLayer = null;
  let VALS = null, HOURK = null, HFIK = null, isoLines = [];
  const rgbOf = (css) => css.match(/\d+/g).map(Number);
  const HFI_RGB = HFI_CLASSES.map(([, hex]) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)));
  const ACTIVE_MIN = 20;  // cells ignited in the last 20 min are drawn as the active front
  const dur = () => RESULT.options.duration_h * 60;

  async function showResult(d) {
    stop();
    RESULT = d;
    ARR = await decode(d.arrival_u16, Uint16Array);
    HFI = await decode(d.hfi_u16, Uint16Array);
    const g = d.grid, nH = d.hourly.length;
    // per-cell lookups computed once so each frame is a single pass
    VALS = new Float64Array(ARR.length);
    HOURK = new Uint8Array(ARR.length);
    HFIK = new Uint8Array(ARR.length);
    let r0 = g.h, r1 = -1, c0 = g.w, c1 = -1;
    for (let i = 0; i < ARR.length; i++) {
      if (!ARR[i]) { VALS[i] = -1e7; continue; }
      const t = ARR[i] - 1;
      VALS[i] = -t;
      HOURK[i] = Math.max(1, Math.min(nH, Math.ceil(t / 60)));
      HFIK[i] = HFI_CLASSES.findIndex(([u]) => HFI[i] * 10 < u);
      const r = (i / g.w) | 0, c = i % g.w;
      if (r < r0) r0 = r;
      if (r > r1) r1 = r;
      if (c < c0) c0 = c;
      if (c > c1) c1 = c;
    }
    isoLines.forEach(({ layer }) => layer.remove());
    if (frontLayer) { frontLayer.remove(); frontLayer = null; }
    if (fireOverlay) fireOverlay.remove();
    fireCanvas = document.createElement("canvas");
    fireCanvas.width = g.w;
    fireCanvas.height = g.h;
    fireCanvas.classList.add("fire-raster");
    fireCtx = fireCanvas.getContext("2d");
    fireImg = fireCtx.createImageData(g.w, g.h);
    const bounds = [[g.north + g.h * g.sy, g.west], [g.north, g.west + g.w * g.sx]];
    fireOverlay = L.svgOverlay(fireCanvas, bounds, { interactive: false }).addTo(map);
    // hourly perimeter lines, computed once
    const toLL = (ring) => ring.map(([x, y]) => [g.north + y * g.sy, g.west + x * g.sx]);
    // Arrival times are whole minutes and many cells sit exactly on hour boundaries; contouring
    // at half-minute offsets avoids the 0/0 interpolation d3 produces when both corners equal it.
    isoLines = d3.contours().size([g.w, g.h]).thresholds(d.hourly.map((h) => -(h.minutes + 0.5)))(VALS).map((p) => {
      const minutes = -p.value - 0.5;
      const k = d.hourly.findIndex((h) => h.minutes === minutes) + 1;
      return { minutes, layer: L.polygon(p.coordinates.map((poly) => poly.map(toLL)), {
        color: timeColour(k, nH), weight: 1.3, opacity: 0.9, fill: false, interactive: false }) };
    });
    $("tslider").max = dur();
    $("results").hidden = false;
    if (d.ignition.snapped_m > 0) setIgnitionQuiet(L.latLng(d.ignition.lat, d.ignition.lon));
    renderTables();
    if (r1 >= 0) {
      map.fitBounds([[g.north + (r1 + 1) * g.sy, g.west + c0 * g.sx], [g.north + r0 * g.sy, g.west + (c1 + 1) * g.sx]],
        { maxZoom: 15, padding: [80, 80] });
    }
    windHour = null;
    lastFrontT = null;
    setTime(dur());
  }
  function setIgnitionQuiet(ll) { ignMarker.setLatLng(ll); $("ign-pos").textContent = `${ll.lat.toFixed(5)}°, ${ll.lng.toFixed(5)}°`; }

  function setTime(t) {
    if (!RESULT) return;
    const d = RESULT, g = d.grid, nH = d.hourly.length, mode = document.querySelector('input[name="colour"]:checked').value;
    T = Math.max(0, Math.min(dur(), t));
    $("tslider").value = T;
    // burned-area raster
    const px = fireImg.data, hourRGB = d.hourly.map((h, i) => rgbOf(timeColour(i + 1, nH)));
    let burned = 0;
    for (let i = 0; i < ARR.length; i++) {
      const a = ARR[i];
      if (!a || a - 1 > T) { px[i * 4 + 3] = 0; continue; }
      burned++;
      const o = i * 4;
      if (a - 1 > T - ACTIVE_MIN) { px[o] = 255; px[o + 1] = 244; px[o + 2] = 200; px[o + 3] = 255; continue; }
      const c = mode === "hfi" ? HFI_RGB[HFIK[i]] : hourRGB[HOURK[i] - 1];
      px[o] = c[0]; px[o + 1] = c[1]; px[o + 2] = c[2]; px[o + 3] = mode === "hfi" ? 215 : 150;
    }
    fireCtx.putImageData(fireImg, 0, 0);
    // completed hourly perimeters + current front
    isoLines.forEach(({ minutes, layer }) => (minutes <= T + 1e-6 ? layer.addTo(map) : layer.remove()));
    if (lastFrontT === null || Math.abs(T - lastFrontT) >= 1) {
      lastFrontT = T;
      if (frontLayer) frontLayer.remove();
      const [front] = d3.contours().size([g.w, g.h]).thresholds([-(Math.floor(T) + 0.5)])(VALS);
      frontLayer = front && front.coordinates.length
        ? L.polygon(front.coordinates.map((poly) => poly.map((ring) => ring.map(([x, y]) => [g.north + y * g.sy, g.west + x * g.sx]))),
          { color: "#fff", weight: 2, fill: false, interactive: false }).addTo(map) : null;
    }
    // labels, table row, wind for this hour
    const nowMs = Date.parse(d.options.start_local) + T * 60000;
    const k = Math.max(1, Math.ceil(T / 60)), h = d.hourly[Math.min(k, nH) - 1];
    const ha = burned * g.dx * g.dy / 1e4;
    const clock = new Date(nowMs).toLocaleString("en-CA", { timeZone: d.options.timezone, weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    $("tlabel").textContent = `${clock}  +${(T / 60).toFixed(1)} h  ·  ${n(ha, ha < 10 ? 1 : 0)} ha`;
    $("twx").textContent = h ? `wind ${card(h.wx.wd)} ${n(h.wx.ws)} km/h · FFMC ${n(h.wx.ffmc, 1)} · ISI ${n(h.wx.isi, 1)}` : "";
    document.querySelectorAll("#hourly tbody tr").forEach((tr, i) => tr.classList.toggle("now", i === k - 1));
    legend(mode);
    showWindAt(nowMs);
  }

  function frame(ts) {
    raf = requestAnimationFrame(frame);
    if (lastTs == null) { lastTs = ts; return; }
    const t = T + ((ts - lastTs) / 1000) * +$("speed").value;  // speed = simulated minutes per second
    lastTs = ts;
    if (t >= dur()) { setTime(dur()); stop(); return; }
    setTime(t);
  }
  function play() {
    if (!RESULT) return;
    if (T >= dur()) setTime(0);
    playing = true;
    lastTs = null;
    $("play").classList.add("on");
    $("play").textContent = "❚❚";
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    playing = false;
    cancelAnimationFrame(raf);
    $("play").classList.remove("on");
    $("play").textContent = "▶";
  }
  $("play").onclick = () => (playing ? stop() : play());

  const fmtLocal = (iso) => `${iso.slice(11, 13)}${iso.slice(14, 16)} ${new Date(iso).toLocaleDateString("en-CA", { weekday: "short", timeZone: RESULT?.options.timezone || TZ })}`;

  function legend(mode) {
    const hours = RESULT.hourly;
    let html = "";
    if (mode === "hfi") {
      html += `<b>Head fire intensity class</b> <span class="sub">kW/m</span>` +
        HFI_CLASSES.map(([, c, l]) => `<div class="row"><i style="background:${c}"></i>${l}</div>`).join("");
    } else {
      html += `<b>Fire perimeter by hour</b><div class="row"><i style="background:#fff4c8"></i>Active front (last ${ACTIVE_MIN} min)</div>` + hours.map((h, i) => `<div class="row"><i style="background:${timeColour(i + 1, RESULT.hourly.length)}"></i>${fmtLocal(h.local)} · ${n(h.area_ha, 1)} ha</div>`).join("");
    }
    if ($("show-fuel").checked) {
      html += `<div style="margin-top:8px"><b>Fuel type</b></div><img alt="FBP fuel types legend" src="https://cwfis.cfs.nrcan.gc.ca/geoserver/wms?service=WMS&request=GetLegendGraphic&format=image/png&layer=public:cffdrs_fbp_fuel_types&legend_options=fontAntiAliasing:true;fontSize:10">`;
    }
    $("legend").innerHTML = html;
  }

  function renderTables() {
    const d = RESULT, H = d.hourly, last = H[H.length - 1];
    const maxRos = Math.max(...H.map((h) => h.head.ROS || 0));
    const maxHfi = Math.max(...H.map((h) => h.hfi_max || 0));
    const crown = H.some((h) => (h.crown_fraction || 0) > 0);
    $("cards").innerHTML = [
      [`${n(last.area_ha, last.area_ha < 10 ? 1 : 0)} ha`, `Area after ${d.options.duration_h} h`],
      [`${n(last.perimeter_km, 1)} km`, "Perimeter"],
      [`${n(maxRos, 1)} m/min`, "Max head ROS (ignition fuel)"],
      [`${n(maxHfi / 1000, 1)} MW/m`, "Max fire intensity"],
      [crown ? "Crowning" : "Surface", "Fire type"],
      [`${n(d.options.res_m)} m`, "Grid cell"],
    ].map(([v, k]) => `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
    const warns = [];
    if (d.edge_hit) warns.push("The fire reached the edge of the modelled area; increase the domain radius.");
    if (d.ignition.snapped_m > 0) warns.push(`Ignition moved ${d.ignition.snapped_m} m to the nearest burnable cell (${esc(d.ignition.fuel)}).`);
    const uncl = d.fuels.find((f) => f.code === -9999), tot = d.fuels.reduce((s, f) => s + f.cells, 0);
    if (uncl && uncl.cells / tot > 0.1 && d.options.unclassified === "NF")
      warns.push(`${n(100 * uncl.cells / tot)}% of the area has no FBP fuel type (often agriculture) and is treated as non-fuel. Change "Unclassified land" if that's grass.`);
    $("warn").innerHTML = warns.map((w) => `<div class="w">${w}</div>`).join("");
    let prevArea = 0;
    $("hourly").innerHTML = `<thead><tr><th>Time</th><th>Area ha</th><th>+ha</th><th>Perim km</th><th>Wind</th><th>FFMC</th><th>BUI</th><th>ISI</th><th>Head ROS</th><th>HFI max</th><th>Type</th></tr></thead><tbody>` +
      H.map((h) => {
        const grow = h.area_ha - prevArea; prevArea = h.area_ha;
        return `<tr><td>${fmtLocal(h.local)}</td><td>${n(h.area_ha, 1)}</td><td>${n(grow, 1)}</td><td>${n(h.perimeter_km, 2)}</td>
          <td>${card(h.wx.wd)} ${n(h.wx.ws)}</td><td>${n(h.wx.ffmc, 1)}</td><td>${n(h.wx.bui, 0)}</td><td>${n(h.wx.isi, 1)}</td>
          <td>${n(h.head.ROS, 1)}</td><td>${n(h.hfi_max)}</td><td><span class="ft ${h.head.FD}">${h.head.FD}</span></td></tr>`;
      }).join("") + "</tbody>";
    const s = d.seed, o = d.options, ig = d.ignition;
    $("inputs").innerHTML = `
      <div><b>Ignition fuel:</b> ${esc(ig.fuel)}; elevation ${n(ig.elev)} m, slope ${n(ig.slope, 1)}%, aspect ${n(ig.aspect)}°.</div>
      <div><b>Start:</b> ${fmtLocal(o.start_local)} (${esc(o.timezone)}), ${o.duration_h} h, RDPS ${o.run.slice(8)}Z ${o.run.slice(0, 8)}.</div>
      <div><b>Starting codes (FWI1987):</b> ${s.source}${s.seed_date ? `, ${s.seed_date}` : ""} — FFMC ${n(s.ffmc, 1)}, DMC ${n(s.dmc, 1)}, DC ${n(s.dc, 0)}; then hourly FFMC and daily DMC/DC/BUI from the RDPS forecast at the ignition.</div>
      <div><b>Fuel options:</b> grass curing ${n(o.curing)}%, fuel load ${o.gfl} kg/m², deciduous ${o.leaf}, unclassified land ${o.unclassified === "NF" ? "non-fuel" : o.unclassified}.</div>
      <div><b>Model:</b> FBP System (cffdrs-validated) with point-ignition acceleration; minimum-travel-time growth on a ${n(o.res_m)} m grid, 32 spread directions, weather uniform over the area and updated hourly. Slope from the CWFIS 250 m DEM.</div>
      <div><b>Not modelled:</b> spotting, suppression, sub-hourly wind changes, local wind effects. Head ROS and fire type are for the ignition cell's fuel.</div>
      <div><b>Fuels in the area:</b> ${d.fuels.slice(0, 8).map((f) => `${esc(f.label)} ${n(100 * f.cells / tot)}%`).join(", ")}.</div>`;
  }

  $("tslider").oninput = (e) => { stop(); setTime(+e.target.value); };
  document.querySelectorAll('input[name="colour"]').forEach((r) => (r.onchange = () => setTime(T)));
  document.addEventListener("keydown", (e) => {
    const typing = (e.target.tagName === "INPUT" && e.target.type !== "range") || e.target.tagName === "SELECT";
    if (!RESULT || typing) return;
    if (e.key === " ") { playing ? stop() : play(); e.preventDefault(); }
    else if (e.key === "ArrowRight") { stop(); setTime(T + 10); e.preventDefault(); }
    else if (e.key === "ArrowLeft") { stop(); setTime(T - 10); e.preventDefault(); }
  });
  $("show-fuel").onchange = (e) => { e.target.checked ? fuelLayer.addTo(map) : fuelLayer.remove(); if (RESULT) legend(document.querySelector('input[name="colour"]:checked').value); };
  if (!run) { $("status").textContent = "No model run available yet."; }
})();
