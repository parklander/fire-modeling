/* Synoptic overlay for the weather map: isobars and H/L centres from the RDPS mean sea-level pressure frame,
   plus WPC surface fronts, drawn on a canvas above any layer and redrawn as the map moves.

   SynopticLayer(map, canvas, meta) -> { setPressure(q), setFronts(set), setVisible(on), redraw() }
   - q: the mslp frame (Uint16Array over the display raster, 0 = no data) or null
   - set: { kind: "analysis"|"forecast", valid, fronts: [{kind, points: [[lat, lon], ...]}] } or null
   Front symbols go on the left of the direction the points run (WPC coded-bulletin convention). */
(function () {
  "use strict";
  const F = 3;             // contour on a grid 3x coarser than the display raster (pressure is smooth)
  const STEP = 4;          // isobar interval, hPa
  const RADIUS = 12;       // H/L search half-window in coarse cells (~ 400 km)
  const PROMINENCE = 2;    // hPa: a centre must stand this far above/below its surroundings
  const COLOURS = { cold: "#3b82ff", warm: "#ff4436", occluded: "#b65cff", trough: "#e0a03a", dryline: "#c8a26b", squall: "#ff4436" };

  window.SynopticLayer = function (map, canvas, meta) {
    const { width: W, height: H, bounds: B } = meta;
    const spec = meta.variables.mslp;
    const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    const yN = mercY(B.north), yS = mercY(B.south);
    const toLatLng = (px, py) => {  // display-raster pixel coordinates -> [lat, lon]
      const y = yN - (py / H) * (yN - yS);
      return [(2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI, B.west + (px / W) * (B.east - B.west)];
    };
    const ctx = canvas.getContext("2d");
    let visible = false, lines = [], centres = [], frontSet = null;

    // ---- pressure field -> isobar polylines and H/L centres -------------------------------------
    function setPressure(q) {
      lines = []; centres = [];
      if (!q || typeof d3 === "undefined" || !d3.contours) { redraw(); return; }
      const w = Math.floor(W / F), h = Math.floor(H / F);
      const raw = new Float64Array(w * h), ok = new Uint8Array(w * h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        const v = q[(y * F + 1) * W + x * F + 1];
        if (v) { raw[y * w + x] = (v - 1) / spec.scale + spec.offset; ok[y * w + x] = 1; }
      }
      // 3x3 mean of valid neighbours: smoother lines; no-data cells get the mean too so contours close cleanly
      const val = new Float64Array(w * h);
      let any = false;
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          const xx = x + dx, yy = y + dy;
          if (xx >= 0 && xx < w && yy >= 0 && yy < h && ok[yy * w + xx]) { s += raw[yy * w + xx]; n++; }
        }
        val[y * w + x] = n ? s / n : NaN;
        if (n) any = true;
      }
      if (!any) { redraw(); return; }
      let lo = Infinity, hi = -Infinity;
      for (let i = 0; i < val.length; i++) if (ok[i]) { lo = Math.min(lo, val[i]); hi = Math.max(hi, val[i]); }
      const fill = lo - 50;  // outside the RDPS domain: well below every isobar level
      const grid = Float64Array.from(val, (v) => (Number.isNaN(v) ? fill : v));
      const levels = [];
      for (let l = Math.ceil(lo / STEP) * STEP; l <= hi; l += STEP) levels.push(l);

      // a contour point is usable if it isn't on the grid edge or next to missing data
      const good = (x, y) => {
        const cx = Math.floor(x), cy = Math.floor(y);
        if (cx < 1 || cy < 1 || cx >= w - 1 || cy >= h - 1) return false;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) if (!ok[(cy + dy) * w + cx + dx]) return false;
        return true;
      };
      for (const c of d3.contours().size([w, h]).thresholds(levels)(grid)) {
        for (const poly of c.coordinates) for (const ring of poly) {
          let cur = [];
          for (const [x, y] of ring) {
            if (good(x, y)) cur.push(toLatLng(x * F, y * F));
            else { if (cur.length > 3) lines.push({ level: c.value, pts: cur }); cur = []; }
          }
          if (cur.length > 3) lines.push({ level: c.value, pts: cur });
        }
      }
      // H/L: local extremes over a (2R+1)^2 window (separable running max/min), with some prominence
      const run = (src, fn, init) => {
        const a = new Float64Array(w * h), b = new Float64Array(w * h);
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
          let m = init;
          for (let k = Math.max(0, x - RADIUS); k <= Math.min(w - 1, x + RADIUS); k++) if (ok[y * w + k]) m = fn(m, src[y * w + k]);
          a[y * w + x] = m;
        }
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
          let m = init;
          for (let k = Math.max(0, y - RADIUS); k <= Math.min(h - 1, y + RADIUS); k++) m = fn(m, a[k * w + x]);
          b[y * w + x] = m;
        }
        return b;
      };
      const mx = run(val, Math.max, -Infinity), mn = run(val, Math.min, Infinity);
      const taken = [];
      for (let y = RADIUS; y < h - RADIUS; y++) for (let x = RADIUS; x < w - RADIUS; x++) {
        const i = y * w + x;
        if (!good(x + 0.5, y + 0.5)) continue;
        const v = val[i];
        const type = v === mx[i] && v - mn[i] >= PROMINENCE ? "H" : v === mn[i] && mx[i] - v >= PROMINENCE ? "L" : null;
        if (!type || taken.some(([tx, ty]) => Math.abs(tx - x) <= RADIUS && Math.abs(ty - y) <= RADIUS)) continue;
        taken.push([x, y]);
        const [lat, lon] = toLatLng((x + 0.5) * F, (y + 0.5) * F);
        centres.push({ type, lat, lon, hpa: Math.round(v) });
      }
      redraw();
    }

    function setFronts(set) { frontSet = set; redraw(); }
    function setVisible(on) { visible = on; canvas.hidden = !on; redraw(); }

    // ---- drawing ---------------------------------------------------------------------------------
    function resize() {
      const s = map.getSize(), dpr = window.devicePixelRatio || 1;
      if (canvas.width !== s.x * dpr || canvas.height !== s.y * dpr) {
        canvas.width = s.x * dpr; canvas.height = s.y * dpr;
        canvas.style.width = s.x + "px"; canvas.style.height = s.y + "px";
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return s;
    }
    const P = (ll) => map.latLngToContainerPoint(ll);
    function halo(text, x, y, fill, font) {
      ctx.font = font; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(10,13,18,.85)"; ctx.strokeText(text, x, y);
      ctx.fillStyle = fill; ctx.fillText(text, x, y);
    }

    function drawIsobars(size) {
      ctx.lineJoin = "round";
      const labels = [];
      for (const ln of lines) {
        const pts = ln.pts.map(P);
        ctx.beginPath();
        pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.strokeStyle = ln.level % 20 === 0 ? "rgba(255,255,255,.95)" : "rgba(235,240,245,.7)";
        ctx.lineWidth = ln.level % 20 === 0 ? 1.8 : 1.1;
        ctx.stroke();
        // one label per on-screen stretch of line, kept apart from other labels
        let acc = 0;
        for (let i = 1; i < pts.length; i++) {
          acc += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
          const p = pts[i];
          if (acc > 220 && p.x > 30 && p.y > 30 && p.x < size.x - 30 && p.y < size.y - 30
              && !labels.some((q) => Math.hypot(q.x - p.x, q.y - p.y) < 90)) {
            labels.push(p); acc = 0;
            halo(String(ln.level % 100).padStart(2, "0"), p.x, p.y, "#ffffff", "600 11px Inter, Segoe UI, sans-serif");
          }
        }
      }
    }

    function drawCentres() {
      for (const c of centres) {
        const p = P([c.lat, c.lon]);
        halo(c.type, p.x, p.y - 6, c.type === "H" ? "#5aa9ff" : "#ff5a4e", "800 22px Inter, Segoe UI, sans-serif");
        halo(String(c.hpa), p.x, p.y + 12, "#ffffff", "600 11px Inter, Segoe UI, sans-serif");
      }
    }

    // walk a screen-space polyline, calling fn(x, y, ux, uy, k) every `gap` px (unit tangent ux, uy)
    function along(pts, gap, offset, fn) {
      let next = offset, dist = 0, k = 0;
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i], seg = Math.hypot(b.x - a.x, b.y - a.y);
        if (!seg) continue;
        const ux = (b.x - a.x) / seg, uy = (b.y - a.y) / seg;
        while (next <= dist + seg) {
          const t = next - dist;
          fn(a.x + ux * t, a.y + uy * t, ux, uy, k++);
          next += gap;
        }
        dist += seg;
      }
    }
    // split a screen-space polyline into consecutive pieces `len` px long (following the line's bends)
    function chunks(pts, len) {
      const out = [];
      let cur = [pts[0]], left = len;
      for (let i = 1; i < pts.length; i++) {
        let a = pts[i - 1];
        const b = pts[i];
        let seg = Math.hypot(b.x - a.x, b.y - a.y);
        while (seg > left) {
          const t = left / seg, m = { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
          cur.push(m); out.push(cur); cur = [m];
          a = m; seg -= left; left = len;
        }
        cur.push(b); left -= seg;
      }
      if (cur.length > 1) out.push(cur);
      return out;
    }
    function triangle(x, y, ux, uy, side, col) {  // side +1 = left of travel (screen y points down)
      const nx = uy * side, ny = -ux * side, s = 7;
      ctx.beginPath();
      ctx.moveTo(x - ux * s, y - uy * s); ctx.lineTo(x + ux * s, y + uy * s); ctx.lineTo(x + nx * s * 1.3, y + ny * s * 1.3);
      ctx.closePath(); ctx.fillStyle = col; ctx.fill();
    }
    function semicircle(x, y, ux, uy, side, col) {
      const ang = Math.atan2(uy, ux);
      ctx.beginPath();
      // left of travel in screen coordinates is the anticlockwise half
      ctx.arc(x, y, 6.5, ang, ang + Math.PI, side > 0);
      ctx.closePath(); ctx.fillStyle = col; ctx.fill();
    }

    function drawFront(f) {
      const pts = f.points.map(P);
      if (pts.length < 2) return;
      const path = () => { ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); };
      ctx.setLineDash([]); ctx.lineWidth = 2.6;
      if (f.kind === "stationary") {  // alternating blue/red pieces: triangles on one side, semicircles on the other
        const L = 26;
        chunks(pts, L).forEach((c, k) => {
          ctx.beginPath(); c.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.strokeStyle = k % 2 ? COLOURS.warm : COLOURS.cold; ctx.stroke();
        });
        along(pts, L, L / 2, (x, y, ux, uy, k) =>
          (k % 2 ? semicircle(x, y, ux, uy, -1, COLOURS.warm) : triangle(x, y, ux, uy, 1, COLOURS.cold)));
        return;
      }
      const col = COLOURS[f.kind] || "#cccccc";
      if (f.kind === "trough") { ctx.setLineDash([9, 6]); ctx.lineWidth = 2; }
      if (f.kind === "dryline" || f.kind === "squall") { ctx.setLineDash([2, 5]); ctx.lineWidth = 2.4; }
      path(); ctx.strokeStyle = col; ctx.stroke(); ctx.setLineDash([]);
      if (f.kind === "cold") along(pts, 38, 18, (x, y, ux, uy) => triangle(x, y, ux, uy, 1, col));
      else if (f.kind === "warm") along(pts, 38, 18, (x, y, ux, uy) => semicircle(x, y, ux, uy, 1, col));
      else if (f.kind === "occluded") along(pts, 30, 15, (x, y, ux, uy, k) =>
        (k % 2 ? semicircle : triangle)(x, y, ux, uy, 1, col));
    }

    function redraw() {
      const size = resize();
      ctx.clearRect(0, 0, size.x, size.y);
      if (!visible) return;
      drawIsobars(size);
      if (frontSet) for (const f of frontSet.fronts) drawFront(f);
      drawCentres();
    }
    map.on("move zoom resize viewreset", redraw);
    return { setPressure, setFronts, setVisible, redraw };
  };
})();
