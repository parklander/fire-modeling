/* RDPS fire-weather viewer */
(async function () {
  "use strict";

  // ---- colour ramps: [value, "#rrggbb", alpha?] ------------------------------
  const RAMPS = {
    temp: { min: -40, max: 40, stops: [
      [-40, "#3b0f70"], [-30, "#4c3fb5"], [-20, "#3a78d1"], [-10, "#5fb2e6"], [0, "#e8f4f8"],
      [5, "#bfe6a8"], [10, "#f6e88a"], [15, "#fbc35a"], [20, "#f68e3c"], [25, "#e55332"],
      [30, "#c11f3a"], [35, "#86103f"], [40, "#4a0a2e"]], ticks: [-40, -20, 0, 10, 20, 30, 40] },
    rh: { min: 0, max: 100, stops: [
      [0, "#7a0403"], [10, "#c11f1f"], [20, "#ee5a24"], [30, "#f8a33c"], [40, "#f7dc6f"],
      [50, "#d4e9a4"], [60, "#9fd5a0"], [70, "#5fb7a6"], [80, "#3a8fb7"], [90, "#2c5f9e"],
      [100, "#1f3a73"]], ticks: [0, 20, 40, 60, 80, 100] },
    wspd: { min: 0, max: 100, stops: [
      [0, "#24405e", 0.25], [10, "#3f7f9e", 0.6], [20, "#58b3a0"], [30, "#a7d36b"], [40, "#f2e05a"],
      [50, "#f7a541"], [60, "#ec5f30"], [80, "#c2185b"], [100, "#7b1fa2"], [130, "#e1bee7"]],
      ticks: [0, 20, 40, 60, 80, 100] },
    precip: { min: 0, max: 100, log: true, stops: [
      [0, "#000000", 0], [0.2, "#c7e9c0", 0.45], [1, "#86cf7a"], [2.5, "#3fae55"], [5, "#2a8f8f"],
      [10, "#2f6db5"], [20, "#3b3ea8"], [35, "#7a2fa8"], [50, "#b62f8f"], [75, "#e0527a"],
      [100, "#f7b3b3"]], ticks: [0.2, 1, 5, 10, 25, 50, 100] },
  };
  // FWI system: CWFIS danger classes (breaks from the CWFIS GeoServer legends) as [upper bound, colour, label]
  const C = { low: "#2b6fe0", mod: "#2fbf4f", high: "#f5e33b", vh: "#e0a000", ext: "#ff2a1a" };
  const cls = (b) => ({
    classes: [[b[0], C.low], [b[1], C.mod], [b[2], C.high], [b[3], C.vh], [Infinity, C.ext]]
      .map(([u, c], i) => [u, c, i === 0 ? `0–${b[0]}` : u === Infinity ? `>${b[3]}` : `${b[i - 1]}–${u}`]),
  });
  Object.assign(RAMPS, {
    fwi: cls([5, 10, 20, 30]),
    isi: cls([2, 5, 10, 15]),
    ffmc: cls([74, 84, 88, 91]),
    dmc: cls([21, 27, 40, 60]),
    dc: cls([80, 190, 300, 425]),
    bui: { classes: [[20, C.low, "0–20"], [30, "#1e7f3a", "20–30"], [40, C.mod, "30–40"], [60, C.high, "40–60"], [90, C.vh, "60–90"], [Infinity, C.ext, ">90"]] },
    gust: RAMPS.wspd,
    twspd: RAMPS.wspd,
    pbl: { min: 0, max: 3000, stops: [[0, "#3b2a6b", 0.55], [250, "#4c3fb5"], [500, "#3a78d1"], [1000, "#58b3a0"], [1500, "#a7d36b"], [2000, "#f2e05a"], [3000, "#f7a541"]], ticks: [0, 500, 1000, 2000, 3000] },
    // BC smoke-control ventilation categories
    vi: { classes: [[2350, "#d7263d", "Poor <2350"], [4700, "#f2c14e", "Fair"], [Infinity, "#2fbf4f", "Good >4700"]] },
    // no CWFIS classes for the grassland FWI: continuous
    gfwi: { min: 0, max: 40, stops: [[0, "#2b6fe0", 0.35], [5, "#2fbf4f"], [10, "#a7d36b"], [15, "#f5e33b"], [25, "#e0a000"], [35, "#ff2a1a"], [50, "#9c1aff"]], ticks: [0, 10, 20, 30, 40] },
  });
  const LAYERS = [
    { id: "temp", name: "Temperature", group: "Weather" },
    { id: "rh", name: "Humidity", group: "Weather" },
    { id: "wspd", name: "Wind", group: "Weather" },
    { id: "precip", name: "Precip (accum.)", group: "Weather" },
    { id: "gust", name: "Gusts", group: "Weather" },
    { id: "pbl", name: "Mixing ht", group: "Smoke" },
    { id: "twspd", name: "Transport wind", group: "Smoke" },
    { id: "vi", name: "Ventilation", group: "Smoke" },
    { id: "fwi", name: "FWI", group: "FWI2025" },
    { id: "isi", name: "ISI", group: "FWI2025" },
    { id: "ffmc", name: "FFMC", group: "FWI2025" },
    { id: "bui", name: "BUI", group: "FWI2025" },
    { id: "dmc", name: "DMC", group: "FWI2025" },
    { id: "dc", name: "DC", group: "FWI2025" },
    { id: "gfwi", name: "Grass FWI", group: "FWI2025" },
  ];
  const READOUT_VARS = ["temp", "rh", "wspd", "wdir", "precip", "ffmc", "isi", "bui", "fwi"];
  function badge(v, val) {
    if (isNaN(val)) return "—";
    const c = RAMPS[v].classes.find(([u]) => val <= u);
    return `<i class="badge" style="background:${c[1]}"></i>${val.toFixed(1)}`;
  }
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

  // ---- state ----------------------------------------------------------------
  const meta = await (await fetch("/api/meta")).json();
  const { width: W, height: H, bounds: B } = meta;
  const S = {
    runs: [], run: null, hour: 0, layer: "rh", playing: false,
    wind: "particles", opacity: 0.75, point: null, followLatest: true,
  };
  try { Object.assign(S, JSON.parse(localStorage.getItem("fw-prefs") || "{}"), { playing: false, point: null }); } catch (_) {}
  if (!["particles", "arrows", "off"].includes(S.wind)) S.wind = "particles";
  if (S.wind === "particles" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) S.wind = "arrows";
  const savePrefs = () => { try { localStorage.setItem("fw-prefs", JSON.stringify({ layer: S.layer, wind: S.wind, opacity: S.opacity })); } catch (_) {} };

  const $ = (id) => document.getElementById(id);
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
  const yN = mercY(B.north), yS = mercY(B.south);

  function latLonToPx(lat, lon) {
    const x = Math.floor(((lon - B.west) / (B.east - B.west)) * W);
    const y = Math.floor(((yN - mercY(lat)) / (yN - yS)) * H);
    return x >= 0 && x < W && y >= 0 && y < H ? y * W + x : -1;
  }
  function decodeVal(v, q) {
    if (!q) return NaN;
    const spec = meta.variables[v];
    return (q - 1) / spec.scale + spec.offset;
  }

  // ---- colour lookup tables over the uint16 code space -----------------------
  function rampColor(ramp, v) {
    if (ramp.classes) {
      const c = ramp.classes.find(([u]) => v <= u) || ramp.classes.at(-1);
      return [...hex(c[1]), 1];
    }
    const st = ramp.stops;
    if (v <= st[0][0]) return [...hex(st[0][1]), st[0][2] ?? 1];
    for (let i = 1; i < st.length; i++) {
      if (v <= st[i][0]) {
        const [v0, c0, a0 = 1] = st[i - 1], [v1, c1, a1 = 1] = st[i];
        const t = (v - v0) / (v1 - v0), A = hex(c0), Bc = hex(c1);
        return [0, 1, 2].map((k) => A[k] + (Bc[k] - A[k]) * t).concat(a0 + (a1 - a0) * t);
      }
    }
    const l = st[st.length - 1];
    return [...hex(l[1]), l[2] ?? 1];
  }
  const LUTS = {};
  function lut(v) {
    if (LUTS[v]) return LUTS[v];
    const spec = meta.variables[v], ramp = RAMPS[v];
    const out = new Uint32Array(65536);
    const top = ramp.classes ? Infinity : ramp.stops.at(-1)[0];
    const maxCode = Math.min(65535, Math.ceil((top - spec.offset) * spec.scale) + 1);
    for (let q = 1; q < 65536; q++) {
      const val = q <= maxCode ? (q - 1) / spec.scale + spec.offset : top;
      const [r, g, b, a] = rampColor(ramp, val);
      out[q] = ((Math.round(a * 255) << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r)) >>> 0;
    }
    return (LUTS[v] = out);
  }

  // ---- frame cache ----------------------------------------------------------
  const cache = new Map(); // key -> Promise<Uint16Array|null>
  const CACHE_MAX = 90;
  function getFrame(run, v, h) {
    const key = `${run}/${v}/${h}`;
    if (cache.has(key)) {
      const p = cache.get(key);
      cache.delete(key); cache.set(key, p); // LRU bump
      return p;
    }
    const p = fetch(`/api/frame/${run}/${v}/${h}`).then(async (r) => {
      if (!r.ok) { cache.delete(key); return null; }
      const bytes = new Uint8Array(await r.arrayBuffer());
      const n = bytes.length >> 1, q = new Uint16Array(n);
      for (let i = 0; i < n; i++) q[i] = (bytes[i] << 8) | bytes[n + i]; // un-shuffle
      return q;
    }).catch(() => { cache.delete(key); return null; });
    cache.set(key, p);
    while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
    return p;
  }

  // ---- map ------------------------------------------------------------------
  const map = L.map("map", { zoomControl: true, worldCopyJump: false, minZoom: 2, maxZoom: 10 })
    .fitBounds([[45, -135], [65, -65]]);
  map.zoomControl.setPosition("topright");
  const attr = 'Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors | Data: <a href="https://eccc-msc.github.io/open-data/">ECCC MSC</a> RDPS';
  const esri = (name) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${name}/MapServer/tile/{z}/{y}/{x}`;
  L.tileLayer(esri("World_Dark_Gray_Base"), { attribution: attr, maxNativeZoom: 16 }).addTo(map);
  map.createPane("labels").style.zIndex = 620;
  map.getPane("labels").style.pointerEvents = "none";
  L.tileLayer(esri("World_Dark_Gray_Reference"), { pane: "labels", maxNativeZoom: 16 }).addTo(map);

  const fieldCanvas = document.createElement("canvas");
  fieldCanvas.width = W; fieldCanvas.height = H;
  const fctx = fieldCanvas.getContext("2d");
  const img = fctx.createImageData(W, H);
  const img32 = new Uint32Array(img.data.buffer);
  // SVGOverlay just positions an arbitrary element over the bounds; a canvas works fine.
  const overlay = L.svgOverlay(fieldCanvas, [[B.south, B.west], [B.north, B.east]], { opacity: S.opacity, interactive: false }).addTo(map);
  let pointMarker = null;

  // ---- wind: animated particles or static arrows (shared with the fire page) ---
  // S.wind is "particles" | "arrows" | "off". The transport-wind layer shows the
  // mixed-layer wind; every other layer shows the 10 m wind.
  const wind = WindLayer(map, $("arrows"), meta);
  wind.setMode(S.wind);
  const windVars = () => (S.layer === "twspd" ? ["twspd", "twdir"] : ["wspd", "wdir"]);

  // ---- rendering ------------------------------------------------------------
  let renderToken = 0;
  async function render() {
    const tok = ++renderToken, run = S.run, h = S.hour, layer = S.layer;
    if (!run) return;
    const [sv, dv] = windVars(), showWind = S.wind !== "off";
    const [q, spd, dir] = await Promise.all([
      getFrame(run, layer, h),
      showWind ? getFrame(run, sv, h) : null,
      showWind ? getFrame(run, dv, h) : null,
    ]);
    if (tok !== renderToken) return;
    if (q) {
      const L_ = lut(layer);
      for (let i = 0; i < q.length; i++) img32[i] = L_[q[i]];
      fctx.putImageData(img, 0, 0);
    } else {
      fctx.clearRect(0, 0, W, H);
    }
    wind.setFrames(spd, dir, sv, dv);
    updateReadout();
    prefetch();
  }
  function prefetch() {
    const run = S.run, hrs = availableHours();
    const idx = hrs.indexOf(S.hour);
    for (let k = 1; k <= 4 && idx >= 0; k++) {
      const h = hrs[(idx + k) % hrs.length];
      getFrame(run, S.layer, h);
      if (S.wind !== "off") for (const v of windVars()) getFrame(run, v, h);
    }
    if (!S.playing) for (const v of READOUT_VARS) getFrame(run, v, S.hour);
  }

  // ---- runs / time ----------------------------------------------------------
  const runDate = (run) => new Date(Date.UTC(+run.slice(0, 4), +run.slice(4, 6) - 1, +run.slice(6, 8), +run.slice(8, 10)));
  const runName = (run) => `${run.slice(8, 10)}Z ${runDate(run).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })}`;
  const currentRun = () => S.runs.find((r) => r.run === S.run);
  const isIndex = (v) => meta.variables[v]?.index;
  const BURN_LAYERS = new Set(["gust", "pbl", "twspd", "vi"]);  // hourly to T+48, then 3-hourly
  const availableHours = () => (isIndex(S.layer) ? currentRun()?.index_hours
    : BURN_LAYERS.has(S.layer) ? currentRun()?.smoke_hours : currentRun()?.hours) ?? [];
  const fmtLocal = (d) => d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const fmtUTC = (d) => `${String(d.getUTCHours()).padStart(2, "0")}Z ${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", timeZone: "UTC" })}`;

  function pickDefaultRun() {
    return (S.runs.find((r) => r.complete) || S.runs.find((r) => r.hours.length) || S.runs[0])?.run ?? null;
  }

  async function loadRuns() {
    S.runs = await (await fetch("/api/runs")).json();
    const sel = $("run-select");
    sel.innerHTML = S.runs.map((r) =>
      `<option value="${r.run}">${runName(r.run)} ${r.complete ? "" : `(${Math.round((100 * r.count) / r.total)}%)`}</option>`).join("");
    // Jump to a newer complete run automatically unless the user picked a run themselves.
    // Keep the displayed valid time (the current hour on first load), not the forecast hour.
    const def = pickDefaultRun();
    if (!S.run || !S.runs.some((r) => r.run === S.run) || (S.followLatest && def && def > S.run)) {
      const keep = S.run ? validMs() : Date.now();
      S.run = def;
      if (def) { S.hour = hourAt(def, keep); snapHour(); }
    }
    sel.value = S.run ?? "";
    updateBanner(); updateTimebar();
  }

  const validMs = () => runDate(S.run).getTime() + S.hour * 3600e3;
  // forecast hour of `run` nearest to a wall-clock time, kept within the forecast range
  const hourAt = (run, ms) => Math.max(0, Math.min(84, Math.round((ms - runDate(run).getTime()) / 3600e3)));

  function snapHour() {
    const hrs = availableHours();
    if (!hrs.length) return;
    if (!hrs.includes(S.hour)) S.hour = hrs.reduce((a, b) => (Math.abs(b - S.hour) < Math.abs(a - S.hour) ? b : a));
  }

  function updateBanner() {
    const ban = $("banner");
    const newer = S.runs.find((r) => r.run > S.run);
    if (!newer) { ban.hidden = true; return; }
    ban.hidden = false;
    const pct = Math.round((100 * newer.count) / newer.total);
    ban.innerHTML = newer.complete
      ? `<span><b>${runName(newer.run)}</b> run is complete</span><button data-run="${newer.run}">Switch</button>`
      : `<span><b>${runName(newer.run)}</b> arriving · ${newer.hours.length}/85 h</span><span class="bar"><i style="width:${pct}%"></i></span><button data-run="${newer.run}">View</button>`;
    ban.querySelector("button").onclick = (e) => setRun(e.target.dataset.run);
  }

  function updateTimebar() {
    const run = S.run;
    const cr = currentRun();
    $("run-label").textContent = run ? `Run ${runName(run)} · weather ${cr?.hours.length ?? 0}/85 h · FWI ${cr?.index_hours?.length ?? 0}/85 h` : "Waiting for data…";
    const set = new Set(availableHours());
    $("avail").innerHTML = meta.hours.map((h) => `<i class="${set.has(h) ? "ok" : ""}"></i>`).join("");
    $("hour").value = S.hour;
    if (!run) return;
    const t0 = runDate(run), valid = new Date(t0.getTime() + S.hour * 3600e3);
    $("valid-time").textContent = fmtLocal(valid);
    $("valid-sub").textContent = `${fmtUTC(valid)} · T+${String(S.hour).padStart(2, "0")}h`;
    // day boundaries in local time
    const ticks = [];
    for (let h = 1; h <= 84; h++) {
      const d = new Date(t0.getTime() + h * 3600e3);
      if (d.getHours() === 0) ticks.push(`<span style="left:${(h / 84) * 100}%">${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })}</span>`);
    }
    $("day-ticks").innerHTML = ticks.join("");
    syncChartCursor();
  }

  function setRun(run) {
    const keep = S.run ? validMs() : Date.now();
    S.run = run;
    S.hour = hourAt(run, keep);
    S.followLatest = run === pickDefaultRun();
    $("run-select").value = run;
    snapHour(); updateBanner(); updateTimebar(); render();
    if (S.point) loadPoint(S.point.lat, S.point.lng);
  }
  function setHour(h) {
    const hrs = availableHours();
    if (!hrs.length) return;
    if (!hrs.includes(h)) h = hrs.reduce((a, b) => (Math.abs(b - h) < Math.abs(a - h) ? b : a));
    S.hour = h; updateTimebar(); render();
  }
  function step(dir) {
    const hrs = availableHours(), i = hrs.indexOf(S.hour);
    if (!hrs.length) return;
    setHour(hrs[(i + dir + hrs.length) % hrs.length]);
  }

  // ---- legend ---------------------------------------------------------------
  function drawLegend() {
    const ramp = RAMPS[S.layer], spec = meta.variables[S.layer];
    $("legend-title").textContent = spec.units ? `${spec.label} (${spec.units})` : `${spec.label}${ramp.classes ? " · CWFIS classes" : ""}`;
    const c = $("legend-bar"), ctx = c.getContext("2d"), w = c.width;
    if (ramp.classes) {
      const n = ramp.classes.length, bw = w / n;
      ctx.clearRect(0, 0, w, c.height);
      ramp.classes.forEach(([, col], i) => { ctx.fillStyle = col; ctx.fillRect(i * bw, 0, bw - 1, c.height); });
      $("legend-ticks").innerHTML = ramp.classes.map(([, , lab], i) => `<span style="left:${((i + 0.5) / n) * 100}%">${lab}</span>`).join("");
      return;
    }
    const toV = (t) => ramp.log ? Math.exp(Math.log(0.1) + t * (Math.log(ramp.max) - Math.log(0.1))) : ramp.min + t * (ramp.max - ramp.min);
    const toT = (v) => ramp.log ? (Math.log(v) - Math.log(0.1)) / (Math.log(ramp.max) - Math.log(0.1)) : (v - ramp.min) / (ramp.max - ramp.min);
    ctx.clearRect(0, 0, w, c.height);
    for (let x = 0; x < w; x++) {
      const [r, g, b, a] = rampColor(ramp, toV(x / (w - 1)));
      ctx.fillStyle = `rgba(${r},${g},${b},${Math.max(a, 0.15)})`;
      ctx.fillRect(x, 0, 1, c.height);
    }
    $("legend-ticks").innerHTML = ramp.ticks.map((t) => `<span style="left:${toT(t) * 100}%">${t}</span>`).join("");
  }

  // ---- variable tabs --------------------------------------------------------
  $("vars").innerHTML = LAYERS.map((l, i) =>
    (i === 0 || LAYERS[i - 1].group !== l.group ? `<span class="group">${l.group}</span>` : "") +
    `<button role="tab" data-v="${l.id}">${l.name}</button>`).join("");
  function setLayer(v) {
    S.layer = v; savePrefs();
    for (const b of $("vars").querySelectorAll("button")) b.setAttribute("aria-selected", b.dataset.v === v);
    snapHour(); updateTimebar(); drawLegend(); render();
  }
  $("vars").onclick = (e) => e.target.dataset.v && setLayer(e.target.dataset.v);

  // ---- hover readout --------------------------------------------------------
  let hoverLL = null, hoverXY = null;
  map.on("mousemove", (e) => { hoverLL = e.latlng; hoverXY = e.containerPoint; updateReadout(); });
  map.on("mouseout", () => { hoverLL = null; $("readout").hidden = true; });
  async function updateReadout() {
    const ro = $("readout");
    if (!hoverLL || !S.run) return;
    const i = latLonToPx(hoverLL.lat, hoverLL.lng);
    if (i < 0) { ro.hidden = true; return; }
    const run = S.run, h = S.hour;
    const names = READOUT_VARS;
    const frames = await Promise.all(names.map((v) => getFrame(run, v, h)));
    const vals = Object.fromEntries(names.map((v, k) => [v, frames[k]]));
    if (run !== S.run || h !== S.hour || !hoverLL) return;
    const g = (v, d = 0) => (vals[v] && vals[v][i] ? decodeVal(v, vals[v][i]).toFixed(d) : "—");
    const dir = vals.wdir && vals.wdir[i] ? decodeVal("wdir", vals.wdir[i]) : null;
    ro.innerHTML = `
      <div class="hd">${hoverLL.lat.toFixed(2)}°, ${hoverLL.lng.toFixed(2)}° · click for forecast</div>
      <span class="k">Temp</span><span class="v">${g("temp", 1)} °C</span>
      <span class="k">RH</span><span class="v">${g("rh")} %</span>
      <span class="k">Wind</span><span class="v">${dir != null ? COMPASS[Math.round(dir / 22.5) % 16] + " " : ""}${g("wspd")} km/h</span>
      <span class="k">Direction</span><span class="v">${dir != null ? Math.round(dir) + "°" : "—"}</span>
      <span class="k">Precip (accum.)</span><span class="v">${g("precip", 1)} mm</span>
      <span class="sep"></span>
      <span class="k">FFMC</span><span class="v">${g("ffmc", 1)}</span>
      <span class="k">ISI</span><span class="v">${g("isi", 1)}</span>
      <span class="k">BUI</span><span class="v">${g("bui", 1)}</span>
      <span class="k">FWI</span><span class="v">${badge("fwi", vals.fwi && vals.fwi[i] ? decodeVal("fwi", vals.fwi[i]) : NaN)}</span>`;
    ro.hidden = false;
    const size = map.getSize(), rw = ro.offsetWidth, rh = ro.offsetHeight;
    let x = hoverXY.x + 16, y = hoverXY.y + 16;
    if (x + rw > size.x - 8) x = hoverXY.x - rw - 16;
    if (y + rh > size.y - 90) y = hoverXY.y - rh - 16;
    ro.style.left = x + "px"; ro.style.top = y + "px";
  }

  // ---- meteogram ------------------------------------------------------------
  Chart.defaults.color = "#9aa5b1";
  Chart.defaults.borderColor = "rgba(255,255,255,.07)";
  Chart.defaults.font.family = getComputedStyle(document.documentElement).fontFamily;
  Chart.defaults.font.size = 11;
  const charts = {};
  const arrowImg = (() => {
    const c = document.createElement("canvas"); c.width = c.height = 16;
    const x = c.getContext("2d"); x.strokeStyle = "#e8ecf1"; x.lineWidth = 1.6; x.lineCap = "round";
    x.beginPath(); x.moveTo(8, 14); x.lineTo(8, 2); x.moveTo(4, 6); x.lineTo(8, 2); x.lineTo(12, 6); x.stroke();
    return c;
  })();
  const cursorPlugin = {
    id: "cursor",
    afterDatasetsDraw(chart) {
      const i = chart.$hours?.indexOf(S.hour);
      if (i == null || i < 0) return;
      const x = chart.scales.x.getPixelForValue(i), { top, bottom } = chart.chartArea, ctx = chart.ctx;
      ctx.save(); ctx.strokeStyle = "rgba(255,138,61,.8)"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
    },
  };
  let mgLocalHours = [];  // local hour of each meteogram point, for the 6-hourly x-axis labels
  let mgValid = [];       // valid time (ISO) of each point, for tooltip titles
  function baseOpts(extra) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { boxWidth: 10, boxHeight: 10 } },
        tooltip: {
          backgroundColor: "#12161c", borderColor: "rgba(255,255,255,.1)", borderWidth: 1,
          callbacks: { title: (items) => fmtLocal(new Date(mgValid[items[0].dataIndex])) },
        },
      },
      scales: {
        x: {
          // label every 6 local hours (00, 06, 12, 18; midnight shows the day) instead of
          // letting Chart.js auto-skip to an arbitrary interval
          ticks: {
            maxRotation: 0, autoSkip: false,
            callback(value, i) { return mgLocalHours[i] % 6 === 0 ? this.getLabelForValue(value) : null; },
          },
          grid: {
            drawTicks: false,
            color: (c) => (mgLocalHours[c.index] === 0 ? "rgba(255,255,255,.14)"
              : mgLocalHours[c.index] % 6 === 0 ? "rgba(255,255,255,.05)" : "transparent"),
          },
        },
        ...extra,
      },
      onClick: (_e, els, chart) => { if (els.length) setHour(chart.$hours[els[0].index]); },
    };
  }
  function syncChartCursor() { for (const c of Object.values(charts)) c.draw(); }

  async function loadPoint(lat, lng) {
    if (!S.run) return;
    const r = await fetch(`/api/point?run=${S.run}&lat=${lat}&lon=${lng}`);
    if (!r.ok) return;
    const d = await r.json();
    if (!S.point || S.point.lat !== lat || S.point.lng !== lng) return;
    $("meteogram").hidden = false;
    $("mg-title").textContent = S.pointName ? `${S.pointName} · ${lat.toFixed(2)}°, ${lng.toFixed(2)}°` : `${lat.toFixed(3)}°, ${lng.toFixed(3)}°`;
    $("mg-sub").textContent = `Nearest grid point ${d.cell_lat}°, ${d.cell_lon}° · RDPS ${runName(d.run)} · local time`;
    $("mg-fire").href = `/fire?lat=${lat.toFixed(5)}&lon=${lng.toFixed(5)}`;
    const aoi = $("aoi").value;
    $("mg-firewx").href = aoi ? `/static/firewx.html?aoi=${aoi}`
      : `/static/firewx.html?lat=${lat.toFixed(4)}&lon=${lng.toFixed(4)}`;
    $("mg-report").href = aoi ? `/static/report.html?aoi=${aoi}&run=${d.run}`
      : `/static/report.html?lat=${lat.toFixed(4)}&lon=${lng.toFixed(4)}&run=${d.run}`;
    mgLocalHours = d.valid.map((t) => new Date(t).getHours());
    mgValid = d.valid;
    // short axis labels (06 / 12 / 18, weekday at midnight); tooltips show the full time
    const labels = d.valid.map((t) => {
      const dt = new Date(t);
      return dt.getHours() === 0 ? dt.toLocaleDateString(undefined, { weekday: "short" }) : String(dt.getHours()).padStart(2, "0");
    });
    const s = d.series;
    const hourly = s.precip.map((v, i) => (i === 0 || v == null || s.precip[i - 1] == null ? null : Math.max(0, +(v - s.precip[i - 1]).toFixed(2))));
    for (const c of Object.values(charts)) c.destroy();

    charts.trh = new Chart($("ch-trh"), {
      type: "line", plugins: [cursorPlugin],
      data: { labels, datasets: [
        { label: "Temp °C", data: s.temp, borderColor: "#f68e3c", backgroundColor: "#f68e3c", yAxisID: "y", pointRadius: 0, borderWidth: 2, tension: 0.3 },
        { label: "RH %", data: s.rh, borderColor: "#5fb7a6", backgroundColor: "#5fb7a6", yAxisID: "y1", pointRadius: 0, borderWidth: 2, tension: 0.3 },
      ] },
      options: baseOpts({ y: { position: "left", title: { display: true, text: "°C" } }, y1: { position: "right", min: 0, max: 100, grid: { display: false }, title: { display: true, text: "%" } } }),
    });
    charts.wind = new Chart($("ch-wind"), {
      type: "line", plugins: [cursorPlugin],
      data: { labels, datasets: [{
        label: "Wind km/h (arrows: direction)", data: s.wspd, borderColor: "#a7d36b", backgroundColor: "#a7d36b",
        borderWidth: 2, tension: 0.3,
        pointStyle: (c) => (c.dataIndex % 3 === 0 ? arrowImg : false),
        rotation: (c) => ((s.wdir[c.dataIndex] ?? 0) + 180) % 360,
        pointRadius: (c) => (c.dataIndex % 3 === 0 ? 8 : 0),
      }, {
        // gusts are fetched hourly to T+48, then 3-hourly: join across the gaps
        label: "Gusts km/h", data: s.gust, borderColor: "rgba(232,236,241,.65)", backgroundColor: "rgba(232,236,241,.65)",
        borderWidth: 1.5, borderDash: [5, 4], tension: 0.3, pointRadius: 0, spanGaps: true,
      }] },
      options: (() => {
        const o = baseOpts({ y: { min: 0, suggestedMax: 30, title: { display: true, text: "km/h" } } });
        o.plugins.legend.labels.usePointStyle = false;
        Object.assign(o.plugins.tooltip.callbacks, { afterLabel: (c) => {
          if (c.datasetIndex !== 0) return "";
          const dd = s.wdir[c.dataIndex]; return dd == null ? "" : `From ${COMPASS[Math.round(dd / 22.5) % 16]} (${Math.round(dd)}°)`;
        } });
        return o;
      })(),
    });
    charts.fwi = new Chart($("ch-fwi"), {
      type: "line", plugins: [cursorPlugin],
      data: { labels, datasets: [
        { label: "FWI", data: s.fwi, borderColor: "#ff6a3d", backgroundColor: "#ff6a3d", pointRadius: 0, borderWidth: 2, tension: 0.3, yAxisID: "y" },
        { label: "ISI", data: s.isi, borderColor: "#f5e33b", backgroundColor: "#f5e33b", pointRadius: 0, borderWidth: 1.5, tension: 0.3, yAxisID: "y" },
        { label: "FFMC", data: s.ffmc, borderColor: "#8fb7ff", backgroundColor: "#8fb7ff", pointRadius: 0, borderWidth: 1.5, borderDash: [4, 3], tension: 0.3, yAxisID: "y1" },
      ] },
      options: (() => {
        const o = baseOpts({ y: { min: 0, suggestedMax: 10, title: { display: true, text: "FWI / ISI" } }, y1: { position: "right", min: 0, max: 101, grid: { display: false }, title: { display: true, text: "FFMC" } } });
        Object.assign(o.plugins.tooltip.callbacks, { footer: (items) => {
          const k = items[0].dataIndex, f = (v) => (v == null ? "—" : v.toFixed(1));
          return `BUI ${f(s.bui[k])} · DMC ${f(s.dmc[k])} · DC ${f(s.dc[k])} · GFWI ${f(s.gfwi[k])}`;
        } });
        return o;
      })(),
    });
    charts.precip = new Chart($("ch-precip"), {
      data: { labels, datasets: [
        { type: "bar", label: "Hourly mm", data: hourly, backgroundColor: "rgba(63,174,85,.7)", yAxisID: "y" },
        { type: "line", label: "Accumulated mm", data: s.precip, borderColor: "#5f9fe6", backgroundColor: "#5f9fe6", pointRadius: 0, borderWidth: 2, yAxisID: "y1" },
      ] },
      plugins: [cursorPlugin],
      options: baseOpts({ y: { min: 0, suggestedMax: 1, title: { display: true, text: "mm/h" } }, y1: { position: "right", min: 0, suggestedMax: 5, grid: { display: false }, title: { display: true, text: "mm" } } }),
    });
    for (const c of Object.values(charts)) c.$hours = d.hours;
  }
  function selectPoint(latlng, name = null) {
    S.point = L.latLng(latlng);
    S.pointName = name;
    if (pointMarker) pointMarker.setLatLng(S.point);
    else pointMarker = L.circleMarker(S.point, { radius: 6, color: "#ff8a3d", weight: 2, fillColor: "#ff8a3d", fillOpacity: 0.3 }).addTo(map);
    pointMarker.unbindTooltip();
    if (name) pointMarker.bindTooltip(name, { permanent: true, direction: "right", className: "aoi-label" });
    loadPoint(S.point.lat, S.point.lng);
  }
  map.on("click", (e) => {
    $("aoi").value = "";
    selectPoint(e.latlng);
  });

  // ---- areas of interest -------------------------------------------------------
  let AOIS = [];
  function selectAOI(id, fly = true) {
    const a = AOIS.find((x) => x.id === id);
    if (!a) return;
    $("aoi").value = id;
    try { localStorage.setItem("fw-aoi", id); } catch (_) {}
    if (fly) map.setView([a.lat, a.lon], 8);
    selectPoint([a.lat, a.lon], a.name);
  }
  $("aoi").onchange = (e) => (e.target.value ? selectAOI(e.target.value) : null);
  async function loadAOIs() {
    try {
      const d = await (await fetch("/api/aoi")).json();
      AOIS = d.areas;
      $("aoi").innerHTML = `<option value="">Custom point</option>` +
        AOIS.map((a) => `<option value="${a.id}">${a.name}${a.prov ? ` (${a.prov})` : ""}</option>`).join("");
      let saved = null;
      try { saved = localStorage.getItem("fw-aoi"); } catch (_) {}
      return AOIS.some((a) => a.id === saved) ? saved : d.default;
    } catch (_) {
      return null;
    }
  }
  $("mg-close").onclick = () => {
    $("meteogram").hidden = true; S.point = null;
    if (pointMarker) { pointMarker.remove(); pointMarker = null; }
  };

  // ---- controls -------------------------------------------------------------
  $("hour").oninput = (e) => setHour(+e.target.value);
  $("run-select").onchange = (e) => setRun(e.target.value);
  $("wind-mode").value = S.wind;
  $("wind-mode").onchange = (e) => { S.wind = e.target.value; savePrefs(); wind.setMode(S.wind); render(); };
  $("opacity").value = S.opacity;
  $("opacity").oninput = (e) => { S.opacity = +e.target.value; overlay.setOpacity(S.opacity); savePrefs(); };
  let playTimer = null;
  function setPlaying(on) {
    S.playing = on;
    $("play").classList.toggle("on", on);
    $("play").textContent = on ? "❚❚" : "▶";
    clearInterval(playTimer);
    if (on) playTimer = setInterval(() => step(1), 450);
    else prefetch();
  }
  $("play").onclick = () => setPlaying(!S.playing);
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT" || (e.target.tagName === "INPUT" && e.target.type !== "range")) return;
    if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
    else if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
    else if (e.key === " ") { setPlaying(!S.playing); e.preventDefault(); }
  });

  // ---- live updates (server pushes as AMQP-triggered downloads finish) -------
  let refreshPending = null;
  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onopen = () => { $("live-dot").className = "dot live"; $("live-dot").title = "Live: listening for new RDPS data"; };
    es.onerror = () => { $("live-dot").className = "dot down"; $("live-dot").title = "Live feed disconnected, retrying…"; };
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      clearTimeout(refreshPending);
      refreshPending = setTimeout(async () => {
        const before = availableHours().length, prevRun = S.run;
        await loadRuns();
        if (S.run !== prevRun || (before === 0 && availableHours().length)) render();
        if (S.point && ev.run === S.run && ev.complete) loadPoint(S.point.lat, S.point.lng);
      }, 800);
    };
  }

  // ---- go -------------------------------------------------------------------
  setLayer(S.layer in RAMPS ? S.layer : "rh");
  const startAOI = await loadAOIs();
  await loadRuns();
  S.followLatest = true;
  render();
  if (startAOI) selectAOI(startAOI);
  connectEvents();
})();
