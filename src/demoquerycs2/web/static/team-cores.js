"use strict";

/* Offline Settings: user names, stable Steam IDs, and round-side core matching. */
const TeamCores = {
  players: [], cores: [], selected: new Set(), editing: null, busy: false, loadSeq: 0,

  init() {
    document.getElementById("core-new").addEventListener("click", () => this.edit(null));
    document.getElementById("core-cancel").addEventListener("click", () => this.closeEditor());
    document.getElementById("core-search").addEventListener("input", () => this.renderPlayers());
    document.getElementById("core-form").addEventListener("submit", (e) => {
      e.preventDefault(); this.save();
    });
  },

  message(text, error = false) {
    const el = document.getElementById("core-message");
    el.textContent = text; el.classList.toggle("core-error", error);
  },

  async load(reportFailure = false) {
    if (App.demoMode || this.busy) return;
    const seq = ++this.loadSeq;
    this.message("Loading players and team cores…");
    document.getElementById("core-new").disabled = true;
    try {
      const data = await API.get("/api/settings/team-cores");
      if (seq !== this.loadSeq) return;
      this.players = data.players; this.cores = data.cores;
      this.renderCores();
      if (!document.getElementById("core-form").hidden) this.renderPlayers();
      document.getElementById("core-new").disabled = this.players.length < 3;
      this.message(this.players.length < 3 ? "Scan demos with at least three players to create a team core." : "");
    } catch (e) {
      if (seq === this.loadSeq) this.message("Could not load team cores. Close and reopen Settings to retry.", true);
      if (reportFailure) throw new Error("Your change was saved, but Settings could not refresh. Close and reopen Settings to retry.");
    }
  },

  player(sid) { return this.players.find(p => p.steamid === sid) || { name: "Not currently indexed", steamid: sid }; },

  button(label, action) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = label; b.disabled = this.busy;
    b.addEventListener("click", action); return b;
  },

  renderCores() {
    const list = document.getElementById("core-list"); list.replaceChildren();
    for (const core of this.cores) {
      const card = document.createElement("div"); card.className = "core-card";
      const head = document.createElement("div"); head.className = "core-card-head";
      const name = document.createElement("strong"); name.textContent = core.name;
      const actions = document.createElement("div"); actions.className = "btnrow";
      actions.append(this.button("Edit", () => this.edit(core)), this.button("Delete", () => this.remove(core)));
      head.append(name, actions);
      const members = document.createElement("div"); members.className = "small muted";
      members.textContent = core.steamids.map(sid => this.player(sid).name).join(" · ");
      const status = document.createElement("div"); status.className = "small muted";
      status.textContent = `${core.rounds.toLocaleString()} matching rounds` +
        (core.conflicts ? ` · ${core.conflicts.toLocaleString()} rounds overlap another core and keep their recorded name` : "");
      card.append(head, members, status); list.append(card);
    }
    if (!this.cores.length) {
      const empty = document.createElement("p"); empty.className = "small muted";
      empty.textContent = "No team cores yet. Name a regular lineup even when your demos have no team names.";
      list.append(empty);
    }
  },

  edit(core) {
    if (this.busy) return;
    this.editing = core?.core_id ?? null;
    this.selected = new Set(core?.steamids || []);
    document.getElementById("core-form").hidden = false;
    document.getElementById("core-name").value = core?.name || "";
    document.getElementById("core-search").value = "";
    document.getElementById("core-save").textContent = core ? "Save changes" : "Save team core";
    this.message(""); this.renderPlayers(); document.getElementById("core-name").focus();
  },

  closeEditor() {
    if (this.busy) return;
    document.getElementById("core-form").hidden = true;
    this.editing = null; this.selected.clear();
  },

  renderPlayers() {
    const selected = document.getElementById("core-selected"); selected.replaceChildren();
    for (const sid of this.selected) {
      const p = this.player(sid);
      const b = this.button(`${p.name} ×`, () => { this.selected.delete(sid); this.renderPlayers(); });
      b.title = `Remove ${p.name} (${sid})`; b.setAttribute("aria-label", b.title);
      selected.append(b);
    }
    const count = this.selected.size;
    document.getElementById("core-count").textContent = `${count} of 3–5 players selected. Every selected player must be on the same side.`;
    document.getElementById("core-save").disabled = this.busy || count < 3 || count > 5;
    const query = document.getElementById("core-search").value.trim().toLocaleLowerCase();
    const matches = this.players.filter(p => p.name.toLocaleLowerCase().includes(query) || p.steamid.includes(query));
    const list = document.getElementById("core-players"); list.replaceChildren();
    for (const p of matches.slice(0, 60)) {
      const row = document.createElement("label"); row.className = "core-player";
      const input = document.createElement("input"); input.type = "checkbox";
      input.checked = this.selected.has(p.steamid);
      input.disabled = this.busy || (!input.checked && count >= 5);
      input.addEventListener("change", () => {
        if (input.checked) this.selected.add(p.steamid); else this.selected.delete(p.steamid);
        this.renderPlayers();
        // Replacing the list must not strand keyboard focus after a selection.
        const next = [...list.querySelectorAll("input")].find(el => el.value === p.steamid);
        if (next) next.focus();
      });
      input.value = p.steamid;
      const text = document.createElement("span");
      const name = document.createElement("span"); name.textContent = p.name;
      const id = document.createElement("small"); id.textContent = `${p.steamid} · ${p.demos} demos`;
      text.append(name, id); row.append(input, text); list.append(row);
    }
    document.getElementById("core-results-note").textContent = matches.length > 60
      ? `Showing 60 of ${matches.length} players. Type a name or Steam ID to narrow the list.`
      : matches.length ? "Names come from each player’s latest indexed recording. Steam IDs keep renamed players linked."
      : "No players match this search.";
  },

  setBusy(value) {
    this.busy = value;
    document.querySelectorAll("#team-cores-section input, #team-cores-section button").forEach(el => { el.disabled = value; });
    if (!value) {
      document.getElementById("core-new").disabled = this.players.length < 3;
      this.renderPlayers();
    }
  },

  async changed() {
    App.invalidateSearch();
    if (App.current) await App.loadTeams(App.current.map_name);
    Stats.clear(); App.lastQueryHash = "";
    document.getElementById("results-title").textContent = "Results";
    document.getElementById("results-list").textContent = "Team cores changed. Search again to refresh your results.";
    await this.load(true);
  },

  async save() {
    if (this.busy) return;
    this.setBusy(true); this.message("Saving and matching existing rounds… The first save may take a moment.");
    try {
      await API.post("/api/settings/team-cores", {
        core_id: this.editing, name: document.getElementById("core-name").value,
        steamids: [...this.selected],
      });
      this.setBusy(false); this.closeEditor(); await this.changed();
      this.message("Team core saved. It applies to existing demos and future scans.");
    } catch (e) { this.setBusy(false); this.message(e.message || "Could not save this team core.", true); }
  },

  async remove(core) {
    if (this.busy) return;
    if (!await App.confirm({ title: `Delete ${core.name}?`, text: "Recorded team names will be used again. Your demos stay indexed.", ok: "Delete team core" })) return;
    this.setBusy(true);
    try {
      await API.post("/api/settings/team-cores/delete", { core_id: core.core_id });
      this.setBusy(false); this.closeEditor(); await this.changed(); this.message("Team core deleted.");
    } catch (e) { this.setBusy(false); this.message(e.message || "Could not delete this team core.", true); }
  },
};
