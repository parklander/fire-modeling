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
    // cloud: like a satellite picture on the dark base, clear transparent to overcast near-white
    cloud: { min: 0, max: 100, stops: [
      [0, "#9aa5b1", 0], [10, "#c9d1da", 0.08], [30, "#d9dee5", 0.3], [60, "#e6eaef", 0.58], [85, "#f1f4f7", 0.8],
      [100, "#ffffff", 0.9]], ticks: [0, 25, 50, 75, 100] },
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
  // Continuous alternative for the FWI-system class ramps: the same CWFIS colours, each placed at the middle of its
  // class and blended in between; above the top class it deepens to dark red. [min, max] of the ramp per index.
  const CONT_RANGE = { fwi: [0, 50], isi: [0, 25], ffmc: [50, 100], bui: [0, 120], dmc: [0, 100], dc: [0, 700] };
  const CONT = {};
  function continuousOf(v) {
    if (CONT[v]) return CONT[v];
    const [min, max] = CONT_RANGE[v], cl = RAMPS[v].classes, n = cl.length;
    const up = cl.map(([u]) => u), lastFinite = up[n - 2], step = up[n - 2] - up[n - 3];
    const stops = [[min, cl[0][1]]];
    cl.forEach(([u, col], i) => {
      const lo = i === 0 ? min : up[i - 1], hi = u === Infinity ? lastFinite + step : u;
      stops.push([(lo + hi) / 2, col]);
    });
    stops.push([max, "#7a0a1a"]);
    return (CONT[v] = { min, max, stops, ticks: [min, ...up.slice(0, n - 1), max], continuous: true });
  }
  const hasScaleChoice = (v) => v in CONT_RANGE;
  // the ramp the map uses for a layer: CWFIS classes, or the continuous version when chosen
  const rampFor = (v) => (S.fwiScale === "continuous" && hasScaleChoice(v) ? continuousOf(v) : RAMPS[v]);

  // Precipitation rate (mm/h) coloured by type: one intensity ramp per type, transparent where dry.
  // Index = type code from the server: 1 rain, 2 rain/snow mix, 3 freezing rain, 4 ice pellets, 5 snow.
  const PTYPE = [
    null,
    { key: "Rain", stops: [[0.05, "#9ec9ea", 0.35], [0.5, "#4a90d9", 0.8], [2, "#2a4fc8"], [5, "#a23bc4"], [10, "#f2c94c"], [25, "#ff4a3d"]] },
    { key: "Mix", stops: [[0.05, "#f3c4e8", 0.4], [0.5, "#e590d6", 0.85], [2, "#cc4fbd"], [5, "#8e2386"]] },
    { key: "Frz rain", stops: [[0.05, "#ffb3a8", 0.5], [0.5, "#ff7a6b", 0.9], [2, "#e5322a"], [5, "#a30d0d"]] },
    { key: "Pellets", stops: [[0.05, "#ffd9a0", 0.5], [0.5, "#ffb760", 0.9], [2, "#f08a1c"], [5, "#c25e00"]] },
    { key: "Snow", stops: [[0.05, "#eefaff", 0.4], [0.5, "#c4efff", 0.85], [2, "#7fd6f2"], [5, "#2fb0d6"]] },
  ];
  const PT_MID = PTYPE.map((p) => p && p.stops[2][1]);  // one colour per type for chart bars
  let PT_LUTS = null;
  function precipLuts() {  // per type: prate uint16 code -> RGBA
    if (PT_LUTS) return PT_LUTS;
    const spec = meta.variables.prate;
    PT_LUTS = PTYPE.map((p) => {
      const out = new Uint32Array(65536);
      if (!p) return out;  // type 0 / nodata: transparent
      const ramp = { stops: p.stops };
      for (let q = 1; q < 65536; q++) {
        const v = (q - 1) / spec.scale + spec.offset;
        if (v < 0.05) continue;
        const [r, g, b, a] = rampColor(ramp, Math.min(v, 30));
        out[q] = ((Math.round(a * 255) << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r)) >>> 0;
      }
      return out;
    });
    return PT_LUTS;
  }
  const ptName = (t) => meta.ptype_names?.[t] ?? "—";

  // ---- GOES-East satellite layers (ECCC GeoMet WMS): observed images, 1 km, every 10 min, ~2 days kept ----------
  const SAT = {
    sat_fire: { key: "fire", title: "GOES-East fire temperature",
      desc: "<b>Active fires</b> show as red (small or cooler) through orange and yellow to near-white (large, very hot). "
        + "Surfaces and clouds are muted. By day an RGB of shortwave-infrared bands; at night shortwave infrared, where fires are bright spots." },
    sat_smoke: { key: "smoke", title: "GOES-East smoke",
      desc: "By day the blue band, which smoke scatters strongly: <b>smoke shows as a grey-white haze</b>, thicker smoke brighter; "
        + "cloud is also bright but sharper-edged and textured. At night shortwave infrared (smoke isn't visible at night)." },
  };
  for (const k of Object.keys(SAT)) RAMPS[k] = { sat: true };
  const isSat = (v) => v in SAT;
  let GOES = null, satLayer = null;
  async function loadGoes() {
    try { GOES = await (await fetch("/api/goes")).json(); } catch (_) { return; }
    if (isSat(S.layer)) updateSat();
  }
  // the image time for the displayed hour: on GeoMet's 10-min grid, within its window (forecast hours: the latest)
  function satTime() {
    const t = GOES?.layers?.[SAT[S.layer].key];
    if (!t) return null;
    const step = t.step_min * 60e3, start = Date.parse(t.start), end = Date.parse(t.end);
    const want = Math.floor(validMs() / step) * step;
    const ms = Math.max(start, Math.min(end, want));
    return { iso: new Date(ms).toISOString().replace(".000Z", "Z"), latest: want >= end, oldest: want < start, layer: t.layer };
  }
  function updateSat() {
    if (!isSat(S.layer)) { if (satLayer) { satLayer.remove(); satLayer = null; } return; }
    const st = satTime();
    if (!st) { $("legend-desc").innerHTML = SAT[S.layer].desc + "<br><i>Satellite image times unavailable.</i>"; return; }
    if (!satLayer || satLayer.options.layers !== st.layer) {
      if (satLayer) satLayer.remove();
      satLayer = L.tileLayer.wms(GOES.wms, { layers: st.layer, format: "image/png", transparent: true, version: "1.3.0",
        time: st.iso, opacity: S.opacity, zIndex: 5, attribution: "GOES-East imagery: ECCC GeoMet" }).addTo(map);
    } else if (satLayer.wmsParams.time !== st.iso) {
      satLayer.setParams({ time: st.iso });
    }
    const when = new Date(st.iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    $("legend-desc").innerHTML = `${SAT[S.layer].desc}<br><b>Image ${when}</b>`
      + (st.latest ? " (latest available: forecast hours show the most recent image)" : st.oldest ? " (oldest kept: about 2 days)" : "");
  }

  const LAYERS = [
    { id: "temp", name: "Temperature", group: "Weather" },
    { id: "rh", name: "Humidity", group: "Weather" },
    { id: "wspd", name: "Wind", group: "Weather" },
    { id: "precip", name: "Precip (accum.)", group: "Weather" },
    { id: "prate", name: "Precip rate", group: "Weather" },
    { id: "cloud", name: "Cloud cover", group: "Weather" },
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
    { id: "sat_fire", name: "Fire temperature", group: "Satellite (GOES-East)" },
    { id: "sat_smoke", name: "Smoke", group: "Satellite (GOES-East)" },
  ];
  const READOUT_VARS = ["temp", "rh", "wspd", "wdir", "precip", "prate", "ptype", "cloud", "ffmc", "isi", "bui", "fwi"];
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
    wind: "particles", opacity: 0.75, point: null, followLatest: true, fwiScale: "classes", synoptic: false, synKey: false,
  };
  try { Object.assign(S, JSON.parse(localStorage.getItem("fw-prefs") || "{}"), { playing: false, point: null }); } catch (_) {}
  if (!["particles", "arrows", "off"].includes(S.wind)) S.wind = "particles";
  if (S.wind === "particles" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) S.wind = "arrows";
  const savePrefs = () => { try { localStorage.setItem("fw-prefs", JSON.stringify({ layer: S.layer, wind: S.wind, opacity: S.opacity, fwiScale: S.fwiScale, synoptic: S.synoptic, synKey: S.synKey, firms: S.firms })); } catch (_) {} };

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
    const ramp = rampFor(v), key = ramp.continuous ? `${v}:cont` : v;
    if (LUTS[key]) return LUTS[key];
    const spec = meta.variables[v];
    const out = new Uint32Array(65536);
    const top = ramp.classes ? Infinity : ramp.stops.at(-1)[0];
    const maxCode = Math.min(65535, Math.ceil((top - spec.offset) * spec.scale) + 1);
    for (let q = 1; q < 65536; q++) {
      const val = q <= maxCode ? (q - 1) / spec.scale + spec.offset : top;
      const [r, g, b, a] = rampColor(ramp, val);
      out[q] = ((Math.round(a * 255) << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r)) >>> 0;
    }
    return (LUTS[key] = out);
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

  // ---- FIRMS hotspots overlay (same styling as the fire growth page): above any layer, wind and synoptic -----
  map.createPane("hotspots").style.zIndex = 470;  // above the wind (450) and synoptic (455) canvases, below labels
  const hotRenderer = L.canvas({ pane: "hotspots", padding: 0.3 });
  const hotLayer = L.layerGroup().addTo(map);
  const hoursAgo = (iso) => (Date.now() - Date.parse(iso)) / 3600e3;
  const ageColour = (iso) => (hoursAgo(iso) < 6 ? "#ff3b1f" : hoursAgo(iso) < 24 ? "#ff9f1c" : "#ffd166");
  async function loadHotspots() {
    hotLayer.clearLayers();
    $("firms-note").hidden = !S.firms;
    if (!S.firms) return;
    let d;
    try { d = await (await fetch("/api/hotspots?hours=48")).json(); } catch (_) { return; }
    if (!S.firms) return;
    const clock = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    let fresh = 0;
    for (const h of d.detections) {
      if (!h.persistent && !h.low_conf) fresh++;
      L.circleMarker([h.lat, h.lon], {
        renderer: hotRenderer, pane: "hotspots", radius: h.persistent ? 2.5 : 4, weight: 1,
        color: h.persistent ? "#8a939c" : "#2b0d00", fillColor: h.persistent ? "#8a939c" : ageColour(h.time),
        fillOpacity: h.low_conf ? 0.25 : 0.9,
      }).bindTooltip(`${h.sensor} · ${clock(h.time)} · FRP ${h.frp == null ? "—" : h.frp.toFixed(1)} MW`
        + (h.persistent ? " · persistent heat source (excluded)" : "") + (h.low_conf ? " · low confidence" : ""))
        .addTo(hotLayer);
    }
    const st = d.status || {};
    $("firms-note").innerHTML = `<b>FIRMS</b> ${fresh} detections, last 48 h`
      + `${st.last_poll ? ` · polled ${new Date(st.last_poll).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}` : ""}<br>`
      + `<i class="hs-dot" style="background:#ff3b1f"></i>&lt; 6 h<i class="hs-dot" style="background:#ff9f1c"></i>&lt; 24 h`
      + `<i class="hs-dot" style="background:#ffd166"></i>older<i class="hs-dot" style="background:#8a939c;border-color:#8a939c"></i>persistent (industrial)`;
  }
  // ---- synoptic overlay: RDPS isobars + H/L every hour, WPC fronts at their valid times ----------
  const syn = SynopticLayer(map, $("synoptic"), meta);
  let FRONTS = null, synKey = null;
  async function loadFronts() {
    try { FRONTS = await (await fetch("/api/fronts")).json(); } catch (_) { return; }
    updateSynoptic();
  }
  // the WPC set (analysis or day 1-3 forecast) valid within 3 h of the displayed time, if any
  function frontsFor(ms) {
    if (!FRONTS) return null;
    const sets = [FRONTS.analysis && { ...FRONTS.analysis, kind: "analysis", window: 3 },
      ...(FRONTS.forecast || []).map((f) => ({ ...f, kind: "forecast", window: f.approx_time ? 4 : 3 }))]
      .filter((x) => x && x.valid && x.fronts?.length);
    let best = null;
    for (const x of sets) {
      const dh = Math.abs(new Date(x.valid).getTime() - ms) / 3600e3;
      if (dh <= x.window && (!best || dh < best.dh)) best = { ...x, dh };
    }
    return { best, sets };
  }
  async function updateSynoptic() {
    const note = $("syn-note");
    syn.setVisible(S.synoptic);
    note.hidden = !S.synoptic;
    if (!S.synoptic || !S.run) return;
    const run = S.run, h = S.hour, key = `${run}/${h}`;
    const q = await getFrame(run, "mslp", h);
    if (run !== S.run || h !== S.hour) return;
    if (key !== synKey) { synKey = q ? key : null; syn.setPressure(q); }  // retry later if not downloaded yet
    const ff = frontsFor(validMs());
    syn.setFronts(ff?.best || null);
    const when = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const avail = (ff?.sets || []).map((x) => (x.kind === "analysis" ? `analysis ${when(x.valid)}` : `day ${x.day} ${x.approx_time ? new Date(x.valid).toLocaleDateString(undefined, { weekday: "short" }) : when(x.valid)}`));
    note.innerHTML = `<b>Isobars</b> every 4 hPa, H/L: RDPS sea-level pressure${q ? "" : " (not available for this run)"}<br>`
      + (ff?.best ? `<b>Fronts</b>: ${ff.best.kind === "analysis" ? "WPC surface analysis" : `WPC day ${ff.best.day} forecast`}, valid ${ff.best.approx_time ? new Date(ff.best.valid).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + " (time not given)" : when(ff.best.valid)}`
        : `<b>Fronts</b>: none within 3 h of this time${avail.length ? ` (WPC: ${avail.join(", ")})` : ""}`);
  }
  // the Key button (explains isobars, H/L and front symbols) shows with the overlay; open/closed is remembered
  function syncKey() {
    $("syn-key-btn").hidden = !S.synoptic;
    $("syn-key").hidden = !(S.synoptic && S.synKey);
    $("syn-key-btn").setAttribute("aria-expanded", String(!$("syn-key").hidden));
  }
  $("syn-key-btn").onclick = () => { S.synKey = !S.synKey; savePrefs(); syncKey(); };
  $("syn-key-close").onclick = () => { S.synKey = false; savePrefs(); syncKey(); };
  $("synoptic-on").checked = S.synoptic;
  $("synoptic-on").onchange = (e) => { S.synoptic = e.target.checked; savePrefs(); syncKey(); if (S.synoptic && !FRONTS) loadFronts(); updateSynoptic(); };
  syncKey();

  // ---- rendering ------------------------------------------------------------
  let renderToken = 0;
  async function render() {
    const tok = ++renderToken, run = S.run, h = S.hour, layer = S.layer;
    if (!run) return;
    const [sv, dv] = windVars(), showWind = S.wind !== "off";
    const sat = isSat(layer);
    const [q, spd, dir, qt] = await Promise.all([
      sat ? null : getFrame(run, layer, h),
      showWind ? getFrame(run, sv, h) : null,
      showWind ? getFrame(run, dv, h) : null,
      layer === "prate" ? getFrame(run, "ptype", h) : null,
    ]);
    if (tok !== renderToken) return;
    if (q && layer === "prate" && qt) {
      const T = precipLuts();  // ptype code q-1 picks the ramp
      for (let i = 0; i < q.length; i++) { const t = qt[i] - 1; img32[i] = t > 0 && t < 6 ? T[t][q[i]] : 0; }
      fctx.putImageData(img, 0, 0);
    } else if (q && layer !== "prate") {
      const L_ = lut(layer);
      for (let i = 0; i < q.length; i++) img32[i] = L_[q[i]];
      fctx.putImageData(img, 0, 0);
    } else {
      fctx.clearRect(0, 0, W, H);
    }
    wind.setFrames(spd, dir, sv, dv);
    updateSat();
    updateSynoptic();
    updateReadout();
    prefetch();
  }
  function prefetch() {
    const run = S.run, hrs = availableHours();
    const idx = hrs.indexOf(S.hour);
    for (let k = 1; k <= 4 && idx >= 0; k++) {
      const h = hrs[(idx + k) % hrs.length];
      if (!isSat(S.layer)) getFrame(run, S.layer, h);
      if (S.layer === "prate") getFrame(run, "ptype", h);
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
  const availableHours = () => (S.layer === "prate" ? currentRun()?.precip_hours : S.layer === "cloud" ? currentRun()?.cloud_hours : isIndex(S.layer) ? currentRun()?.index_hours
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
    const sat = isSat(S.layer);
    $("legend-bar").hidden = sat; $("legend-ticks").hidden = sat; $("legend-desc").hidden = !sat;
    if (sat) {
      $("legend-title").textContent = SAT[S.layer].title;
      $("legend-scale").hidden = true;
      updateSat();
      return;
    }
    if (S.layer === "prate") {  // one gradient segment per precipitation type
      $("legend-title").textContent = "Precip rate by type · each 0.1→10 mm/h";
      $("legend-scale").hidden = true;
      const c = $("legend-bar"), ctx = c.getContext("2d"), w = c.width, n = 5, bw = w / n;
      ctx.clearRect(0, 0, w, c.height);
      for (let k = 1; k <= n; k++) {
        for (let x = 0; x < bw - 2; x++) {
          const v = Math.exp(Math.log(0.1) + (x / (bw - 3)) * (Math.log(10) - Math.log(0.1)));
          const [r, g, b, a] = rampColor({ stops: PTYPE[k].stops }, v);
          ctx.fillStyle = `rgba(${r},${g},${b},${Math.max(a, 0.35)})`;
          ctx.fillRect((k - 1) * bw + x, 0, 1, c.height);
        }
      }
      $("legend-ticks").innerHTML = PTYPE.slice(1).map((p, i) => `<span style="left:${((i + 0.5) / n) * 100}%">${p.key}</span>`).join("");
      return;
    }
    const ramp = rampFor(S.layer), spec = meta.variables[S.layer];
    $("legend-title").textContent = spec.units ? `${spec.label} (${spec.units})`
      : `${spec.label}${ramp.classes ? " · CWFIS classes" : ramp.continuous ? " · continuous" : ""}`;
    const sw = $("legend-scale");
    sw.hidden = !hasScaleChoice(S.layer);
    for (const b of sw.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.scale === (S.fwiScale || "classes"));
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
    `<button role="tab" data-v="${l.id}">${l.name}</button>`).join("")
    + `<span class="group">Overlays</span><label class="ov" title="NASA FIRMS satellite fire detections (VIIRS, MODIS), last 48 h">`
    + `<input type="checkbox" id="ov-firms"> FIRMS hotspots (48 h)</label>`;
  $("ov-firms").checked = !!S.firms;
  $("ov-firms").onchange = (e) => { S.firms = e.target.checked; savePrefs(); loadHotspots(); };
  if (S.firms) loadHotspots();
  function setLayer(v) {
    S.layer = v; savePrefs();
    for (const b of $("vars").querySelectorAll("button")) b.setAttribute("aria-selected", b.dataset.v === v);
    snapHour(); updateTimebar(); drawLegend(); render();
  }
  $("vars").onclick = (e) => e.target.dataset.v && setLayer(e.target.dataset.v);
  $("legend-scale").onclick = (e) => {
    const sc = e.target.dataset?.scale;
    if (!sc || sc === S.fwiScale) return;
    S.fwiScale = sc; savePrefs(); drawLegend(); render();
  };

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
      <span class="k">Precip rate</span><span class="v">${(() => {
        const r = vals.prate && vals.prate[i] ? decodeVal("prate", vals.prate[i]) : null;
        const t = vals.ptype && vals.ptype[i] ? Math.round(decodeVal("ptype", vals.ptype[i])) : null;
        if (r == null) return "—";
        return t ? `<i class="badge" style="background:${PT_MID[t]}"></i>${ptName(t)} ${r.toFixed(1)} mm/h` : "None";
      })()}</span>
      <span class="k">Cloud</span><span class="v">${g("cloud")} %</span>
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
  // Every chart gets a left and a right axis of the same fixed width, so the plot areas (and their time axes) line
  // up down the panel; a chart with only a left axis gets a blank right one.
  const AXIS_W = 48;
  const fixW = (s) => { s.width = AXIS_W; };
  function sideAxes(extra) {
    const out = { ...extra };
    out.y = { ...(out.y || {}), afterFit: fixW };
    out.y1 = out.y1 ? { ...out.y1, afterFit: fixW }
      : { position: "right", afterFit: fixW, grid: { display: false }, border: { display: false }, ticks: { display: false } };
    return out;
  }
  function baseOpts(extra) {
    extra = sideAxes(extra);
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
    // front watch: nearest WPC front and its motion, RDPS frontal wind shifts here (filled in when it arrives)
    fetch(`/api/frontwatch/point?lat=${lat}&lon=${lng}`).then((r) => (r.ok ? r.json() : null)).then((fw) => {
      if (!S.point || S.point.lat !== lat || S.point.lng !== lng) return;
      $("mg-front").innerHTML = fw ? frontWatchHtml(fw, { compact: true }) : "";
      $("mg-front").hidden = !fw;
    }).catch(() => { $("mg-front").hidden = true; });
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
    const hourly = s.precip.map((v, i) => (s.prate?.[i] != null ? s.prate[i]
      : i === 0 || v == null || s.precip[i - 1] == null ? null : Math.max(0, +(v - s.precip[i - 1]).toFixed(2))));
    const barColour = hourly.map((v, i) => { const t = Math.round(s.ptype?.[i] ?? 0); return t > 0 ? PT_MID[t] : "rgba(63,174,85,.7)"; });
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
        { type: "bar", label: "Hourly mm (colour = type)", data: hourly, backgroundColor: barColour, yAxisID: "y" },
        { type: "line", label: "Accumulated mm", data: s.precip, borderColor: "#5f9fe6", backgroundColor: "#5f9fe6", pointRadius: 0, borderWidth: 2, yAxisID: "y1" },
      ] },
      plugins: [cursorPlugin],
      options: (() => {
        const o = baseOpts({ y: { min: 0, suggestedMax: 1, title: { display: true, text: "mm/h" } }, y1: { position: "right", min: 0, suggestedMax: 5, grid: { display: false }, title: { display: true, text: "mm" } } });
        Object.assign(o.plugins.tooltip.callbacks, { footer: (items) => {
          const t = Math.round(s.ptype?.[items[0].dataIndex] ?? 0);
          return t > 0 ? ptName(t) : "";
        } });
        return o;
      })(),
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
  let aoiOutline = null;  // the selected area's boundary, if it has one
  function selectAOI(id, fly = true) {
    const a = AOIS.find((x) => x.id === id);
    if (!a) return;
    $("aoi").value = id;
    try { localStorage.setItem("fw-aoi", id); } catch (_) {}
    if (aoiOutline) { aoiOutline.remove(); aoiOutline = null; }
    if (a.boundary) {
      aoiOutline = L.polygon(a.boundary, { color: "#ff8a3d", weight: 2, fill: false, dashArray: "6 4", interactive: false }).addTo(map);
    }
    if (fly) map.setView([a.lat, a.lon], 8);
    selectPoint([a.lat, a.lon], a.name);
    renderAlerts();
  }
  $("aoi").onchange = (e) => {
    if (e.target.value === "__edit") { location.href = "/static/aoi.html"; return; }
    if (e.target.value) selectAOI(e.target.value);
    else { if (aoiOutline) { aoiOutline.remove(); aoiOutline = null; } renderAlerts(); }
  };
  // ---- ECCC public alerts at the bases (server polls battleboard feeds / GeoMet) ----
  const LVL = { warning: 3, watch: 2, statement: 1 };
  const escH = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const when = (iso) => new Date(iso).toLocaleString("en-CA", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  let ALERTS = null, alertsDismissed = null, FRONT_ALERTS = [];
  const alertKey = (a, x) => `${a.id}|${x.name}`;
  const worst = (list) => list.reduce((w, x) => (LVL[x.level] > LVL[w] ? x.level : w), "statement");
  function renderAlerts() {
    if (!ALERTS) return;
    const active = ALERTS.areas.filter((a) => a.alerts.length);
    const fa = FRONT_ALERTS;  // model-based front watch alerts (ours, not ECCC's)
    const chip = $("alert-chip");
    chip.className = "hdr-link alert-chip" + (fa.length ? " on lvl-warning" : active.length ? ` on lvl-${worst(active.flatMap((a) => a.alerts))}` : "");
    chip.textContent = [active.length ? `⚠ Alerts: ${active.length} base${active.length > 1 ? "s" : ""}` : "",
      fa.length ? `⚠ Front: ${[...new Set(fa.map((x) => x.name))].join(", ")}` : ""].filter(Boolean).join(" · ") || "No alerts";
    const sel = ALERTS.areas.find((a) => a.id === $("aoi").value);
    const mine = sel?.alerts || [];
    const key = mine.map((x) => alertKey(sel, x)).join(",");
    // shown when the selected base has alerts (until closed), or when opened from the chip
    const panel = $("alerts");
    const myFront = fa.filter((x) => x.aoi === $("aoi").value);
    const fkey = key + myFront.map((x) => x.event.time).join(",");
    if (!panel.dataset.open && ((!mine.length && !myFront.length) || alertsDismissed === fkey)) { panel.hidden = true; return; }
    const others = active.filter((a) => a !== sel);
    const checked = sel?.checked || ALERTS.areas[0]?.checked;
    const item = (x) => `<div class="wx-alert lvl-${x.level}"><div class="n">${escH(x.name)}</div>
        <div class="sub">${x.issued ? `Issued ${when(x.issued)}` : ""}${x.expires ? ` · expires ${when(x.expires)}` : ""}</div>
        ${x.text ? `<div class="t">${escH(x.text)}</div>` : ""}
        ${x.link || sel.page ? `<div class="sub"><a href="${escH(x.link || sel.page)}" target="_blank" rel="noopener">Full text on weather.gc.ca ↗</a></div>` : ""}</div>`;
    panel.innerHTML = `
      <div class="al-head"><b>ECCC alerts${sel ? ` · ${escH(sel.name)}` : ""}</b><button class="icon" id="al-close" aria-label="Close">×</button></div>
      ${sel ? (mine.length ? mine.map(item).join("") : `<div class="sub">No alerts in effect${sel.region ? ` for ${escH(sel.region)}` : ""}.${sel.page ? ` <a href="${escH(sel.page)}" target="_blank" rel="noopener">Check on weather.gc.ca ↗</a>` : ""}</div>`)
        : `<div class="sub">Pick a base to see its alerts.</div>`}
      ${fa.length ? `<div class="others"><div class="sub">Front watch (RDPS forecast, not an ECCC alert)</div>${fa.map((x) =>
        `<div class="wx-alert lvl-warning"><div class="n">Frontal wind shift · ${escH(x.name)}</div>
          <div class="t">${when(x.event.time)}: ${escH(x.event.from_card)} ${x.event.from_speed} → ${escH(x.event.to_card)} ${x.event.to_speed} km/h, max ${x.event.max_wind}${x.event.max_gust != null ? `, gusts ${x.event.max_gust}` : ""}</div></div>`).join("")}</div>` : ""}
      ${others.length ? `<div class="others"><div class="sub">Other bases</div>${others.map((a) =>
        `<button data-id="${escH(a.id)}" class="lvl-${worst(a.alerts)}"><b>⚠</b> ${escH(a.name)}: ${escH(a.alerts.map((x) => x.name).join(", "))}</button>`).join("")}</div>` : ""}
      <div class="foot"><span class="sub">${sel?.source ? `${escH(sel.source)}${sel.feed ? ` (${escH(sel.feed)})` : ""} · ` : ""}checked ${checked ? new Date(checked).toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false }) : "—"}</span>
        ${"Notification" in window && Notification.permission === "default" ? `<button id="al-notify" title="Show a desktop notification when a new alert is issued for a base (while this page is open)">Notify me</button>` : ""}</div>`;
    panel.hidden = false;
    $("al-close").onclick = () => { delete panel.dataset.open; alertsDismissed = fkey; panel.hidden = true; };
    panel.querySelectorAll(".others button").forEach((b) => (b.onclick = () => selectAOI(b.dataset.id)));
    const nb = $("al-notify");
    if (nb) nb.onclick = () => Notification.requestPermission().then(renderAlerts);
  }
  $("alert-chip").onclick = () => {
    const panel = $("alerts");
    if (panel.hidden) { panel.dataset.open = "1"; renderAlerts(); }
    else { delete panel.dataset.open; panel.hidden = true; }
  };
  async function loadFrontAlerts() {
    let d;
    try { d = await (await fetch("/api/frontalerts")).json(); } catch (_) { return; }
    const seen = new Set(FRONT_ALERTS.map((x) => `${x.id}|${x.event.time}`)), first = loadFrontAlerts.done !== true;
    FRONT_ALERTS = d.active || [];
    loadFrontAlerts.done = true;
    if (!first && "Notification" in window && Notification.permission === "granted") {
      for (const x of FRONT_ALERTS) if (!seen.has(`${x.id}|${x.event.time}`)) {
        new Notification(`Frontal wind shift: ${x.name}`, {
          body: `${when(x.event.time)}: ${x.event.from_card} ${x.event.from_speed} → ${x.event.to_card} ${x.event.to_speed} km/h${x.event.max_gust != null ? `, gusts ${x.event.max_gust}` : ""}`,
          tag: `front|${x.id}|${x.event.time}` });
      }
    }
    renderAlerts();
  }
  async function loadAlerts() {
    let d;
    try { d = await (await fetch("/api/alerts")).json(); } catch (_) { return; }
    const seen = ALERTS && new Set(ALERTS.areas.flatMap((a) => a.alerts.map((x) => alertKey(a, x))));
    ALERTS = d;
    if (seen && "Notification" in window && Notification.permission === "granted") {
      for (const a of d.areas) for (const x of a.alerts) {
        if (!seen.has(alertKey(a, x))) new Notification(`${a.name}: ${x.name}`, { body: (x.text || "").slice(0, 200), tag: alertKey(a, x) });
      }
    }
    renderAlerts();
  }
  async function loadAOIs() {
    try {
      const d = await (await fetch("/api/aoi")).json();
      AOIS = d.areas;
      $("aoi").innerHTML = `<option value="">Custom point</option>` +
        AOIS.map((a) => `<option value="${a.id}">${a.name}${a.prov ? ` (${a.prov})` : ""}</option>`).join("") +
        `<option value="__edit">Edit areas…</option>`;
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
  $("opacity").oninput = (e) => { S.opacity = +e.target.value; overlay.setOpacity(S.opacity); satLayer?.setOpacity(S.opacity); savePrefs(); };
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
      if (ev.type === "alerts") { loadAlerts(); return; }
      if (ev.type === "frontalerts") { loadFrontAlerts(); return; }
      if (ev.type === "incidents") { if (S.firms) loadHotspots(); return; }  // after each FIRMS poll
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
  loadAlerts();
  loadFrontAlerts();
  loadGoes();
  setInterval(loadGoes, 5 * 60 * 1000);  // a new image every 10 min
  setInterval(loadFrontAlerts, 10 * 60 * 1000);
  setInterval(loadAlerts, 5 * 60 * 1000);  // backstop in case an SSE push is missed
  if (S.synoptic) loadFronts();
  setInterval(() => S.synoptic && loadFronts(), 15 * 60 * 1000);  // WPC issues every 3 h
  connectEvents();
})();
