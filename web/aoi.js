/* Areas of interest editor: list, reference point, boundary (box or polygon), alert feed and Manitoba stations.
   Saves the whole list to /api/aoi (validated server-side, previous version backed up). */
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  let DOC = null, ORIG = "", sel = 0, editing = false, CAN_EDIT = true;

  // ---- map ------------------------------------------------------------------------------------------------
  const map = L.map("map", { zoomControl: true, worldCopyJump: false }).setView([52, -100], 4);
  const esri = (s) => `https://server.arcgisonline.com/ArcGIS/rest/services/${s}/MapServer/tile/{z}/{y}/{x}`;
  const imagery = L.tileLayer(esri("World_Imagery"), { maxZoom: 19, maxNativeZoom: 18, attribution: "Tiles © Esri" });
  const topo = L.tileLayer(esri("World_Topo_Map"), { maxZoom: 19, maxNativeZoom: 18, attribution: "Tiles © Esri" });
  const openTopo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    subdomains: "abc", maxZoom: 19, maxNativeZoom: 17,  // tiles stop at 17; deeper zooms enlarge them
    attribution: "Map data © OpenStreetMap contributors, SRTM · Map style © OpenTopoMap (CC-BY-SA)",
  });
  imagery.addTo(map);
  // place names over Esri maps; OpenTopoMap has its own, so they're hidden with it
  const places = L.tileLayer(esri("Reference/World_Boundaries_and_Places"), { maxZoom: 19, maxNativeZoom: 18 }).addTo(map);
  L.control.layers({ Imagery: imagery, Topographic: topo, OpenTopoMap: openTopo }, null, { position: "topright" }).addTo(map);
  map.on("baselayerchange", (e) => (e.layer === openTopo ? places.remove() : places.addTo(map)));
  map.pm.setGlobalOptions({ snappable: false, allowSelfIntersection: false });
  const shapes = L.layerGroup().addTo(map);
  let selMarker = null, selShape = null;

  // ---- helpers --------------------------------------------------------------------------------------------
  const dirty = () => JSON.stringify(DOC) !== ORIG;
  const cur = () => DOC.areas[sel];
  function status(msg, cls = "") { $("status").textContent = msg; $("status").className = cls; }
  function areaKm2(ring) {  // shoelace on a local flat projection
    const lat0 = ring.reduce((s, p) => s + p[0], 0) / ring.length, kx = 111.32 * Math.cos(lat0 * Math.PI / 180), ky = 110.57;
    let a = 0;
    for (let i = 0; i < ring.length; i++) {
      const [y1, x1] = ring[i], [y2, x2] = ring[(i + 1) % ring.length];
      a += x1 * kx * y2 * ky - x2 * kx * y1 * ky;
    }
    return Math.abs(a) / 2;
  }
  const ringOf = (layer) => layer.getLatLngs()[0].map((p) => [+p.lat.toFixed(5), +p.lng.toFixed(5)]);
  const isBox = (r) => r && r.length === 4 && new Set(r.map((p) => p[0])).size === 2 && new Set(r.map((p) => p[1])).size === 2;

  // ---- drawing --------------------------------------------------------------------------------------------
  function drawAll() {
    shapes.clearLayers();
    if (selMarker) { selMarker.remove(); selMarker = null; }
    selShape = null;
    DOC.areas.forEach((a, i) => {
      const me = i === sel;
      if (a.boundary) {
        const poly = L.polygon(a.boundary, { color: me ? "#ff8a3d" : "#ffffff", weight: me ? 2.5 : 1.5, fillOpacity: me ? 0.12 : 0.05, dashArray: me ? null : "4 4" });
        poly.addTo(shapes);
        if (me) selShape = poly;
        else poly.on("click", () => select(i));
      }
      if (!me) {
        L.circleMarker([a.lat, a.lon], { radius: 5, color: "#fff", weight: 1.5, fillColor: "#ff8a3d", fillOpacity: 0.9 })
          .bindTooltip(a.name, { className: "lbl", direction: "right", offset: [6, 0] }).on("click", () => select(i)).addTo(shapes);
      }
    });
    const a = cur();
    if (!a) return;
    selMarker = L.marker([a.lat, a.lon], { draggable: CAN_EDIT, title: "Reference point (drag to move)" })
      .bindTooltip(`${a.name} · reference point`, { className: "lbl", direction: "right", offset: [10, -14], permanent: true }).addTo(map);
    selMarker.on("dragend", () => {
      const p = selMarker.getLatLng();
      a.lat = +p.lat.toFixed(5); a.lon = +p.lng.toFixed(5);
      renderForm(); changed();
    });
    if (selShape && editing) {
      selShape.pm.enable({ allowSelfIntersection: false });
      selShape.on("pm:edit", () => { a.boundary = ringOf(selShape); renderForm(); changed(); });
    }
  }

  map.on("pm:create", (e) => {
    const a = cur();
    a.boundary = ringOf(e.layer);
    e.layer.remove();
    editing = false;
    drawAll(); renderForm(); changed();
    status(`Boundary drawn for ${a.name}. Save to keep it.`);
  });

  // ---- list and form ----------------------------------------------------------------------------------------
  function renderList() {
    $("list").innerHTML = DOC.areas.map((a, i) => `<div class="item ${i === sel ? "sel" : ""}" data-i="${i}">
      <span class="star">${a.id === DOC.default ? "★" : ""}</span><span class="nm">${esc(a.name)}</span>
      <span class="tag ${a.boundary ? "box" : ""}">${a.boundary ? (isBox(a.boundary) ? "box" : "polygon") : "point"}</span>
      <span class="sub">${esc(a.prov || "")}</span></div>`).join("");
    for (const el of $("list").querySelectorAll(".item")) el.onclick = () => select(+el.dataset.i);
  }
  function renderForm() {
    const a = cur();
    if (!a) { $("form").innerHTML = `<p class="hint">No areas.</p>`; return; }
    const b = a.boundary;
    const bdesc = b ? `${isBox(b) ? "Box" : `Polygon, ${b.length} corners`}, ${areaKm2(b).toFixed(1)} km² (${Math.round(areaKm2(b) * 100).toLocaleString()} ha)` : "None: pages use the reference point (fuels within a 10 km circle).";
    $("form").innerHTML = `
      <label>Name <input id="f-name" value="${esc(a.name)}" maxlength="80"></label>
      <div class="row3">
        <label>ID <input id="f-id" value="${esc(a.id)}" title="Used in links and mailing_list.json; changing it breaks those"></label>
        <label>Prov. <input id="f-prov" value="${esc(a.prov || "")}" maxlength="3"></label>
        <label>&nbsp;<button id="f-zoom" type="button">Zoom to</button></label>
      </div>
      <div class="row2">
        <label>Reference latitude <input id="f-lat" value="${a.lat}" inputmode="decimal"></label>
        <label>Longitude <input id="f-lon" value="${a.lon}" inputmode="decimal"></label>
      </div>
      <p class="hint" style="margin:0">The reference point is used for point forecasts, reports and alerts. Drag the marker or type coordinates.</p>
      <div class="box"><h4>Boundary</h4>
        <div class="hint">${bdesc}</div>
        <div class="tools">
          <button id="b-box" type="button">Draw box</button>
          <button id="b-poly" type="button">Draw polygon</button>
          <button id="b-edit" type="button" ${b ? "" : "disabled"}>${editing ? "Done editing" : "Edit corners"}</button>
          <button id="b-centre" type="button" ${b ? "" : "disabled"} title="Move the reference point to the middle of the boundary">Point to centre</button>
          <button id="b-clear" type="button" class="danger" ${b ? "" : "disabled"}>Clear</button>
        </div>
        <label>Import from a GeoJSON file (WGS84; the largest polygon is used)
          <input id="b-file" type="file" accept=".geojson,.json,application/geo+json"></label>
      </div>
      <div class="box"><h4>Alerts and stations</h4>
        <label>ECCC alert feed code <input id="f-feed" value="${esc(a.alert_feed || "")}" placeholder="e.g. mbrm67 (blank: alerts at the point)"></label>
        ${a.alert_feed ? `<a class="hint" target="_blank" rel="noopener" href="https://weather.gc.ca/warnings/report_e.html?${esc(a.alert_feed)}">Check this region on weather.gc.ca ↗</a>` : ""}
        <label>Manitoba report stations, nearest first <input id="f-st" list="st-list" value="${esc((a.mb_stations || []).join(", "))}" placeholder="e.g. SHILO AG, CARBERRY (WZE)"></label>
      </div>`;
    const bind = (id, fn) => { const el = $(id); if (el) el.oninput = el.onchange = () => { fn(el.value); changed(false); }; };
    bind("f-name", (v) => { a.name = v; renderList(); });
    bind("f-id", (v) => { const old = a.id; a.id = v.trim().toLowerCase(); if (DOC.default === old) DOC.default = a.id; });
    bind("f-prov", (v) => { a.prov = v.trim().toUpperCase(); renderList(); });
    bind("f-lat", (v) => { if (!isNaN(+v) && v !== "") { a.lat = +v; selMarker?.setLatLng([a.lat, a.lon]); } });
    bind("f-lon", (v) => { if (!isNaN(+v) && v !== "") { a.lon = +v; selMarker?.setLatLng([a.lat, a.lon]); } });
    bind("f-feed", (v) => { v = v.trim().toLowerCase(); if (v) a.alert_feed = v; else delete a.alert_feed; });
    bind("f-st", (v) => { const s = v.split(",").map((x) => x.trim()).filter(Boolean); if (s.length) a.mb_stations = s; else delete a.mb_stations; });
    $("f-zoom").onclick = zoomTo;
    $("b-box").onclick = () => { editing = false; map.pm.enableDraw("Rectangle"); status("Click one corner of the box, then the opposite corner."); };
    $("b-poly").onclick = () => { editing = false; map.pm.enableDraw("Polygon"); status("Click each corner; click the first corner to finish."); };
    $("b-edit").onclick = () => { editing = !editing; drawAll(); renderForm(); status(editing ? "Drag the corners; drag a middle handle to add one; right-click a corner to remove it." : ""); };
    $("b-clear").onclick = () => { if (confirm(`Remove the boundary of ${a.name}?`)) { delete a.boundary; editing = false; drawAll(); renderForm(); changed(); } };
    $("b-centre").onclick = () => {
      const r = a.boundary, lat = r.reduce((s, p) => s + p[0], 0) / r.length, lon = r.reduce((s, p) => s + p[1], 0) / r.length;
      a.lat = +lat.toFixed(5); a.lon = +lon.toFixed(5); drawAll(); renderForm(); changed();
    };
    $("b-file").onchange = (e) => importGeoJSON(e.target.files[0], a);
  }
  function changed(redraw = true) {
    $("save").disabled = !dirty() || !CAN_EDIT;
    $("revert").disabled = !dirty();
    if (redraw) renderList();
  }
  function select(i) {
    sel = Math.max(0, Math.min(DOC.areas.length - 1, i));
    editing = false;
    map.pm.disableDraw();
    renderList(); renderForm(); drawAll();
  }
  function zoomTo() {
    const a = cur();
    if (a.boundary) map.fitBounds(L.polygon(a.boundary).getBounds().pad(0.3));
    else map.setView([a.lat, a.lon], 11);
  }

  // ---- GeoJSON boundary import ------------------------------------------------------------------------------
  async function importGeoJSON(file, a) {
    if (!file) return;
    try {
      const gj = JSON.parse(await file.text());
      const geoms = [];
      const walk = (g) => {
        if (!g) return;
        if (g.type === "FeatureCollection") g.features.forEach((f) => walk(f.geometry));
        else if (g.type === "Feature") walk(g.geometry);
        else if (g.type === "GeometryCollection") g.geometries.forEach(walk);
        else if (g.type === "Polygon") geoms.push(g.coordinates[0]);
        else if (g.type === "MultiPolygon") g.coordinates.forEach((p) => geoms.push(p[0]));
      };
      walk(gj);
      if (!geoms.length) throw new Error("no polygon found in the file");
      let ring = geoms.map((r) => r.map(([x, y]) => [y, x])).sort((p, q) => areaKm2(q) - areaKm2(p))[0];
      if (ring.some(([y, x]) => Math.abs(y) > 90 || Math.abs(x) > 180)) throw new Error("coordinates aren't longitude/latitude: export as GeoJSON in WGS84 (EPSG:4326)");
      if (ring.length > 1500) { const k = Math.ceil(ring.length / 1500); ring = ring.filter((_, i) => i % k === 0); }  // keep it light
      a.boundary = ring.map(([y, x]) => [+y.toFixed(5), +x.toFixed(5)]);
      editing = false; drawAll(); renderForm(); changed(); zoomTo();
      status(`Imported ${geoms.length > 1 ? `the largest of ${geoms.length} polygons` : "the polygon"} from ${file.name}. Save to keep it.`, "ok");
    } catch (e) {
      status(`Couldn't import ${file.name}: ${e.message}`, "err");
    }
  }

  // ---- list actions -------------------------------------------------------------------------------------------
  $("add").onclick = () => {
    let id = "new-area", k = 2;
    while (DOC.areas.some((a) => a.id === id)) id = `new-area-${k++}`;
    const c = map.getCenter();
    DOC.areas.push({ id, name: "New area", prov: "", lat: +c.lat.toFixed(5), lon: +c.lng.toFixed(5) });
    select(DOC.areas.length - 1); changed();
    status("New area at the map centre: drag its marker, name it, and draw its boundary.");
  };
  const move = (d) => {
    const j = sel + d;
    if (j < 0 || j >= DOC.areas.length) return;
    [DOC.areas[sel], DOC.areas[j]] = [DOC.areas[j], DOC.areas[sel]];
    sel = j; renderList(); changed();
  };
  $("up").onclick = () => move(-1);
  $("down").onclick = () => move(1);
  $("mkdefault").onclick = () => { DOC.default = cur().id; renderList(); changed(); };
  $("del").onclick = () => {
    const a = cur();
    if (DOC.areas.length <= 1 || !confirm(`Delete ${a.name}? (You can restore it from an earlier version after saving.)`)) return;
    DOC.areas.splice(sel, 1);
    if (DOC.default === a.id) DOC.default = DOC.areas[0].id;
    select(sel); changed();
  };

  // ---- load / save / restore -----------------------------------------------------------------------------------
  async function load(keepSel) {
    DOC = await (await fetch("/api/aoi", { cache: "no-store" })).json();
    delete DOC._note;
    ORIG = JSON.stringify(DOC);
    const want = keepSel ? keepSel : (() => { try { return localStorage.getItem("fw-aoi"); } catch (_) { return null; } })();
    sel = Math.max(0, DOC.areas.findIndex((a) => a.id === want));
    select(sel); changed();
  }
  async function loadBackups() {
    const d = await (await fetch("/api/aoi/backups", { cache: "no-store" })).json();
    CAN_EDIT = d.can_edit;
    $("backups").innerHTML = `<option value="">Earlier versions (${d.backups.length})…</option>` + d.backups.map((n) =>
      `<option value="${esc(n)}">${n.slice(4, 8)}-${n.slice(8, 10)}-${n.slice(10, 12)} ${n.slice(13, 15)}:${n.slice(15, 17)}</option>`).join("");
    if (!CAN_EDIT) status("View only: areas can be edited from the server PC or a Tailscale device.", "err");
  }
  $("save").onclick = async () => {
    $("save").disabled = true;
    status("Saving…");
    const body = JSON.stringify(DOC);
    const r = await fetch("/api/aoi", { method: "POST", headers: { "Content-Type": "application/json" }, body });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { status(d.detail || `Save failed (${r.status})`, "err"); changed(false); return; }
    const id = cur()?.id;
    await load(id); await loadBackups();
    status(`Saved ${d.areas.length} areas. The previous version was kept.`, "ok");
  };
  $("revert").onclick = () => { if (confirm("Discard all unsaved changes?")) load(cur()?.id).then(() => status("Changes discarded.")); };
  $("backups").onchange = async (e) => {
    const n = e.target.value;
    e.target.value = "";
    if (!n || !confirm(`Restore the version saved ${e.target.selectedOptions[0]?.text || n}? Unsaved changes are lost; the current version is kept as a backup.`)) return;
    const r = await fetch(`/api/aoi/restore/${encodeURIComponent(n)}`, { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { status(d.detail || "Restore failed", "err"); return; }
    await load(cur()?.id); await loadBackups();
    status("Restored.", "ok");
  };
  window.addEventListener("beforeunload", (e) => { if (DOC && dirty()) { e.preventDefault(); e.returnValue = ""; } });

  // Manitoba report stations, for the station field's suggestions
  fetch("/api/mbwx/stations").then((r) => r.json()).then((d) => {
    const dl = document.createElement("datalist");
    dl.id = "st-list";
    dl.innerHTML = d.stations.map((s) => `<option value="${esc(s.station)}">`).join("");
    document.body.appendChild(dl);
  }).catch(() => {});

  await loadBackups();
  await load();
  zoomTo();
})();
