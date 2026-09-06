"use strict";
/* App orchestration: map list, controls, search, scan progress. */

const App = {
  roundClockS: 115,        // pre-plant round clock (1:55); server value wins if it differs
  maps: [],
  current: null,
  scanTimer: null,
  searchSeq: 0,
  mapSeq: 0,
  teamsSeq: 0,
  ready: false,

  demoMode: false,
  indexedOk: null,          // demos indexed OK; null until first /api/demos fetch

  async init() {
    Sketch.init();
    Playback.init();
    this.bindControls();
    try {
      const h = await API.get("/api/health");
      this.demoMode = !!h.demo_mode;
      this.shareBase = h.share_base;
      if (h.round_clock_s && h.round_clock_s !== this.roundClockS) {
        this.roundClockS = h.round_clock_s;
        this.populateClock();
      }
      if (this.demoMode) {
        document.body.classList.add("demo-mode");
        // logo navigates the embedding page to the site (inert in the desktop app)
        const logo = document.getElementById("site-logo-link");
        logo.href = "https://cs2analysis.com/agents/compare";
        logo.target = "_top";
      }
    } catch (e) { throw new Error("Could not connect to DemoQuery. Reload to try again."); }
    this.checkUpdate();
    await this.maybeShowTutorial();
    await this.refreshMaps();
    this.ready = true;
    if (window.parent !== window) window.parent.postMessage({ dqReady: true }, "*");
    await this.refreshDemoCount();
    if (!this.demoMode) { await this.refreshDemosDir(); this.pollScanOnce(); }
    if (this.demoMode) {
      document.getElementById("demo-note").textContent = "Preloaded pro demo library";
      try {
        const library = await API.get("/api/library-summary");
        document.getElementById("demo-note").textContent =
          `2026 pro matches — ${library.n_demos} demos preloaded`;
      } catch (e) { /* keep the generic preloaded-library note */ }
      // site-hosted HLTV mark for the (web-only) button; the offline app never
      // sets this src, so it stays free of external requests
      const hlogo = document.getElementById("pb-hltv-logo");
      hlogo.addEventListener("load", () => { hlogo.hidden = false; });
      hlogo.addEventListener("error", () => hlogo.remove());
      hlogo.src = "https://cs2analysis.com/icons/dq/hltv.png";
    }
  },

  bindControls() {
    TeamCores.init();
    document.getElementById("map-select").addEventListener("change", (e) => this.selectMap(e.target.value));
    for (const s of ["ct", "t", "smoke", "molly"]) {
      document.getElementById(`side-${s}`).addEventListener("click", () => this.setSide(s));
    }
    document.getElementById("clear-btn").addEventListener("click", () => {
      Sketch.markers = { ct: [], t: [], smoke: [], molly: [] };
      Sketch.render();
      this.updateMarkerSummary();
    });
    document.getElementById("level-upper").addEventListener("click", () => this.setLevel("upper"));
    document.getElementById("level-lower").addEventListener("click", () => this.setLevel("lower"));
    document.getElementById("dist-slider").addEventListener("input", (e) => {
      document.getElementById("dist-val").textContent =
        e.target.value === "0" ? "exact" : `${e.target.value}u`;
    });
    document.querySelectorAll(".buys button, #f-util button").forEach((b) =>
      b.addEventListener("click", () => b.classList.toggle("on")));
    // phase and site are pick-one: no combination of them is ever invalid
    for (const id of ["f-phase", "f-site"]) {
      document.querySelectorAll(`#${id} button`).forEach((b) =>
        b.addEventListener("click", () => {
          document.querySelectorAll(`#${id} button`).forEach((o) => o.classList.remove("on"));
          b.classList.add("on");
          this.updatePhaseUI();
        }));
    }
    // team side is pick-one too, but doesn't touch the phase/site UI
    document.querySelectorAll("#f-team-side button").forEach((b) =>
      b.addEventListener("click", () => {
        document.querySelectorAll("#f-team-side button").forEach((o) => o.classList.remove("on"));
        b.classList.add("on");
      }));
    document.getElementById("f-team").addEventListener("change", () => this.updateTeamUI());
    document.getElementById("show-zones").addEventListener("change", async (e) => {
      Sketch.showOverlay = e.target.checked;
      if (Sketch.showOverlay) await Sketch.loadNodes().catch(() => {});
      Sketch.render();
    });
    document.getElementById("clear-index-btn").addEventListener("click", () => this.clearIndex());
    for (const side of ["ct", "t"]) {
      const mn = document.getElementById(`f-alive-${side}-min`);
      const mx = document.getElementById(`f-alive-${side}-max`);
      for (let i = 0; i <= 5; i++) {
        mn.add(new Option(String(i), i));
        mx.add(new Option(String(i), i));
      }
      mn.value = 0; mx.value = 5;
    }
    this.populateClock();
    // the reset button lives inside the <summary>: don't let its click also
    // toggle the details element open/closed
    document.getElementById("f-reset").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.resetFilters();
    });
    document.getElementById("search-btn").addEventListener("click", () => this.search());
    document.getElementById("scan-btn").addEventListener("click", () => this.startScan());
    document.getElementById("open-demos-btn").addEventListener("click", async () => {
      try { await API.post("/api/open-demos-folder"); } catch (e) { /* ignore */ }
    });
    document.getElementById("pick-demos-btn").addEventListener("click", async () => {
      const btn = document.getElementById("pick-demos-btn");
      btn.disabled = true;
      try {
        const r = await API.post("/api/pick-demos-folder");
        if (!r.cancelled) this.onDemosDirChanged(r);
      } catch (e) {
        this.showDemosDirMsg("Couldn't open the folder picker.", true);
      } finally { btn.disabled = false; }
    });
    document.getElementById("reset-demos-btn").addEventListener("click", async () => {
      try { this.onDemosDirChanged(await API.post("/api/settings/demos-dir", { path: null })); }
      catch (e) { this.showDemosDirMsg("Couldn't reset the folder.", true); }
    });
    // settings modal
    const settings = document.getElementById("settings-modal");
    const closeSettings = () => { settings.hidden = true; };
    document.getElementById("settings-btn").addEventListener("click", () => this.openSettings());
    document.getElementById("demos-settings-link").addEventListener("click", () => this.openSettings());
    document.getElementById("settings-close").addEventListener("click", closeSettings);
    settings.addEventListener("click", (e) => { if (e.target === settings) closeSettings(); });
    document.getElementById("show-tutorial-launch").addEventListener("change", (e) =>
      this.setTutorialHidden(!e.target.checked));
    document.getElementById("check-updates").addEventListener("change", (e) =>
      API.post("/api/settings/ui", { check_updates: e.target.checked }).catch(() => {}));

    // tutorial: opens on launch unless "do not show again" was checked; reopen via help button
    const tut = document.getElementById("tutorial-modal");
    const closeTut = () => {
      tut.hidden = true;
      this.setTutorialHidden(document.getElementById("tutorial-dontshow").checked);
    };
    document.getElementById("help-btn").addEventListener("click", () => { tut.hidden = false; });
    document.getElementById("tutorial-close").addEventListener("click", closeTut);
    document.getElementById("tutorial-got-it").addEventListener("click", closeTut);
    tut.addEventListener("click", (e) => { if (e.target === tut) closeTut(); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const confirm = document.getElementById("confirm-modal");
      if (!confirm.hidden) document.getElementById("confirm-cancel").click();
      else if (!settings.hidden) closeSettings();
      else if (!tut.hidden) closeTut();
    });
  },

  openSettings() {
    document.getElementById("settings-modal").hidden = false;
    this.refreshIndexSize();
    TeamCores.load();
  },

  resetFilters() {
    document.querySelectorAll("#f-phase button").forEach((b) =>
      b.classList.toggle("on", b.dataset.phase === "all"));
    document.querySelectorAll("#f-site button").forEach((b) =>
      b.classList.toggle("on", b.dataset.site === "any"));
    document.querySelectorAll("#f-util button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".buys button").forEach((b) => b.classList.add("on"));
    document.getElementById("f-clock-from").value = this.roundClockS;
    document.getElementById("f-clock-to").value = 0;
    for (const side of ["ct", "t"]) {
      document.getElementById(`f-alive-${side}-min`).value = 0;
      document.getElementById(`f-alive-${side}-max`).value = 5;
    }
    document.getElementById("f-team").value = "";
    document.querySelectorAll("#f-team-side button").forEach((b) =>
      b.classList.toggle("on", b.dataset.tside === "any"));
    this.updateTeamUI();
    this.updatePhaseUI();
  },

  fmtBytes(b) {
    return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`;
  },

  // the clear button carries the index's on-disk size, so clearing is an
  // informed decision (an empty schema file is ~100KB — hide the noise)
  async refreshIndexSize() {
    if (this.demoMode) return;
    const btn = document.getElementById("clear-index-btn");
    try {
      const s = await API.get("/api/index/size");
      btn.textContent = s.total_bytes > 1e6
        ? `Clear indexed maps (${this.fmtBytes(s.total_bytes)})`
        : "Clear indexed maps";
    } catch (e) {
      btn.textContent = "Clear indexed maps";
    }
  },

  async checkUpdate() {
    if (this.demoMode) return;
    try {
      const ui = await API.get("/api/settings/ui");
      document.getElementById("check-updates").checked = ui.check_updates !== false;
    } catch (e) { /* ignore */ }
    // the sidecar checks once at boot; poll briefly until it has an answer
    for (let i = 0; i < 5; i++) {
      let u;
      try { u = await API.get("/api/update"); } catch (e) { return; }
      if (u.checked) {
        if (u.available && u.latest) {
          document.getElementById("update-ver").textContent = u.latest;
          document.getElementById("update-banner").hidden = false;
        }
        return;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  },

  // in-app confirm dialog (native confirm() shows the server's IP in its title)
  confirm({ title, text, ok }) {
    const modal = document.getElementById("confirm-modal");
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-text").textContent = text;
    const okBtn = document.getElementById("confirm-ok");
    const cancelBtn = document.getElementById("confirm-cancel");
    okBtn.textContent = ok || "OK";
    modal.hidden = false;
    return new Promise((resolve) => {
      const done = (v) => {
        modal.hidden = true;
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBg);
        resolve(v);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      const onBg = (e) => { if (e.target === modal) done(false); };
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onBg);
    });
  },

  async maybeShowTutorial() {
    let hide = false;
    try { hide = !!localStorage.getItem("dq_tutorial_hide"); } catch (e) { /* ignore */ }
    if (!hide && !this.demoMode) {
      try { hide = !!(await API.get("/api/settings/ui")).hide_tutorial; } catch (e) { /* ignore */ }
    }
    document.getElementById("tutorial-dontshow").checked = hide;
    document.getElementById("show-tutorial-launch").checked = !hide;
    // a shared link lands on its moment; the tutorial waits for a plain visit
    // (the checkboxes still reflect the saved preference)
    const p = new URLSearchParams(location.hash.slice(1));
    const deepLink = p.has("d") || p.has("find");
    if (!hide && !deepLink) document.getElementById("tutorial-modal").hidden = false;
  },

  setTutorialHidden(hide) {
    document.getElementById("tutorial-dontshow").checked = hide;
    document.getElementById("show-tutorial-launch").checked = !hide;
    try {
      if (hide) localStorage.setItem("dq_tutorial_hide", "1");
      else localStorage.removeItem("dq_tutorial_hide");
    } catch (e) { /* ignore */ }
    if (!this.demoMode) {
      API.post("/api/settings/ui", { hide_tutorial: hide }).catch(() => {});
    }
  },

  async clearIndex() {
    const sure = await this.confirm({
      title: "Clear indexed maps?",
      text: "Every parsed demo, round and state is removed from the index.\n\n" +
        "Your demo files in the demos folder are NOT touched — you can re-index them " +
        "anytime with “Scan folder for new demos”.",
      ok: "Clear index",
    });
    if (!sure) return;
    const btn = document.getElementById("clear-index-btn");
    btn.disabled = true;
    try {
      await API.post("/api/index/clear");
      document.getElementById("results-list").innerHTML = "";
      document.getElementById("scan-progress").textContent =
        "Index cleared — “Scan folder for new demos” re-indexes them.";
      this.showDemosDirMsg("Index cleared.", false);
      await this.refreshMaps();
      await this.refreshDemoCount();
      await this.refreshIndexSize();
    } catch (e) {
      document.getElementById("scan-progress").textContent = `Couldn't clear the index: ${e.message}`;
    } finally { btn.disabled = false; }
  },

  async refreshMaps() {
    this.maps = await API.get("/api/maps");
    const sel = document.getElementById("map-select");
    const prev = sel.value;
    sel.innerHTML = "";
    for (const [label, active] of [["Active Duty", true], ["Other maps", false]]) {
      const group = document.createElement("optgroup");
      group.label = label;
      for (const m of this.maps.filter((x) => x.active_duty === active)) {
        group.appendChild(new Option(`${m.map_name}  (${m.n_demos} demos)`, m.map_name));
      }
      if (group.children.length) sel.appendChild(group);
    }
    const withData = this.maps.filter((m) => m.n_states > 0);
    const pick = prev && this.maps.some((m) => m.map_name === prev) ? prev
      : (withData[0] || this.maps[0] || {}).map_name;
    if (pick) {
      sel.value = pick;
      await this.selectMap(pick);
    }
  },

  async selectMap(name) {
    const seq = ++this.mapSeq;
    this.invalidateSearch();
    this.current = this.maps.find((m) => m.map_name === name) || null;
    if (!this.current) { this.mapLoading = false; return false; }
    this.mapLoading = true;
    document.getElementById("search-btn").disabled = true;
    try {
      document.getElementById("map-stats").textContent =
        `${this.current.n_states.toLocaleString()} states · ${this.current.k} nodes`;
      await Sketch.setMap(this.current);
      if (seq !== this.mapSeq) return false;
      this.setLevel("upper");
      this.updateMarkerSummary();
      document.getElementById("results-list").innerHTML = "";
      document.getElementById("results-title").textContent = "Results";
      document.getElementById("resolved").textContent = "";
      Stats.clear();
      this.renderEmptyState();
      await this.loadTeams(name);
      return seq === this.mapSeq;
    } catch (e) {
      if (seq !== this.mapSeq) return false;
      this.current = null;
      this.showError("This map could not load. Select another map or reload to retry.");
      return false;
    } finally {
      if (seq === this.mapSeq) {
        this.mapLoading = false;
        document.getElementById("search-btn").disabled = !this.current;
      }
    }
  },

  async loadTeams(mapName) {
    const seq = ++this.teamsSeq;
    const sel = document.getElementById("f-team");
    const prev = sel.value;
    sel.innerHTML = '<option value="">Any team</option>';
    try {
      const teams = await API.get(`/api/teams?map_name=${encodeURIComponent(mapName)}`);
      if (seq !== this.teamsSeq || this.current?.map_name !== mapName) return;
      for (const t of teams) sel.add(new Option(t, t));
      if (prev && teams.includes(prev)) sel.value = prev;   // keep the pick if this map has it
    } catch (e) {
      if (seq !== this.teamsSeq || this.current?.map_name !== mapName) return;
      document.getElementById("resolved").textContent = "Team names could not load. Select this map again to retry.";
    }
    this.updateTeamUI();
  },

  updateTeamUI() {
    // the side toggle only means something once a team is chosen
    const off = !document.getElementById("f-team").value;
    document.querySelectorAll("#f-team-side button").forEach((b) => { b.disabled = off; });
    document.querySelector("label[title^='Only include rounds']")?.classList.toggle("dim", off);
  },

  setSide(s) {
    Sketch.side = s;
    for (const k of ["ct", "t", "smoke", "molly"]) {
      document.getElementById(`side-${k}`).classList.toggle(`active-${k}`, s === k);
    }
  },

  setLevel(lv) {
    Sketch.level = lv;
    document.getElementById("level-upper").classList.toggle("active-lvl", lv === "upper");
    document.getElementById("level-lower").classList.toggle("active-lvl", lv === "lower");
    Sketch.render();
  },

  updateMarkerSummary() {
    const m = Sketch.markers;
    const fmt = (arr) => arr.length ? arr.map((x) => x.label).filter(Boolean).join(", ") : "—";
    let text = `CT: ${fmt(m.ct)}   |   T: ${fmt(m.t)}`;
    if (m.smoke.length) text += `   |   Smoke: ${fmt(m.smoke)}`;
    if (m.molly.length) text += `   |   Molly: ${fmt(m.molly)}`;
    document.getElementById("marker-summary").textContent = text;
  },

  populateClock() {
    const from = document.getElementById("f-clock-from");
    const to = document.getElementById("f-clock-to");
    from.innerHTML = ""; to.innerHTML = "";
    for (let s = this.roundClockS; s >= 0; s -= 5) {   // counts down, like the clock
      from.add(new Option(fmtClock(s), s));
      to.add(new Option(fmtClock(s), s));
    }
    from.value = this.roundClockS; to.value = 0;
  },

  updatePhaseUI() {
    // each phase owns its sub-control: the clock measures pre-plant, the site post-plant
    const phase = document.querySelector("#f-phase button.on").dataset.phase;
    const clockOff = phase === "post";
    const siteOff = phase === "pre";
    document.getElementById("f-clock-from").disabled = clockOff;
    document.getElementById("f-clock-to").disabled = clockOff;
    document.getElementById("f-clock-lbl").classList.toggle("dim", clockOff);
    document.getElementById("f-clock-hint").classList.toggle("dim", clockOff);
    document.querySelectorAll("#f-site button").forEach((b) => { b.disabled = siteOff; });
    document.getElementById("f-site-lbl").classList.toggle("dim", siteOff);
  },

  filters() {
    const buys = (side) =>
      [...document.querySelectorAll(`.buys[data-side=${side}] button.on`)].map((b) => b.dataset.buy);
    const phase = document.querySelector("#f-phase button.on").dataset.phase;
    const site = document.querySelector("#f-site button.on").dataset.site;
    // phase + site encode onto the bomb_sites wire format: "none" = pre-plant,
    // A/B = post-plant on that site, all three = no bomb filter
    const sites = [];
    if (phase !== "post") sites.push("none");
    if (phase !== "pre") {
      if (site !== "B") sites.push("A");
      if (site !== "A") sites.push("B");
    }
    const utilOn = (u) =>
      document.querySelector(`#f-util button[data-util=${u}].on`) ? true : null;
    const team = document.getElementById("f-team").value || null;
    return {
      bomb_sites: sites,
      smoke_active: utilOn("smoke"),
      molly_active: utilOn("molly"),
      ct_buy: buys("ct"),
      t_buy: buys("t"),
      alive_ct: [+document.getElementById("f-alive-ct-min").value, +document.getElementById("f-alive-ct-max").value],
      alive_t: [+document.getElementById("f-alive-t-min").value, +document.getElementById("f-alive-t-max").value],
      time_left: [+document.getElementById("f-clock-from").value, +document.getElementById("f-clock-to").value],
      team,
      team_side: team ? document.querySelector("#f-team-side button.on").dataset.tside : null,
    };
  },

  searchLimit: 50,

  invalidateSearch() {
    this.searchSeq++;
    this.lastQueryHash = "";
    const btn = document.getElementById("search-btn");
    btn.disabled = false;
    btn.textContent = "Search";
  },

  showError(message) {
    const list = document.getElementById("results-list");
    const text = document.createElement("div");
    text.className = "muted small";
    text.setAttribute("role", "status");
    text.textContent = message;
    list.replaceChildren(text);
  },

  // "Load more": re-run the same query for a bigger page, keeping the scroll spot
  async loadMore() {
    const btn = document.getElementById("load-more");
    if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
    this.searchLimit += 50;
    await this.search({ more: true });
  },

  async search(opts = {}) {
    if (!this.current || this.mapLoading) return;
    const seq = ++this.searchSeq;
    const map = this.current;
    this.lastQueryHash = "";
    if (!this.demoMode && this.indexedOk === 0) {
      this.renderEmptyState(true);      // searching an empty index: re-show the setup steps
      return;
    }
    if (!opts.more) this.searchLimit = 50;
    const panel = document.getElementById("results-panel");
    const keepScroll = opts.more ? panel.scrollTop : 0;
    const btn = document.getElementById("search-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner btn-spinner"></span>Searching…';
    if (!opts.more) {
      Stats.clear();
      document.getElementById("results-title").textContent = "Results";
      document.getElementById("results-list").innerHTML =
        '<div class="loading-block"><span class="spinner dark"></span>' +
        `Searching ${this.current.n_states.toLocaleString()} states…</div>`;
    }
    const filters = this.filters();
    try {
      const res = await API.post("/api/search", {
        map_name: this.current.map_name,
        ct_points: Sketch.markers.ct.map((m) => ({ x: m.x, y: m.y, level: m.level })),
        t_points: Sketch.markers.t.map((m) => ({ x: m.x, y: m.y, level: m.level })),
        smoke_points: Sketch.markers.smoke.map((m) => ({ x: m.x, y: m.y, level: m.level })),
        molly_points: Sketch.markers.molly.map((m) => ({ x: m.x, y: m.y, level: m.level })),
        max_distance: +document.getElementById("dist-slider").value,
        filters,
        limit: this.searchLimit,
      });
      if (seq !== this.searchSeq) return;
      Results.render(res, map, filters);
      if (keepScroll) panel.scrollTop = keepScroll;
      const fmtSide = (arr) => arr.map((p) => p.label || "?").join(", ") || "—";
      let resolved = `query → CT: ${fmtSide(res.resolved.ct)} | T: ${fmtSide(res.resolved.t)}`;
      if ((res.resolved.smoke || []).length) resolved += ` | Smoke: ${fmtSide(res.resolved.smoke)}`;
      if ((res.resolved.molly || []).length) resolved += ` | Molly: ${fmtSide(res.resolved.molly)}`;
      document.getElementById("resolved").textContent = resolved;
    } catch (err) {
      if (seq !== this.searchSeq) return;
      Stats.clear();
      this.showError(`Search failed: ${err.message} Try searching again.`);
    } finally {
      if (seq === this.searchSeq) {
        btn.disabled = false;
        btn.textContent = "Search";
      }
    }
  },

  async refreshDemosDir() {
    try { this.renderDemosDir(await API.get("/api/settings/demos-dir")); }
    catch (e) { /* ignore */ }
  },

  renderDemosDir(state) {
    const el = document.getElementById("demos-dir-path");
    el.textContent = state.demos_dir + (state.is_default ? "  (default)" : "");
    el.title = state.demos_dir;
  },

  showDemosDirMsg(text, isErr) {
    const el = document.getElementById("demos-dir-msg");
    el.textContent = text;
    el.classList.toggle("err", !!isErr);
  },

  // folder just changed: update the display, nudge a rescan, and refresh counts
  async onDemosDirChanged(state) {
    this.renderDemosDir(state);
    this.showDemosDirMsg('Folder set — put your .dem files here, then click "Scan folder for new demos".', false);
    await this.refreshDemoCount();
  },

  async refreshDemoCount() {
    try {
      const demos = await API.get("/api/demos");
      const ok = demos.filter((d) => d.status === "ok").length;
      const err = demos.length - ok;
      document.getElementById("demo-count").textContent =
        `${ok} demos indexed${err ? ` · ${err} failed` : ""}`;
      this.indexedOk = ok;
      this.renderEmptyState();
      this.renderReadyState();
    } catch (e) { /* ignore */ }
  },

  // demos are indexed but nothing searched yet: invite the first action instead
  // of leaving the results panel an empty void (cleared by the first render)
  renderReadyState() {
    const list = document.getElementById("results-list");
    if ((!this.indexedOk && !this.demoMode) || list.childElementCount) return;
    list.innerHTML = `
      <div id="ready-state">Click the radar to place <b>CT</b> or <b>T</b> markers, then hit
        <b>Search</b> to find every pro round matching your sketch &mdash; or search with
        filters alone (no markers) for situations like &ldquo;every 3v2 post-plant&rdquo;.<br>
        Every result opens that round as a 2D replay.</div>`;
  },

  // onboarding instructions in the results panel while nothing is indexed
  renderEmptyState(force) {
    const list = document.getElementById("results-list");
    const existing = document.getElementById("empty-state");
    if (this.indexedOk !== 0 || this.demoMode) {
      if (existing) existing.remove();
      return;
    }
    if (!force && (existing || list.childElementCount)) return;
    list.innerHTML = `
      <div id="empty-state">
        <b>No demos indexed yet</b>
        <ol>
          <li>Point the app at the folder that contains your CS2 demo files (<b>.dem</b>)
            &mdash; e.g. GOTV / pro-match demos. Archives must be extracted so the .dem files
            sit in the folder (subfolders are fine).<br>
            <button id="empty-open-settings" class="linkbtn">Choose demos folder&hellip;</button></li>
          <li>Click <b>Scan folder for new demos</b> in the sidebar and wait for parsing to finish.</li>
          <li>Pick a map, sketch a scenario and hit <b>Search</b>.</li>
        </ol>
      </div>`;
    document.getElementById("empty-open-settings")
      .addEventListener("click", () => this.openSettings());
    if (force) {
      const box = document.getElementById("empty-state");
      box.classList.add("pulse");
      box.addEventListener("animationend", () => box.classList.remove("pulse"), { once: true });
    }
  },

  async startScan() {
    try { await API.post("/api/ingest/scan"); } catch (e) { /* 409 = already running */ }
    this.pollScan();
  },

  pollScanOnce() {
    API.get("/api/ingest/status").then((s) => {
      if (s.running) this.pollScan();
    }).catch(() => {});
  },

  pollScan() {
    if (this.scanTimer) return;
    const el = document.getElementById("scan-progress");
    this.scanTimer = setInterval(async () => {
      try {
        const s = await API.get("/api/ingest/status");
        if (s.running) {
          const detail = s.phase.startsWith("parsing")
            ? `Parsing demo ${s.files_done}/${s.files_total}: ${s.current_file}`
            : `${s.phase}${s.current_file ? `: ${s.current_file}` : "…"}`;
          el.textContent = detail;
          this.showProcessing(detail);
        } else {
          clearInterval(this.scanTimer);
          this.scanTimer = null;
          this.hideProcessing();
          if (!s.finished_at) {
            el.textContent = "";
          } else if (!s.files_total && !s.files_skipped) {
            // scanned an empty folder: guide the user to folder setup
            el.textContent = "No .dem files found in the demos folder — open Settings (⚙) " +
              "and choose the folder that contains your demos.";
          } else {
            el.textContent =
              `done — ${s.files_total} new demo${s.files_total === 1 ? "" : "s"}, ` +
              `${s.files_skipped} already indexed` +
              (s.errors.length ? ` · ${s.errors.length} errors` : "");
          }
          await this.refreshMaps();
          await this.refreshDemoCount();
        }
      } catch (e) { /* server briefly busy */ }
    }, 1000);
  },

  // prominent banner while a scan runs, so the app isn't closed mid-parse
  // (closing kills the engine and aborts the scan)
  showProcessing(detail) {
    const b = document.getElementById("processing-banner");
    if (!b) return;
    document.getElementById("processing-detail").textContent = detail;
    b.hidden = false;
  },

  hideProcessing() {
    const b = document.getElementById("processing-banner");
    if (b) b.hidden = true;
  },
};

window.addEventListener("message", event => {
  if (event.source === window.parent && event.data?.dqPing && App.ready) {
    window.parent.postMessage({ dqReady: true }, event.origin);
  }
});
window.addEventListener("DOMContentLoaded", () => App.init().catch(() => {
  document.getElementById("tutorial-modal").hidden = true;
  App.showError("DemoQuery could not load. Reload to reconnect.");
  const retry = document.createElement("button");
  retry.className = "btn";
  retry.textContent = "Reload DemoQuery";
  retry.addEventListener("click", () => location.reload());
  document.getElementById("results-list").appendChild(retry);
  if (window.parent !== window) window.parent.postMessage({ dqError: true }, "*");
}));
