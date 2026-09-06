"use strict";
/* Result cards with mini-canvas snapshots + the collapsible scenario-stats strip. */

const Results = {
  render(res, mapInfo, filters) {
    const list = document.getElementById("results-list");
    const title = document.getElementById("results-title");
    list.innerHTML = "";
    const n = res.moments.length;
    title.innerHTML = "";
    title.appendChild(document.createTextNode(`Results — ${n} scenario${n === 1 ? "" : "s"}`));
    const sub = document.createElement("span");
    sub.className = "results-sub";
    sub.textContent = `${res.n_scanned.toLocaleString()} states scanned · ${res.elapsed_ms} ms`;
    title.appendChild(sub);
    Stats.render(res, filters);
    if (!n) {
      list.innerHTML = '<div class="muted small">No matches. Loosen the tolerance or remove filters.</div>';
      return;
    }
    if (res.spatial) {
      const exact = res.moments.filter((m) => m.exact);
      const close = res.moments.filter((m) => !m.exact);
      // a round can produce several scenarios; count both so the section headers
      // line up with the stats strip, which counts rounds. When the result cap
      // truncates a section, say so ("48 of 71 rounds") instead of mismatching.
      const tally = (arr, statRounds) => {
        const rounds = new Set(arr.map((m) => m.round_id)).size;
        if (arr.length === rounds && rounds >= (statRounds || 0)) return `${arr.length}`;
        const all = statRounds > rounds ? `${rounds} of ${statRounds}` : `${rounds}`;
        return `${arr.length} scenarios · ${all} rounds`;
      };
      const sr = res.stats ? res.stats.rounds : {};
      if (exact.length) {
        list.appendChild(this.sectionHeader(`Exact matches (${tally(exact, sr.exact)})`));
        for (const m of exact) list.appendChild(this.card(m, mapInfo, true));
      }
      if (close.length) {
        list.appendChild(this.sectionHeader(exact.length
          ? `Close matches (${tally(close, sr.close)})`
          : `Closest matches (${tally(close, sr.close)})`));
        for (const m of close) list.appendChild(this.card(m, mapInfo, true));
      }
    } else {
      for (const m of res.moments) {
        list.appendChild(this.card(m, mapInfo, false));
      }
    }
    // a full page means the ranking was cut off - offer the next page
    if (n >= (App.searchLimit || 50)) {
      const more = document.createElement("button");
      more.id = "load-more";
      more.textContent = "Load more scenarios";
      more.addEventListener("click", () => App.loadMore());
      list.appendChild(more);
    }
  },

  sectionHeader(text) {
    const el = document.createElement("div");
    el.className = "result-section";
    el.textContent = text;
    return el;
  },

  card(m, mapInfo, spatial = true) {
    const el = document.createElement("div");
    el.className = "result-card";
    const cv = document.createElement("canvas");
    cv.width = 240; cv.height = 240;
    el.appendChild(cv);
    this.drawSnapshot(cv, m.snapshot, mapInfo);

    // times read as the round clock counting down; post-plant moments have no
    // round clock, so they are labeled instead of given fake numbers
    const fmtLeft = (s) => fmtClock((App.roundClockS || 115) - s);
    const when = m.snapshot.bomb_planted
      ? "post-plant"
      : `${fmtLeft(m.t_start)}&ndash;${fmtLeft(m.t_end)} left`;
    const meta = document.createElement("div");
    meta.className = "result-meta";
    const bombBadge = m.snapshot.bomb_planted ? '<span class="badge bomb">bomb</span>' : "";
    const site = m.bomb_site ? `<span class="badge">site ${m.bomb_site}</span>` : "";
    const scoreBadge = spatial
      ? `<span class="badge score" data-tip="Average distance between your markers and the ` +
        `matched positions, in game units">~${Math.round(m.pos_score)}u</span>`
      : "";
    const matchup = (m.team1 && m.team1 !== "unknown")
      ? `${m.team1}${m.team2 ? " vs " + m.team2 : ""}`
      : m.demo.replace(".dem", "");
    meta.innerHTML = `
      <div class="demo"></div>
      <div class="row">Round ${m.round_num} &middot; ${when}</div>
      <div class="row">${scoreBadge}${bombBadge}${site}</div>
      <div class="row">CT ${m.ct_buy} vs T ${m.t_buy}${m.is_pistol ? " · pistol" : ""}</div>
      <div class="row">winner: <span class="win ${m.winner === "CT" ? "ct" : m.winner === "T" ? "t" : ""}">${m.winner || "?"}</span></div>`;
    meta.querySelector(".demo").textContent = matchup;
    meta.querySelector(".demo").title = m.demo;
    el.appendChild(meta);
    // start playback just before the snapshot moment shown on the card,
    // not the beginning of the whole matched window
    const startAt = Math.max(m.t_start, (m.snapshot.round_time_s || m.t_start) - 3);
    el.addEventListener("click", () => Playback.open(m.round_id, startAt, mapInfo));
    return el;
  },

  async drawSnapshot(cv, snap, mapInfo) {
    const ctx = cv.getContext("2d");
    const img = await loadRadar(mapInfo.map_name, "upper");
    ctx.fillStyle = "#0a0d10";
    ctx.fillRect(0, 0, cv.width, cv.height);
    if (img) ctx.drawImage(img, 0, 0, cv.width, cv.height);
    const c = mapInfo.calibration;
    const s = cv.width / 1024;
    for (const u of (snap.utility || [])) {
      const px = ((u.x - c.pos_x) / c.scale) * s;
      const py = ((c.pos_y - u.y) / c.scale) * s;
      const r = ((u.type === "smoke" ? UTIL.SMOKE_RADIUS_U : UTIL.MOLLY_RADIUS_U) / c.scale) * s;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fillStyle = UTIL.COLORS[u.type];
      ctx.fill();
    }
    for (const p of snap.players) {
      const px = ((p.x - c.pos_x) / c.scale) * s;
      const py = ((c.pos_y - p.y) / c.scale) * s;
      ctx.globalAlpha = p.alive ? 1 : 0.45;
      ctx.beginPath();
      if (p.alive) {
        ctx.arc(px, py, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = p.side === "CT" ? "#4a9eff" : "#ffa64a";
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.strokeStyle = "#0b0f14";
        ctx.stroke();
      } else {
        ctx.strokeStyle = p.side === "CT" ? "#4a9eff" : "#ffa64a";
        ctx.lineWidth = 1.5;
        ctx.moveTo(px - 3, py - 3); ctx.lineTo(px + 3, py + 3);
        ctx.moveTo(px + 3, py - 3); ctx.lineTo(px - 3, py + 3);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    this.drawBomb(ctx, snap, c, s);
  },

  // Bomb in the preview. Keep only the planted ring: carrier proximity at the
  // 1 Hz sample boundary made the dashed dropped indicator flicker.
  drawBomb(ctx, snap, c, s) {
    if (!snap.bomb) return;
    const bpx = ((snap.bomb.x - c.pos_x) / c.scale) * s;
    const bpy = ((c.pos_y - snap.bomb.y) / c.scale) * s;
    const draw = () => {
      const ih = 12;
      const iw = BombIcon.naturalWidth
        ? ih * (BombIcon.naturalWidth / BombIcon.naturalHeight) : ih;
      ctx.drawImage(BombIcon, bpx - iw / 2, bpy - ih / 2, iw, ih);
      ctx.beginPath();
      ctx.arc(bpx, bpy, 8, 0, Math.PI * 2);
      ctx.lineWidth = 1.5;
      if (snap.bomb_planted) {
        ctx.strokeStyle = "rgba(255,65,54,0.8)";
        ctx.stroke();
      }
    };
    if (BombIcon.complete && BombIcon.naturalWidth) draw();
    else BombIcon.addEventListener("load", draw, { once: true });
  },
};

/* Instant tooltip: elements carry data-tip; shown on hover with no OS delay. */
const Tip = {
  el: null,
  init() {
    if (this.el) return;
    this.el = document.createElement("div");
    this.el.id = "dq-tip";
    this.el.hidden = true;
    document.body.appendChild(this.el);
    document.addEventListener("mouseover", (e) => {
      const t = e.target.closest("[data-tip]");
      if (!t) { this.el.hidden = true; return; }
      this.el.textContent = t.dataset.tip;
      this.el.hidden = false;
    });
    document.addEventListener("mousemove", (e) => {
      if (this.el.hidden) return;
      const pad = 12;
      const r = this.el.getBoundingClientRect();
      let x = e.clientX + pad;
      let y = e.clientY + pad;
      if (x + r.width > innerWidth - 4) x = e.clientX - r.width - pad;
      if (y + r.height > innerHeight - 4) y = e.clientY - r.height - pad;
      this.el.style.left = `${x}px`;
      this.el.style.top = `${y}px`;
    });
    document.documentElement.addEventListener("mouseleave", () => { this.el.hidden = true; });
  },
};

/* Scenario stats: round-level aggregates over the FULL match population (the
   server computes them before result truncation). Winrate shows exact and
   close buckets; every other block is exact-match rounds only. */
const Stats = {
  KEY: "dq_stats_open",
  BUYS: ["pistol", "eco", "semi", "full"],
  BUY_LABELS: { pistol: "Pistol", eco: "Eco", semi: "Semi", full: "Full" },
  REASON_LABELS: {
    t_killed: "Elimination", bomb_defused: "Defuse", time_ran_out: "Time",
    ct_killed: "Elimination", bomb_exploded: "Detonation", unknown: "Unknown",
  },
  REASON_COLORS: {
    t_killed: "#4a9eff", bomb_defused: "#2c6cb8", time_ran_out: "#1d5488",
    ct_killed: "#ffa64a", bomb_exploded: "#c97b2e", unknown: "#566274",
  },

  clear() {
    const strip = document.getElementById("stats-strip");
    strip.hidden = true;
    strip.innerHTML = "";
  },

  render(res, filters) {
    Tip.init();
    const strip = document.getElementById("stats-strip");
    strip.innerHTML = "";
    const s = res.stats;
    // no cards -> no stats: with zero visible results the strip would summarize
    // rounds the user cannot open (stage-2 can reject every stage-1 candidate)
    if (!s || !res.moments.length || s.rounds.exact + s.rounds.close === 0) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;

    let open = true;
    try { open = localStorage.getItem(this.KEY) !== "0"; } catch (e) { /* ignore */ }

    const toggle = document.createElement("button");
    toggle.id = "stats-toggle";
    const wr = s.winrate.exact;
    const n = wr.ct + wr.t;
    const headline = n
      ? `${res.spatial ? "exact: " : ""}CT ${this.pct(wr.ct, n)}% · T ${this.pct(wr.t, n)}%` +
        ` (${this.plural(n, "round")})`
      : "no exact matches";
    toggle.innerHTML = '<span class="chev"></span>Scenario stats<span class="stats-headline"></span>';
    toggle.querySelector(".stats-headline").textContent = headline;
    toggle.dataset.tip = `${s.rounds.exact} exact + ${s.rounds.close} close rounds` +
      ` across ${this.plural(s.rounds.demos, "demo")}`;

    const body = document.createElement("div");
    body.id = "stats-body";
    const setOpen = (o) => {
      body.hidden = !o;
      toggle.querySelector(".chev").textContent = o ? "▾" : "▸";
      toggle.setAttribute("aria-expanded", String(o));
    };
    setOpen(open);
    toggle.addEventListener("click", () => {
      setOpen(body.hidden);
      try { localStorage.setItem(this.KEY, body.hidden ? "0" : "1"); } catch (e) { /* ignore */ }
    });
    strip.appendChild(toggle);
    strip.appendChild(body);

    this.winrate(body, s, res.spatial);
    if (s.rounds.exact) {
      this.timing(body, s);
      this.reasons(body, s);
      this.nextKill(body, s);
      this.economy(body, s, filters);
    } else {
      const note = this.row(body, "muted small stats-note");
      note.textContent = "No exact matches — the blocks below need at least one. " +
        "Winrate above still covers the close matches.";
    }
  },

  pct(x, total) { return total ? Math.round((100 * x) / total) : 0; },

  plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; },

  // legend entry: colour swatch + "Label: N", so every shade is identifiable
  legend(parent, items) {
    const leg = this.row(parent, "stats-legend muted small");
    for (const it of items) {
      const chip = document.createElement("span");
      chip.className = `leg-chip${it.off ? " off" : ""}`;
      const sw = document.createElement("span");
      sw.className = `sw${it.cls ? ` ${it.cls}` : ""}`;
      if (it.color) sw.style.background = it.color;
      chip.appendChild(sw);
      chip.appendChild(document.createTextNode(`${it.label}: ${it.value}`));
      if (it.tip) chip.dataset.tip = it.tip;
      leg.appendChild(chip);
    }
    return leg;
  },

  sec(body, label, hint) {
    const h = document.createElement("div");
    h.className = "stats-sec";
    h.textContent = label;
    if (hint) h.dataset.tip = hint;
    body.appendChild(h);
  },

  row(parent, cls) {
    const d = document.createElement("div");
    d.className = cls;
    parent.appendChild(d);
    return d;
  },

  // proportional split bar; segs = [{cls, count, tip, label?}] (0-count segments dropped)
  bar(parent, segs) {
    const bar = document.createElement("div");
    bar.className = "stats-bar";
    const total = segs.reduce((a, x) => a + x.count, 0);
    for (const seg of segs) {
      if (!seg.count) continue;
      const el = document.createElement("span");
      el.className = `seg ${seg.cls}`;
      el.style.flexGrow = seg.count;
      if (seg.tip) el.dataset.tip = seg.tip;
      const p = this.pct(seg.count, total);
      if (seg.label !== false && p >= 14) el.textContent = `${p}%`;
      bar.appendChild(el);
    }
    parent.appendChild(bar);
    return bar;
  },

  labeledBar(body, label, segs) {
    const row = this.row(body, "stats-row");
    const lab = document.createElement("span");
    lab.className = "stats-lab";
    lab.textContent = label;
    row.appendChild(lab);
    this.bar(row, segs);
    return row;
  },

  winrate(body, s, spatial) {
    this.sec(body, "Win Rate per Side",
      "Round winners among matches. A round with any exact-position second counts as exact.");
    const rowFor = (name, w) => {
      const total = w.ct + w.t;
      if (!total) return;
      const row = this.labeledBar(body, `${name} · ${total}`, [
        { cls: "ct", count: w.ct, tip: `CT wins ${w.ct} of ${total}` },
        { cls: "t", count: w.t, tip: `T wins ${w.t} of ${total}` },
      ]);
      const cnt = document.createElement("span");
      cnt.className = "stats-count";
      cnt.textContent = `${w.ct}–${w.t}`;
      row.appendChild(cnt);
    };
    rowFor(spatial ? "Exact" : "Matches", s.winrate.exact);
    if (spatial) rowFor("Close", s.winrate.close);
  },

  timing(body, s) {
    const t = s.timing;
    const nExact = s.rounds.exact;
    const of = (c) => `${c} of ${this.plural(nExact, "round")}`;
    this.sec(body, "Round Time Distribution",
      `Rounds where the scenario is live during each ${t.bin_s}s of the round clock; PP = bomb planted.`);
    const maxC = Math.max(1, ...t.pre, t.post_plant);
    const hist = this.row(body, "stats-hist");
    const col = (c, cls, tip) => {
      const holder = document.createElement("div");
      holder.className = `hcol${cls ? ` ${cls}` : ""}`;
      const lab = document.createElement("span");
      lab.className = "hpct";
      lab.textContent = c ? `${this.pct(c, nExact)}%` : "";
      const bar = document.createElement("div");
      bar.className = "hbar";
      bar.style.height = `${Math.max(3, Math.round(30 * (c / maxC)))}px`;
      if (!c) bar.classList.add("zero");
      holder.dataset.tip = tip;
      holder.appendChild(lab);
      holder.appendChild(bar);
      hist.appendChild(holder);
    };
    t.pre.forEach((c, i) => {
      const hi = t.clock_s - i * t.bin_s;
      const lo = i === t.pre.length - 1 ? 0 : Math.max(0, hi - t.bin_s + 1);
      col(c, "", `${fmtClock(hi)}–${fmtClock(lo)} left · ${of(c)}`);
    });
    col(t.post_plant, "pp", `post-plant · ${of(t.post_plant)}`);
    const axis = this.row(body, "stats-axis");
    axis.innerHTML = '<span class="ax-main"><span></span><span></span></span><span class="ax-pp">PP</span>';
    axis.querySelector(".ax-pp").dataset.tip = "Post-plant — rounds live after the bomb is down";
    axis.querySelector(".ax-main").style.flex = String(t.pre.length);  // match the bar count
    const [a0, a1] = axis.querySelectorAll(".ax-main span");
    a0.textContent = fmtClock(t.clock_s);
    a1.textContent = "0:00";
  },

  reasons(body, s) {
    this.sec(body, "Win Method per Side", "How the winning side closed the exact-match rounds.");
    const wrap = this.row(body, "stats-pies");
    for (const side of ["ct", "t"]) {
      const entries = Object.entries(s.win_reasons[side]).filter(([, v]) => v > 0);
      const total = entries.reduce((a, [, v]) => a + v, 0);
      const col = document.createElement("div");
      col.className = "pie-col";
      const head = document.createElement("div");
      head.className = `pie-head ${side}`;
      head.textContent = `${side.toUpperCase()} wins · ${total}`;
      col.appendChild(head);
      if (total) {
        const legText = entries.map(([k, v]) => `${this.REASON_LABELS[k]}: ${v}`).join(" | ");
        const pie = document.createElement("div");
        pie.className = "pie";
        let acc = 0;
        const stops = entries.map(([k, v]) => {
          const from = (acc / total) * 360;
          acc += v;
          return `${this.REASON_COLORS[k]} ${from}deg ${(acc / total) * 360}deg`;
        });
        pie.style.background = `conic-gradient(${stops.join(", ")})`;
        pie.dataset.tip = legText;
        col.appendChild(pie);
        const leg = this.legend(col, entries.map(([k, v]) => ({
          label: this.REASON_LABELS[k], value: v, color: this.REASON_COLORS[k],
          tip: `${this.pct(v, total)}% of ${side.toUpperCase()} wins`,
        })));
        leg.classList.add("pie-leg");
        if (s.win_reasons[side].unknown) {
          leg.dataset.tip = App.demoMode
            ? "Win reasons are not indexed for part of this library yet."
            : "Some demos predate win-reason indexing — a demo scan backfills them.";
        }
      } else {
        const leg = document.createElement("div");
        leg.className = "muted small pie-leg";
        leg.textContent = "No wins";
        col.appendChild(leg);
      }
      wrap.appendChild(col);
    }
  },

  nextKill(body, s) {
    const nk = s.next_kill;
    const total = nk.ct + nk.t + nk.none;
    if (!total) return;
    this.sec(body, "Side to get Next Kill",
      "Who takes the next kill after the scenario first appears (credited to the side that does not lose the player).");
    this.bar(this.row(body, "stats-row"), [
      { cls: "ct", count: nk.ct, tip: `CT takes the next kill in ${nk.ct} of ${total} rounds` },
      { cls: "t", count: nk.t, tip: `T takes the next kill in ${nk.t} of ${total} rounds` },
      { cls: "none", count: nk.none, tip: `No further kills in ${nk.none} of ${total} rounds` },
    ]);
    const items = [
      { label: "CT", value: nk.ct, cls: "ct" },
      { label: "T", value: nk.t, cls: "t" },
      { label: "None", value: nk.none, cls: "none" },
    ];
    if (nk.median_s !== null) {
      items.push({ label: "Median", value: `${nk.median_s}s`, cls: "blank",
                   tip: "Median seconds from the scenario to that kill" });
    }
    this.legend(body, items);
  },

  economy(body, s, filters) {
    this.sec(body, "Economy Distribution", "Buy types of the exact-match rounds.");
    for (const side of ["ct", "t"]) {
      const e = s.economy[side];
      const chosen = (filters && filters[`${side}_buy`]) || [];
      // an empty buy filter means "no filter" server-side: treat as all selected
      const selected = new Set(chosen.length ? chosen : this.BUYS);
      const row = this.labeledBar(body, side.toUpperCase(),
        this.BUYS.map((b) => ({
          cls: `buy-${side}-${b}`, count: e[b], label: false,
          tip: `${this.BUY_LABELS[b]}: ${e[b]}`,
        })));
      row.classList.add("econ-row");          // narrow label: "CT"/"T" needs no column
      row.querySelector(".stats-lab").classList.add(side);
      this.legend(body, this.BUYS.map((b) => ({
        label: this.BUY_LABELS[b], value: e[b], cls: `buy-${side}-${b}`,
        off: !selected.has(b),
        tip: selected.has(b) ? null : `Excluded by your ${side.toUpperCase()} Buy filter`,
      }))).classList.add("buy-legend");
    }
  },
};
