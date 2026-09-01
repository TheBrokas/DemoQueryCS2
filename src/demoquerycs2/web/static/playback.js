"use strict";
/* Round playback: 1 Hz keyframes with linear interpolation. */

const BombIcon = new Image();
BombIcon.src = "icons/weapons/c4.svg";

const UtilIcons = {};
for (const n of ["smokegrenade", "molotov", "incgrenade", "flashbang", "hegrenade"]) {
  UtilIcons[n] = new Image();
  UtilIcons[n].src = `icons/weapons/${n}.svg`;
}

const Playback = {
  data: null,
  mapInfo: null,
  level: "upper",
  playing: false,
  t: 0,                    // seconds (fractional)
  lastTs: null,
  raf: null,
  _seq: 0,                 // open() generation — a newer open() or close() supersedes an in-flight one

  init() {
    Tip.init();              // playback tooltips work even before the first search
    document.getElementById("playback-close").addEventListener("click", () => this.close());
    document.getElementById("playback-modal").addEventListener("click", (e) => {
      if (e.target.id === "playback-modal") this.close();
    });
    document.getElementById("pb-play").addEventListener("click", () => this.toggle());
    document.getElementById("pb-scrub").addEventListener("input", (e) => {
      this.t = parseFloat(e.target.value);
      this.playing = false;
      this.updatePlayBtn();
      this.draw();
    });
    document.getElementById("pb-upper").addEventListener("click", () => this.setLevel("upper"));
    document.getElementById("pb-lower").addEventListener("click", () => this.setLevel("lower"));
    document.addEventListener("keydown", (e) => {
      if (document.getElementById("playback-modal").hidden) return;
      if (e.key === "Escape") this.close();
      if (e.key === " ") { e.preventDefault(); this.toggle(); }
    });
  },

  fillHeader() {
    const r = this.data.round;
    const setTeam = (id, name, wantLogo) => {
      const el = document.getElementById(id);
      el.innerHTML = "";
      const canon = canonicalTeam(name);
      el.appendChild(document.createTextNode(displayTeam(canon)));
    };
    setTeam("pb-team-ct", r.ct_team || "CT", r.has_teams);
    setTeam("pb-team-t", r.t_team || "T", r.has_teams);
    const hltv = document.getElementById("pb-hltv");
    hltv.hidden = !r.has_teams;
    if (r.has_teams) {
      const ct = canonicalTeam(r.ct_team);
      const t = canonicalTeam(r.t_team);
      if (r.hltv_id) {
        hltv.href = `https://www.hltv.org/matches/${r.hltv_id}/` +
          `${ct.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-vs-${t.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
        hltv.dataset.tip = "Open this match on HLTV";
      } else {
        hltv.href = "https://www.hltv.org/search?query=" + encodeURIComponent(`${ct} ${t}`);
        hltv.dataset.tip = "Find this match on HLTV";
      }
    }
    document.getElementById("pb-score-ct").textContent = r.has_teams ? r.ct_score : "";
    document.getElementById("pb-score-t").textContent = r.has_teams ? r.t_score : "";
    document.getElementById("pb-round-label").textContent = `Round ${r.round_num}`;
  },

  async open(roundId, startSec, mapInfo, opts = {}) {
    const seq = ++this._seq;
    // A map chip can call open() while the previous round is still animating.
    // Stop that loop before awaiting the new payload so it cannot retain an
    // old HUD or spawn a second RAF chain alongside the new playback.
    this.playing = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
    // Internal round navigation should feel continuous: leave the existing
    // frame/HUD visible while the next payload loads.  A genuinely new
    // playback (result card or another map) still closes the old view so its
    // content cannot be mistaken for the requested match.
    if (!opts.keep) document.getElementById("playback-modal").hidden = true;
    const data = await API.get(`/api/rounds/${roundId}/playback`);
    if (seq !== this._seq) return;   // a newer open() or close() supersedes this one
    this.mapInfo = mapInfo;
    this.data = data;
    this.cards = null;       // player-card DOM belongs to the previous round/match
    this.level = "upper";
    this.boxSec = null;
    const plantFrame = this.data.frames.find((f) => f.bomb_planted);
    this.plantT = plantFrame ? plantFrame.t : null;
    this.fillHeader();
    // the round-ending kill can land up to a second past the last 1 Hz state
    // sample - extend the timeline so it reaches the feed (frames clamp to the
    // last sample, so players just hold their final positions)
    const frameMax = this.data.frames.length ? this.data.frames[this.data.frames.length - 1].t : 0;
    const killMax = (this.data.kills || []).reduce((m, k) => Math.max(m, k.t), 0);
    const maxT = Math.max(frameMax, killMax);
    const scrub = document.getElementById("pb-scrub");
    scrub.max = maxT;
    this.t = Math.max(0, Math.min(startSec, maxT));
    scrub.value = this.t;
    this.drawKillMarks(maxT);
    document.getElementById("pb-levels").hidden = !this.mapInfo.has_lower;
    document.getElementById("playback-modal").hidden = false;
    this.playing = true;
    this.lastTs = null;
    this.updatePlayBtn();
    this.loop();
  },

  close() {
    this._seq++;             // abandon an in-flight open()
    this.playing = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
    document.getElementById("playback-modal").hidden = true;
  },

  toggle() {
    this.playing = !this.playing;
    this.lastTs = null;
    this.updatePlayBtn();
    if (this.playing) this.loop();
  },

  updatePlayBtn() {
    document.getElementById("pb-play").innerHTML = this.playing ? "&#10074;&#10074;" : "&#9654;";
  },

  setLevel(lv) {
    this.level = lv;
    document.getElementById("pb-upper").classList.toggle("active-lvl", lv === "upper");
    document.getElementById("pb-lower").classList.toggle("active-lvl", lv === "lower");
    this.draw();
  },

  loop(ts) {
    if (!this.playing) return;
    if (ts !== undefined) {
      if (this.lastTs !== null) {
        const speed = parseFloat(document.getElementById("pb-speed").value);
        this.t += (ts - this.lastTs) / 1000 * speed;
      }
      this.lastTs = ts;
    }
    const maxT = parseFloat(document.getElementById("pb-scrub").max);
    if (this.t >= maxT) {
      this.t = maxT;
      this.playing = false;
      this.updatePlayBtn();
    }
    document.getElementById("pb-scrub").value = this.t;
    this.draw();
    if (this.playing) this.raf = requestAnimationFrame((t2) => this.loop(t2));
  },

  frameAt(t) {
    const frames = this.data.frames;
    if (!frames.length) return [null, null, 0];
    let i = frames.findIndex((f) => f.t > t);
    if (i === -1) i = frames.length;
    const a = frames[Math.max(0, i - 1)];
    const b = frames[Math.min(frames.length - 1, i)];
    const span = b.t - a.t;
    const frac = span > 0 ? (t - a.t) / span : 0;
    return [a, b, Math.max(0, Math.min(1, frac))];
  },

  // States are sampled at 1 Hz, while kills retain their exact event time.
  // Overlay deaths so a terminal kill cannot leave its victim visually alive.
  deadAt(t) {
    return new Set((this.data.kills || []).filter((k) => k.t <= t).map((k) => k.victim));
  },

  async draw() {
    const seq = this._seq;
    const data = this.data;
    const mapInfo = this.mapInfo;
    const cv = document.getElementById("playback-canvas");
    const ctx = cv.getContext("2d");
    const [a, b, frac] = this.frameAt(this.t);
    const level = (mapInfo.has_lower && this.level === "lower") ? "lower" : "upper";
    const img = await loadRadar(mapInfo.map_name, level);
    if (seq !== this._seq || data !== this.data || mapInfo !== this.mapInfo) return;
    ctx.fillStyle = "#0a0d10";
    ctx.fillRect(0, 0, 1024, 1024);
    if (img) ctx.drawImage(img, 0, 0, 1024, 1024);
    if (!a) return;

    const c = this.mapInfo.calibration;
    const lm = c.lower_level_max_units;
    const dead = this.deadAt(this.t);
    const byName = {};
    for (const p of b.players) byName[p.name] = p;

    const lerpYaw = (y0, y1, f) => {
      if (y0 === null || y0 === undefined) return null;
      if (y1 === null || y1 === undefined) return y0;
      const d = ((y1 - y0 + 540) % 360) - 180;
      return y0 + d * f;
    };

    // utility: travel path + in-flight projectile, then area circles (smoke/molly)
    // or brief pop rings (flash/he), each with its weapon icon at the center
    const toPx = (x, y) => [(x - c.pos_x) / c.scale, (c.pos_y - y) / c.scale];
    const drawIcon = (name, px, py, size) => {
      const ic = UtilIcons[name];
      if (ic && ic.complete && ic.naturalWidth) {
        // icons are non-square (e.g. smokegrenade 15x32) - fit inside size, keep aspect
        const s = size / Math.max(ic.naturalWidth, ic.naturalHeight);
        const w = ic.naturalWidth * s, h = ic.naturalHeight * s;
        ctx.drawImage(ic, px - w / 2, py - h / 2, w, h);
      }
    };
    for (const g of (this.data.grenades || [])) {
      const gLower = lm !== null && lm !== undefined && g.z < lm;
      const gDim = this.mapInfo.has_lower && ((this.level === "lower") !== gLower);
      const dimF = gDim ? 0.25 : 1;
      const [gpx, gpy] = toPx(g.x, g.y);
      const sideCol = g.thrower_side === "CT" ? "#4a9eff" : "#ffa64a";
      const icon = UTIL.icon(g.type, g.thrower_side);

      // travel path: builds while in flight, lingers briefly after detonation
      const path = g.path;
      if (path && path.length >= 2 && this.t >= path[0][0] && this.t <= g.t + UTIL.PATH_FADE_S) {
        const pathAlpha = this.t <= g.t ? 0.8
          : 0.8 * (1 - (this.t - g.t) / UTIL.PATH_FADE_S);
        ctx.globalAlpha = dimF * pathAlpha;
        ctx.beginPath();
        let prev = null;
        for (const [pt, px0, py0] of path) {
          if (pt > this.t) break;
          const [ppx, ppy] = toPx(px0, py0);
          if (prev === null) ctx.moveTo(ppx, ppy); else ctx.lineTo(ppx, ppy);
          prev = [ppx, ppy, pt, px0, py0];
        }
        if (prev !== null) {
          if (this.t < g.t) {
            // interpolate to the projectile's current position and mark it
            let cur = prev;
            for (let i = 0; i < path.length; i++) {
              if (path[i][0] > this.t) {
                const [t1, x1, y1] = path[i];
                const [t0, x0, y0] = i ? [path[i - 1][0], path[i - 1][1], path[i - 1][2]]
                  : [t1, x1, y1];
                const f = t1 > t0 ? (this.t - t0) / (t1 - t0) : 0;
                cur = toPx(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f);
                break;
              }
            }
            ctx.lineTo(cur[0], cur[1]);
            ctx.setLineDash([4, 5]);
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = sideCol;
            ctx.stroke();
            ctx.setLineDash([]);
            drawIcon(icon, cur[0], cur[1], 14);   // the nade mid-air
          } else {
            ctx.setLineDash([4, 5]);
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = sideCol;
            ctx.stroke();
            ctx.setLineDash([]);
          }
        }
        ctx.globalAlpha = 1;
      }

      if (g.type === "smoke" || g.type === "molly") {
        const tEnd = (g.t_end === null || g.t_end === undefined) ? g.t : g.t_end;
        if (this.t < g.t || this.t > tEnd) continue;
        const rpx = (g.type === "smoke" ? UTIL.SMOKE_RADIUS_U : UTIL.MOLLY_RADIUS_U) / c.scale;
        const fade = Math.max(0.15, Math.min(1, (tEnd - this.t) / 1.5));
        ctx.globalAlpha = dimF * fade;
        ctx.beginPath();
        ctx.arc(gpx, gpy, rpx, 0, Math.PI * 2);
        ctx.fillStyle = UTIL.COLORS[g.type];
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = sideCol;
        ctx.globalAlpha = dimF * fade * 0.7;
        ctx.stroke();
        ctx.globalAlpha = dimF * Math.min(1, fade + 0.15);
        drawIcon(icon, gpx, gpy, 20);
      } else {
        const age = this.t - g.t;
        if (age < 0 || age > UTIL.FLASH_POP_S) continue;
        const f = age / UTIL.FLASH_POP_S;
        ctx.globalAlpha = dimF * (1 - f);
        ctx.beginPath();
        ctx.arc(gpx, gpy, (20 + 80 * f) / c.scale, 0, Math.PI * 2);
        ctx.lineWidth = 3;
        ctx.strokeStyle = g.type === "flash" ? "#ffffff" : "#ff6b4a";
        ctx.stroke();
        drawIcon(icon, gpx, gpy, 16);
      }
      ctx.globalAlpha = 1;
    }

    // toward-white blend for flashed players (f = 0 none .. 1 fully blind)
    const whiten = (hex, f) => {
      const n = parseInt(hex.slice(1), 16);
      const ch = (sh) => Math.round(((n >> sh) & 255) + (255 - ((n >> sh) & 255)) * f);
      return `rgb(${ch(16)},${ch(8)},${ch(0)})`;
    };
    const posByName = {};
    for (const p of a.players) {
      const q = byName[p.name] || p;
      const alive = p.alive && q.alive && !dead.has(p.name);
      const x = p.x + (q.x - p.x) * frac;
      const y = p.y + (q.y - p.y) * frac;
      const z = p.z + (q.z - p.z) * frac;
      const px = (x - c.pos_x) / c.scale;
      const py = (c.pos_y - y) / c.scale;
      const onLower = lm !== null && lm !== undefined && z < lm;
      const dim = this.mapInfo.has_lower && ((this.level === "lower") !== onLower);
      ctx.globalAlpha = (alive ? 1 : 0.4) * (dim ? 0.25 : 1);
      if (alive) {
        const fl = Math.min(1, ((p.flash || 0) + ((q.flash || 0) - (p.flash || 0)) * frac) / 2);
        const color = whiten(p.side === "CT" ? "#4a9eff" : "#ffa64a", fl * 0.85);
        const yaw = lerpYaw(p.yaw, q.yaw, frac);
        posByName[p.name] = { px, py, ang: yaw !== null ? -yaw * Math.PI / 180 : 0, dim };
        if (yaw !== null) {
          const ang = -yaw * Math.PI / 180;      // game yaw is CCW, canvas y is flipped
          ctx.beginPath();
          ctx.moveTo(px, py);
          ctx.arc(px, py, 30, ang - 0.44, ang + 0.44);
          ctx.closePath();
          ctx.fillStyle = color;
          ctx.globalAlpha *= 0.22;
          ctx.fill();
          ctx.globalAlpha = (alive ? 1 : 0.4) * (dim ? 0.25 : 1);
          ctx.beginPath();
          ctx.moveTo(px, py);
          ctx.lineTo(px + Math.cos(ang) * 16, py + Math.sin(ang) * 16);
          ctx.lineWidth = 2.5;
          ctx.strokeStyle = color;
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.arc(px, py, 9, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#0b0f14";
        ctx.stroke();
        ctx.font = "11px sans-serif";
        ctx.textAlign = "center";
        ctx.lineJoin = "round";
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(6, 9, 12, .85)";   // halo keeps names readable on bright map areas
        ctx.strokeText(p.name, px, py - 13);
        ctx.fillStyle = "#e8eef5";
        ctx.fillText(p.name, px, py - 13);
      } else {
        ctx.strokeStyle = p.side === "CT" ? "#4a9eff" : "#ffa64a";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.moveTo(px - 6, py - 6); ctx.lineTo(px + 6, py + 6);
        ctx.moveTo(px + 6, py - 6); ctx.lineTo(px - 6, py + 6);
        ctx.stroke();
        ctx.font = "10px sans-serif";
        ctx.textAlign = "center";
        ctx.lineJoin = "round";
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(6, 9, 12, .85)";
        ctx.strokeText(p.name, px, py - 11);
        ctx.fillStyle = "#8494a7";
        ctx.fillText(p.name, px, py - 11);
      }
      ctx.globalAlpha = 1;
    }

    // muzzle flashes: a short burst along the shooter's view for each shot
    // fired within the last MUZZLE_S seconds (v8 demos only)
    const slotNames = this.data.slots || {};
    for (const [st, slot] of (this.data.shots || [])) {
      const age = this.t - st;
      if (age < 0 || age > this.MUZZLE_S) continue;
      const pos = posByName[slotNames[slot]];
      if (!pos) continue;
      const f = 1 - age / this.MUZZLE_S;
      ctx.globalAlpha = (pos.dim ? 0.25 : 1) * f;
      const x0 = pos.px + Math.cos(pos.ang) * 11;
      const y0 = pos.py + Math.sin(pos.ang) * 11;
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x0 + Math.cos(pos.ang) * 8, y0 + Math.sin(pos.ang) * 8);
      ctx.lineWidth = 3;
      ctx.strokeStyle = "#ffe9a0";
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(x0, y0, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = "#fff6d8";
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // bomb drawn AFTER the players so its icon is never hidden under a dot;
    // follows the carrier between keyframes, static once planted/dropped
    const bombA = a.bomb, bombB = b.bomb || a.bomb;
    if (bombA) {
      const bx = bombA.x + ((bombB.x - bombA.x) * frac || 0);
      const by = bombA.y + ((bombB.y - bombA.y) * frac || 0);
      const bz = bombA.z + ((bombB.z - bombA.z) * frac || 0);
      const bpx = (bx - c.pos_x) / c.scale;
      const bpy = (c.pos_y - by) / c.scale;
      const bLower = lm !== null && lm !== undefined && bz < lm;
      const bDim = this.mapInfo.has_lower && ((this.level === "lower") !== bLower);
      ctx.globalAlpha = bDim ? 0.25 : 1;
      const icon = BombIcon;
      if (icon.complete && icon.naturalWidth) {
        const ih = 20;
        const iw = ih * (icon.naturalWidth / icon.naturalHeight);
        ctx.drawImage(icon, bpx - iw / 2, bpy - ih / 2, iw, ih);
      } else {
        ctx.save();
        ctx.translate(bpx, bpy);
        ctx.rotate(Math.PI / 4);
        ctx.fillStyle = a.bomb_planted ? "#ff4136" : "#ffcc33";
        ctx.fillRect(-6, -6, 12, 12);
        ctx.restore();
      }
      if (a.bomb_planted) {
        ctx.beginPath();
        ctx.arc(bpx, bpy, 14, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(255,65,54,0.7)";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // HUD round clock: counts down round time, then the 40s bomb timer after plant
    const clock = document.getElementById("pb-clock");
    const { text: clockText, danger } = this.clockAt(this.t);
    clock.innerHTML =
      (danger ? '<img class="pb-clock-bomb" src="icons/weapons/c4.svg" alt="bomb"> ' : "") +
      clockText;
    clock.classList.toggle("danger", danger);
    this.updateBoxes(a);
    this.updateResult();

    // fixed-width readout: the plant icon must not resize this element, or the
    // scrub (and every kill marker on it) shifts mid-round
    document.getElementById("pb-elapsed").textContent = fmtClock(Math.floor(this.t));
    document.getElementById("pb-bomb-ind").style.visibility =
      a.bomb_planted ? "visible" : "hidden";
    this.drawKillFeed();
  },

  // round clock text at playback time t (bomb timer once planted)
  clockAt(t) {
    if (this.plantT !== null && t >= this.plantT) {
      return { text: fmtClock(Math.ceil(Math.max(0, 40 - (t - this.plantT)))), danger: true };
    }
    return { text: fmtClock(Math.ceil(Math.max(0, (App.roundClockS || 115) - t))), danger: false };
  },

  // match-to-date kills/deaths/assists: totals from earlier rounds (server) plus
  // this round's kills up to time t (team kills score -1)
  kdAt(t) {
    const kd = {};
    const get = (n) => (kd[n] = kd[n] || { k: 0, d: 0, a: 0 });
    for (const [name, s] of Object.entries(this.data.match_kda || {})) {
      Object.assign(get(name), { k: s.k, d: s.d, a: s.a });
    }
    for (const k of (this.data.kills || [])) {
      if (k.t > t) break;
      if (k.victim) get(k.victim).d += 1;
      if (k.attacker && k.attacker !== k.victim) {
        get(k.attacker).k += k.attacker_side === k.victim_side ? -1 : 1;
      }
      if (k.assister) get(k.assister).a += 1;
    }
    return kd;
  },

  WIN_REASON_LABELS: {
    t_killed: "Elimination", ct_killed: "Elimination", bomb_defused: "Bomb defused",
    bomb_exploded: "Bomb detonated", time_ran_out: "Time expired",
  },

  // end-of-round banner: who won and how
  updateResult() {
    const el = document.getElementById("pb-result");
    const r = this.data.round;
    const endS = r.end_s !== undefined && r.end_s !== null ? r.end_s : Infinity;
    if (!r.winner || this.t < endS) {
      el.hidden = true;
      return;
    }
    const isCT = r.winner === "CT";
    const team = isCT ? r.ct_team : r.t_team;
    el.className = isCT ? "ct" : "t";
    el.innerHTML = "";
    const who = document.createElement("div");
    who.className = "pb-result-team";
    who.textContent = r.has_teams ? `${team} win` : `${r.winner} win`;
    const how = document.createElement("div");
    how.className = "pb-result-how";
    how.textContent = (this.WIN_REASON_LABELS[r.win_reason] || "Round over") +
      (r.has_teams ? ` · ${r.winner} side` : "");
    el.appendChild(who);
    el.appendChild(how);
    el.hidden = false;
  },

  UTIL_FLASH_MASK: 0x03,
  UTIL_ICON_BITS: [[0x04, "smokegrenade"], [0x08, "hegrenade"], [0x20, "decoy"]],

  // one card per player, built once per round and updated in place afterwards:
  // re-rendering the markup every second reloads every <img> and makes the
  // inventory icons flicker
  buildCards(frame) {
    this.cards = {};
    for (const side of ["CT", "T"]) {
      const box = document.getElementById(side === "CT" ? "pb-side-ct" : "pb-side-t");
      box.innerHTML = "";
      for (const p of frame.players.filter((pl) => pl.side === side)) {
        const el = document.createElement("div");
        el.className = `pb-player ${side.toLowerCase()}`;
        el.innerHTML = `
          <div class="pp-top"><span class="pp-nwrap"><span class="pp-name"></span><img
              class="pp-c4" src="icons/weapons/c4.svg" alt="bomb"
              data-tip="Carrying the bomb" hidden></span>
            <span class="pp-kd" data-tip="Kills / Deaths / Assists"></span></div>
          <div class="pp-hprow"><div class="pp-hp"><div class="pp-hpbar"></div></div>
            <span class="pp-hpnum"></span></div>
          <div class="pp-econ"><span class="pp-armor-wrap"></span>
            <span class="pp-money" data-tip="Money in reserve"></span></div>
          <div class="pp-inv"></div>`;
        el.querySelector(".pp-name").textContent = p.name;
        box.appendChild(el);
        this.cards[p.name] = {
          root: el,
          c4: el.querySelector(".pp-c4"),
          kd: el.querySelector(".pp-kd"),
          hpbar: el.querySelector(".pp-hpbar"),
          hpnum: el.querySelector(".pp-hpnum"),
          econ: el.querySelector(".pp-econ"),
          armor: el.querySelector(".pp-armor-wrap"),
          money: el.querySelector(".pp-money"),
          inv: el.querySelector(".pp-inv"),
          invSig: null,
          armorSig: null,
        };
      }
    }
  },

  // an icon that 404s once is never requested again (that retry loop was the
  // source of per-second flicker on mis-mapped weapon names)
  badIcons: new Set(),

  icon(name, cls) {
    if (!name || this.badIcons.has(name)) return null;
    const img = document.createElement("img");
    img.className = cls;
    img.src = `icons/weapons/${name}.svg`;
    img.alt = name;
    img.dataset.tip = name;
    img.addEventListener("error", () => {
      this.badIcons.add(name);
      img.remove();
    }, { once: true });
    return img;
  },

  // the bomb rides exactly on its carrier: the alive T nearest it (planted or
  // dropped bombs have no carrier, so nobody gets the icon)
  carrierOf(frame) {
    if (!frame.bomb || frame.bomb_planted) return null;
    let best = null;
    let bd = 40;
    for (const p of frame.players) {
      if (!p.alive || p.side !== "T") continue;
      const d = Math.hypot(p.x - frame.bomb.x, p.y - frame.bomb.y);
      if (d < bd) { bd = d; best = p.name; }
    }
    return best;
  },

  // side panels: name, K/D/A (match totals), health, armor, money, inventory;
  // values refresh when the 1 Hz frame or the kill count changes
  updateBoxes(frame) {
    const killCount = (this.data.kills || []).filter((k) => k.t <= this.t).length;
    const key = `${frame.t}:${killCount}`;
    if (this.boxSec === key) return;
    this.boxSec = key;
    if (!this.cards || Object.keys(this.cards).some((n) => !document.body.contains(this.cards[n].root))) {
      this.buildCards(frame);
    }
    const kd = this.kdAt(this.t);
    const dead = this.deadAt(this.t);
    const carrier = this.carrierOf(frame);
    for (const p of frame.players) {
      const c = this.cards[p.name];
      if (!c) continue;
      c.c4.hidden = p.name !== carrier;
      const s = kd[p.name] || { k: 0, d: 0, a: 0 };
      c.kd.textContent = `${s.k} / ${s.d} / ${s.a}`;
      const alive = p.alive && !dead.has(p.name);
      c.root.classList.toggle("dead", !alive);
      const hp = alive ? p.health : 0;
      c.hpbar.style.width = `${hp}%`;
      c.hpbar.className = "pp-hpbar" + (hp > 50 ? "" : hp > 20 ? " low" : " crit");
      c.hpnum.textContent = hp;
      const hasEcon = p.util !== undefined && alive;
      c.econ.hidden = !hasEcon;
      c.inv.hidden = !hasEcon;
      if (!hasEcon) continue;

      const helm = p.util & 0x80;
      const armorSig = p.armor > 0 ? `${helm ? "h" : "a"}${p.armor}` : "";
      if (c.armorSig !== armorSig) {
        c.armorSig = armorSig;
        c.armor.innerHTML = "";
        if (p.armor > 0) {
          const img = this.icon(helm ? "armor_helmet" : "armor", "pp-armor");
          if (img) {
            img.dataset.tip = `Armor${helm ? " + helmet" : ""}`;
            c.armor.appendChild(img);
          }
          c.armor.appendChild(document.createTextNode(` ${p.armor}`));
        }
      }
      c.money.textContent = `$${(p.money || 0).toLocaleString()}`;

      const flashes = p.util & this.UTIL_FLASH_MASK;
      const invSig = `${p.prim}|${p.sec}|${p.util}`;
      if (c.invSig !== invSig) {
        c.invSig = invSig;
        c.inv.innerHTML = "";
        const add = (name) => {
          const img = this.icon(name, "pp-wpn");
          if (img) c.inv.appendChild(img);
        };
        add(p.prim);
        add(p.sec);
        if (flashes) {
          add("flashbang");
          if (flashes > 1) {
            const x = document.createElement("span");
            x.className = "pp-x";
            x.textContent = `×${flashes}`;
            c.inv.appendChild(x);
          }
        }
        for (const [bit, ic] of this.UTIL_ICON_BITS) if (p.util & bit) add(ic);
        if (p.util & 0x10) add(p.side === "CT" ? "incgrenade" : "molotov");
        if (p.util & 0x40) add("defuser");
      }
    }
  },

  KF_LINGER_S: 8,          // how long a kill stays in the feed (playback seconds)
  KF_MAX: 6,
  MUZZLE_S: 0.14,          // muzzle-flash lifetime (playback seconds)

  // timeline ticks for every kill, colored by the side that got it (a kill is
  // credited to the side that did not lose the player, so team kills read right)
  drawKillMarks(maxT) {
    const wrap = document.getElementById("pb-kill-marks");
    wrap.innerHTML = "";
    if (!maxT) return;
    for (const k of (this.data.kills || [])) {
      const side = k.victim_side === "CT" ? "t" : k.victim_side === "T" ? "ct" : "none";
      const mark = document.createElement("span");
      mark.className = `pb-kmark ${side}`;
      mark.style.left = `${Math.min(100, (k.t / maxT) * 100)}%`;
      mark.dataset.tip = `${this.clockAt(k.t).text} · ${k.attacker || "world"} → ${k.victim}`;
      wrap.appendChild(mark);
    }
  },

  drawKillFeed() {
    const feed = document.getElementById("pb-killfeed");
    const kills = (this.data.kills || []).filter(
      (k) => k.t <= this.t && this.t - k.t <= this.KF_LINGER_S);
    const shown = kills.slice(-this.KF_MAX);
    const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const cls = (side) => side === "CT" ? "ct" : "t";
    feed.innerHTML = shown.map((k) => {
      const attacker = k.attacker
        ? `<span class="${cls(k.attacker_side)}">${esc(k.attacker)}</span>`
        : `<span class="wpn">world</span>`;
      const wpn = k.weapon
        ? `<img class="kf-wpn" src="icons/weapons/${esc(k.weapon)}.svg" alt="${esc(k.weapon)}" ` +
          `onerror="this.replaceWith(this.alt)">`
        : `<span class="wpn">?</span>`;
      return `<div class="kf-entry"><span class="kf-time">${this.clockAt(k.t).text}</span>` +
        `${attacker}${wpn}` +
        (k.headshot
          ? '<img class="kf-hs" src="icons/killfeed/icon_headshot.svg" alt="headshot" title="Headshot">'
          : "") +
        `<span class="${cls(k.victim_side)}">${esc(k.victim)}</span></div>`;
    }).join("");
  },
};
