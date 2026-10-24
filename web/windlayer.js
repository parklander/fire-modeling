/* Wind display shared by the weather viewer and the fire page: animated particles
 * (zoom.earth style) or static arrows, drawn on a canvas over a Leaflet map from RDPS
 * speed/direction frames (uint16 codes on the Web Mercator display raster, see /api/meta).
 *
 *   const wind = WindLayer(map, canvasEl, meta);
 *   wind.setFrames(speedCodes, dirCodes, "wspd", "wdir");  // null to clear
 *   wind.setMode("particles" | "arrows" | "off");
 */
(function () {
  "use strict";
  const CELL = 5;            // screen px per wind-field cell
  const PX_PER_KMH = 0.028;  // particle speed: px per frame per km/h (~50 px/s at 30 km/h)
  const BUCKETS = [          // speed (km/h) -> stroke style; faster wind = brighter, thicker streak
    [10, "rgba(255,255,255,0.32)", 0.8], [20, "rgba(255,255,255,0.5)", 1.0],
    [35, "rgba(255,255,255,0.7)", 1.25], [55, "rgba(255,255,255,0.88)", 1.5], [Infinity, "rgba(255,240,220,1)", 1.8],
  ];

  window.WindLayer = function (map, canvas, meta) {
    const B = meta.bounds, W = meta.width, H = meta.height;
    const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    const yN = mercY(B.north), yS = mercY(B.south);
    const decode = (v, q) => (q ? (q - 1) / meta.variables[v].scale + meta.variables[v].offset : NaN);
    const pixel = (lat, lon) => {
      const x = Math.floor(((lon - B.west) / (B.east - B.west)) * W);
      const y = Math.floor(((yN - mercY(lat)) / (yN - yS)) * H);
      return x >= 0 && x < W && y >= 0 && y < H ? y * W + x : -1;
    };

    let mode = "particles", frames = null, field = null, P = null, moving = false, animId = null;

    function sizeCanvas() {
      // capped: fading the trails costs per canvas pixel, and thin streaks don't need 3x
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5), size = map.getSize();
      if (canvas.width !== size.x * dpr || canvas.height !== size.y * dpr) {
        canvas.width = size.x * dpr; canvas.height = size.y * dpr;
        canvas.style.width = size.x + "px"; canvas.style.height = size.y + "px";
      }
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return [ctx, size];
    }

    // Screen-space field on a coarse grid. In Web Mercator, screen x depends only on
    // longitude and y only on latitude, so the raster lookup is per column / per row.
    function buildField() {
      field = null;
      if (!frames) return;
      const [spd, dir, sv, dv] = frames, size = map.getSize();
      const gw = Math.ceil(size.x / CELL) + 1, gh = Math.ceil(size.y / CELL) + 1;
      const cols = new Int32Array(gw), rows = new Int32Array(gh);
      for (let c = 0; c < gw; c++) {
        const x = Math.floor(((map.containerPointToLatLng([c * CELL, 0]).lng - B.west) / (B.east - B.west)) * W);
        cols[c] = x >= 0 && x < W ? x : -1;
      }
      for (let r = 0; r < gh; r++) {
        const y = Math.floor(((yN - mercY(map.containerPointToLatLng([0, r * CELL]).lat)) / (yN - yS)) * H);
        rows[r] = y >= 0 && y < H ? y : -1;
      }
      const u = new Float32Array(gw * gh), v = new Float32Array(gw * gh), s = new Float32Array(gw * gh);
      for (let r = 0; r < gh; r++) {
        for (let c = 0; c < gw; c++) {
          const k = r * gw + c, i = rows[r] < 0 || cols[c] < 0 ? -1 : rows[r] * W + cols[c];
          if (i < 0 || !spd[i] || !dir[i]) { u[k] = v[k] = s[k] = NaN; continue; }
          const sp = decode(sv, spd[i]), a = ((decode(dv, dir[i]) + 180) * Math.PI) / 180;  // blowing toward
          u[k] = Math.sin(a) * sp * PX_PER_KMH; v[k] = -Math.cos(a) * sp * PX_PER_KMH; s[k] = sp;
        }
      }
      field = { gw, gh, u, v, s };
    }
    function sample(x, y) {
      if (!field) return -1;
      const c = Math.round(x / CELL), r = Math.round(y / CELL);
      if (c < 0 || r < 0 || c >= field.gw || r >= field.gh) return -1;
      const k = r * field.gw + c;
      return Number.isNaN(field.s[k]) ? -1 : k;
    }

    // Particles: screen positions; each lives 40–110 frames, then respawns.
    function spawn(i, size) {
      for (let tries = 0; tries < 12; tries++) {
        const x = Math.random() * size.x, y = Math.random() * size.y;
        P.x[i] = x; P.y[i] = y;
        if (sample(x, y) >= 0) break;
      }
      P.age[i] = Math.floor(Math.random() * 40);
      P.life[i] = 40 + Math.floor(Math.random() * 70);
    }
    function seedParticles() {
      const size = map.getSize();
      const n = Math.min(6000, Math.max(600, Math.round((size.x * size.y) / 380)));
      P = { n, x: new Float32Array(n), y: new Float32Array(n), age: new Uint16Array(n), life: new Uint16Array(n) };
      for (let i = 0; i < n; i++) spawn(i, size);
    }
    function tick() {
      animId = requestAnimationFrame(tick);
      if (mode !== "particles" || moving || !field || !P) return;
      const [ctx, size] = sizeCanvas();
      ctx.globalCompositeOperation = "destination-in";  // fade last frame's streaks into trails
      ctx.fillStyle = "rgba(0,0,0,0.9)";
      ctx.fillRect(0, 0, size.x, size.y);
      ctx.globalCompositeOperation = "source-over";
      const paths = BUCKETS.map(() => new Path2D());
      for (let i = 0; i < P.n; i++) {
        const k = sample(P.x[i], P.y[i]);
        if (k < 0 || ++P.age[i] > P.life[i]) { spawn(i, size); continue; }
        const x1 = P.x[i] + field.u[k], y1 = P.y[i] + field.v[k];
        let b = 0;
        while (field.s[k] > BUCKETS[b][0]) b++;
        paths[b].moveTo(P.x[i], P.y[i]);
        paths[b].lineTo(x1, y1);
        P.x[i] = x1; P.y[i] = y1;
      }
      ctx.lineCap = "round";
      BUCKETS.forEach(([, style, width], b) => { ctx.strokeStyle = style; ctx.lineWidth = width; ctx.stroke(paths[b]); });
    }

    function drawArrows(ctx, size) {
      if (!frames) return;
      const [spd, dir, sv, dv] = frames, step = 38;
      ctx.lineCap = "round";
      for (let y = step / 2; y < size.y; y += step) {
        for (let x = step / 2; x < size.x; x += step) {
          const ll = map.containerPointToLatLng([x, y]);
          const i = pixel(ll.lat, ll.lng);
          if (i < 0 || !spd[i] || !dir[i]) continue;
          const s = decode(sv, spd[i]), d = decode(dv, dir[i]);
          // meteorological direction = where wind comes FROM; arrow points downwind
          const a = ((d + 180) * Math.PI) / 180, len = 7 + Math.min(s, 80) * 0.22;
          const dx = Math.sin(a) * len, dy = -Math.cos(a) * len;
          const x0 = x - dx / 2, y0 = y - dy / 2, x1 = x + dx / 2, y1 = y + dy / 2;
          ctx.strokeStyle = "rgba(10,12,16,.55)"; ctx.lineWidth = 3.2;
          arrow(ctx, x0, y0, x1, y1, a);
          ctx.strokeStyle = s > 40 ? "#fff" : "rgba(255,255,255,.82)"; ctx.lineWidth = 1.4;
          arrow(ctx, x0, y0, x1, y1, a);
        }
      }
    }
    function arrow(ctx, x0, y0, x1, y1, a) {
      const h = 4.5;
      ctx.beginPath();
      ctx.moveTo(x0, y0); ctx.lineTo(x1, y1);
      ctx.moveTo(x1 - Math.sin(a - 0.5) * h, y1 + Math.cos(a - 0.5) * h); ctx.lineTo(x1, y1);
      ctx.lineTo(x1 - Math.sin(a + 0.5) * h, y1 + Math.cos(a + 0.5) * h);
      ctx.stroke();
    }

    function redraw() {
      const [ctx, size] = sizeCanvas();
      ctx.clearRect(0, 0, size.x, size.y);
      buildField();
      if (mode === "particles") {
        seedParticles();
        if (!animId) animId = requestAnimationFrame(tick);
      } else if (animId) {
        cancelAnimationFrame(animId); animId = null;
      }
      if (mode === "arrows") drawArrows(ctx, size);
    }
    map.on("movestart zoomstart", () => {
      moving = true;
      if (mode === "particles") sizeCanvas()[0].clearRect(0, 0, canvas.width, canvas.height);
    });
    map.on("moveend zoomend resize", () => { moving = false; redraw(); });
    map.on("move", () => {
      if (mode === "arrows") { const [ctx, size] = sizeCanvas(); ctx.clearRect(0, 0, size.x, size.y); drawArrows(ctx, size); }
    });

    return {
      get mode() { return mode; },
      setMode(m) { mode = m; redraw(); },
      /** New hour's frames. Particles keep flowing (only the field changes); arrows redraw. */
      setFrames(spd, dir, sv, dv) {
        frames = spd && dir ? [spd, dir, sv, dv] : null;
        if (mode === "particles" && animId && P && frames) buildField();
        else redraw();
      },
      redraw,
    };
  };
})();
