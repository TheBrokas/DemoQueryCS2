"use strict";
/* Sketch board: radar canvas, marker placement, node overlay, hover labels. */

const Sketch = {
  map: null,            // map info object from /api/maps
  level: "upper",
  side: "ct",
  markers: { ct: [], t: [], smoke: [], molly: [] },   // {x, y, level, label} in game coords
  nodesData: null,
  showOverlay: false,
  canvas: null,
  ctx: null,

  init() {
    this.canvas = document.getElementById("board");
    this.ctx = this.canvas.getContext("2d");
    this.canvas.addEventListener("click", (e) => this.onClick(e, false));
    this.canvas.addEventListener("contextmenu", (e) => { e.preventDefault(); this.onClick(e, true); });
  },

  async setMap(mapInfo) {
    this.map = mapInfo;
    this.level = "upper";
    this.markers = { ct: [], t: [], smoke: [], molly: [] };
    this.nodesData = null;
    document.getElementById("level-overlay").hidden = !mapInfo.has_lower;
    if (this.showOverlay) await this.loadNodes();
    this.render();
  },

  async loadNodes() {
    if (!this.nodesData && this.map) {
      this.nodesData = await API.get(`/api/maps/${this.map.map_name}/nodes`);
    }
  },

  gameToPx(x, y) {
    const c = this.map.calibration;
    return [(x - c.pos_x) / c.scale, (c.pos_y - y) / c.scale];
  },
  pxToGame(px, py) {
    const c = this.map.calibration;
    return [px * c.scale + c.pos_x, c.pos_y - py * c.scale];
  },
  canvasPos(e) {
    const r = this.canvas.getBoundingClientRect();
    return [(e.clientX - r.left) * 1024 / r.width, (e.clientY - r.top) * 1024 / r.height];
  },
  isLowerZ(z) {
    const lm = this.map && this.map.calibration.lower_level_max_units;
    return lm !== null && lm !== undefined && z < lm;
  },

  nodeAt(gx, gy) {
    if (!this.nodesData) return null;
    const lm = this.nodesData.lower_level_max_units;
    const wantLower = this.level === "lower";
    let fallback = null;
    const { quads, quad_node, quad_z } = this.nodesData;
    for (let i = 0; i < quads.length; i++) {
      const q = quads[i];
      let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
      for (const [qx, qy] of q) {
        if (qx < minx) minx = qx; if (qx > maxx) maxx = qx;
        if (qy < miny) miny = qy; if (qy > maxy) maxy = qy;
      }
      if (gx < minx || gx > maxx || gy < miny || gy > maxy) continue;
      if (this.pointInQuad(gx, gy, q)) {
        const isLower = lm !== null && quad_z[i] < lm;
        if (lm === null || isLower === wantLower) return quad_node[i];
        if (fallback === null) fallback = quad_node[i];
      }
    }
    return fallback;
  },

  pointInQuad(x, y, q) {
    let inside = false;
    for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
      const [xi, yi] = q[i], [xj, yj] = q[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi) inside = !inside;
    }
    return inside;
  },

  async onClick(e, remove) {
    if (!this.map) return;
    e.preventDefault();
    const [px, py] = this.canvasPos(e);
    const [gx, gy] = this.pxToGame(px, py);
    const list = this.markers[this.side];
    if (remove) {
      let best = -1, bestD = 40 * this.map.calibration.scale;
      for (const s of ["ct", "t", "smoke", "molly"]) {
        this.markers[s].forEach((m, i) => {
          const d = Math.hypot(m.x - gx, m.y - gy);
          if (d < bestD) { bestD = d; best = i; this._rmSide = s; }
        });
      }
      if (best >= 0) this.markers[this._rmSide].splice(best, 1);
    } else {
      const cap = (this.side === "ct" || this.side === "t") ? 5 : 10;
      if (list.length >= cap) return;
      const marker = { x: gx, y: gy, level: this.map.has_lower ? this.level : null, label: "..." };
      list.push(marker);
      try {
        const r = await API.get(`/api/resolve?map_name=${this.map.map_name}&x=${gx.toFixed(1)}&y=${gy.toFixed(1)}` +
          (marker.level ? `&level=${marker.level}` : ""));
        marker.label = r.label || "?";
      } catch (err) { marker.label = "?"; }
    }
    this.render();
    App.updateMarkerSummary();
  },

  async render() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, 1024, 1024);
    if (!this.map) return;
    const level = (this.map.has_lower && this.level === "lower") ? "lower" : "upper";
    const img = await loadRadar(this.map.map_name, level);
    if (img) ctx.drawImage(img, 0, 0, 1024, 1024);
    else { ctx.fillStyle = "#141a20"; ctx.fillRect(0, 0, 1024, 1024); }

    if (this.showOverlay && this.nodesData) this.drawOverlay(ctx);

    // utility markers first (true game scale) so player markers draw on top
    this._icons = this._icons || {
      smoke: Object.assign(new Image(), { src: "icons/weapons/smokegrenade.svg" }),
      molly: Object.assign(new Image(), { src: "icons/weapons/molotov.svg" }),
    };
    for (const s of ["smoke", "molly"]) {
      const rpx = (s === "smoke" ? UTIL.SMOKE_RADIUS_U : UTIL.MOLLY_RADIUS_U) / this.map.calibration.scale;
      this.markers[s].forEach((m) => {
        const [px, py] = this.gameToPx(m.x, m.y);
        const active = !this.map.has_lower || m.level === this.level;
        ctx.globalAlpha = active ? 0.9 : 0.35;
        ctx.beginPath();
        ctx.arc(px, py, rpx, 0, Math.PI * 2);
        ctx.fillStyle = UTIL.COLORS[s];
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#0b0f14";
        ctx.stroke();
        const ic = this._icons[s];
        if (ic.complete && ic.naturalWidth) {
          const sc = 18 / Math.max(ic.naturalWidth, ic.naturalHeight);
          const iw = ic.naturalWidth * sc, ih = ic.naturalHeight * sc;
          ctx.drawImage(ic, px - iw / 2, py - ih / 2, iw, ih);
        }
        ctx.globalAlpha = 1;
      });
    }

    for (const s of ["ct", "t"]) {
      this.markers[s].forEach((m, i) => {
        const [px, py] = this.gameToPx(m.x, m.y);
        const active = !this.map.has_lower || m.level === this.level;
        ctx.globalAlpha = active ? 1 : 0.35;
        ctx.beginPath();
        ctx.arc(px, py, 13, 0, Math.PI * 2);
        ctx.fillStyle = s === "ct" ? "#4a9eff" : "#ffa64a";
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = "#0b0f14";
        ctx.stroke();
        ctx.fillStyle = "#0b0f14";
        ctx.font = "bold 14px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(i + 1), px, py + 1);
        ctx.globalAlpha = 1;
      });
    }
  },

  drawOverlay(ctx) {
    const { quads, quad_node, quad_z, lower_level_max_units: lm } = this.nodesData;
    const wantLower = this.level === "lower";
    ctx.save();
    ctx.lineWidth = 0.6;
    for (let i = 0; i < quads.length; i++) {
      if (lm !== null && ((quad_z[i] < lm) !== wantLower)) continue;
      const n = quad_node[i];
      const hue = (n * 47) % 360;
      ctx.fillStyle = `hsla(${hue}, 55%, 55%, 0.18)`;
      ctx.strokeStyle = `hsla(${hue}, 55%, 70%, 0.35)`;
      ctx.beginPath();
      quads[i].forEach(([qx, qy], j) => {
        const [px, py] = this.gameToPx(qx, qy);
        if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  },
};
