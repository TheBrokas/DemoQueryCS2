/* Dependency-free regression checks. Supply app/sketch/api/wrapper source strings
 * to runFrontendChecks(sources), e.g. from a Node vm or an in-browser JS runner.
 * All DOM, requests and image loads are isolated in memory. */
async function runFrontendChecks(sources) {
  const hosted = sources.hosted !== false;
  let passed = 0;
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const deferred = () => {
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return { promise, resolve, reject };
  };
  const element = () => ({ value: "", textContent: "", innerHTML: "", disabled: false,
    hidden: false, style: {}, dataset: {}, children: [], scrollTop: 0,
    classList: { toggle() {} }, events: {}, setAttribute() {},
    addEventListener(name, fn) { this.events[name] = fn; },
    appendChild(node) { this.children.push(node); }, add(node) { this.children.push(node); },
    replaceChildren(...nodes) { this.children = nodes; } });
  function harness(extra = {}) {
    const els = {};
    const document = { getElementById: id => els[id] ||= element(), createElement: element,
      querySelectorAll: () => [], querySelector: () => null };
    const rendered = [];
    const globals = { window: { addEventListener() {} }, document,
      Sketch: { markers: { ct: [], t: [], smoke: [], molly: [] } }, Stats: { clear() {} },
      Results: { render: (res, map) => rendered.push([res, map]) }, API: {},
      Option: function(text, value) { this.text = text; this.value = value; }, ...extra };
    const app = new Function(...Object.keys(globals), sources.app + ";return App;")(...Object.values(globals));
    app.demoMode = true; app.current = { map_name: "de_nuke", n_states: 1 };
    app.filters = () => ({}); app.queryState = () => ({});
    app.hashForQuery = () => "#q=test"; app.announceHash = () => {};
    return { app, document, els, rendered, globals };
  }
  const result = () => ({ resolved: { ct: [], t: [] } });
  if (hosted) {
    const handlers = {}; const h = harness({ window: { addEventListener: (name, fn) => { handlers[name] = fn; } } });
    let restored = 0; h.app.handleDeepLink = async () => { restored++; };
    handlers.hashchange(); assert(restored === 0, "uninitialized app handled a fragment");
    h.app.ready = true; handlers.hashchange();
    assert(restored === 1, "same-tab shared link was ignored"); passed++;
  }
  {
    const h = harness({ Sketch: { setMap: async () => { throw new Error("map failed"); } } });
    h.app.maps = [{ map_name: "de_nuke", n_states: 1 }];
    assert(await h.app.selectMap("de_nuke") === false, "failed map load reported success");
    assert(h.app.mapLoading === false && h.app.current === null, "failed map left loading locked"); passed++;
  }
  {
    const request = deferred(); const h = harness({ API: { post: () => request.promise } });
    const pending = h.app.search(); h.app.invalidateSearch(); h.app.current = { map_name: "de_mirage" };
    request.resolve(result()); await pending;
    assert(h.rendered.length === 0, "old map search rendered");
    if (hosted) assert(h.els["query-share"].disabled, "old share link remained enabled"); passed++;
  }
  {
    const old = deferred(), newer = deferred(); let calls = 0;
    const h = harness({ API: { post: () => (++calls === 1 ? old : newer).promise } });
    const a = h.app.search(), b = h.app.search();
    old.reject(new Error("old failure")); await a;
    assert(h.els["search-btn"].disabled, "old failure unlocked pending new search");
    newer.resolve(result()); await b;
    assert(h.rendered.length === 1, "new search did not render once"); passed++;
  }
  {
    const a = deferred(), b = deferred(); let calls = 0;
    const h = harness({ API: { get: () => (++calls === 1 ? a : b).promise } });
    h.app.updateTeamUI = () => {};
    const first = h.app.loadTeams("de_nuke"); h.app.current = { map_name: "de_mirage" };
    const second = h.app.loadTeams("de_mirage"); b.resolve(["Mirage team"]); await second;
    a.resolve(["Nuke team"]); await first;
    assert(h.els["f-team"].children.length === 1 && h.els["f-team"].children[0].value === "Mirage team",
      "old map team response contaminated selector"); passed++;
  }
  {
    const h = harness(); h.app.updatePhaseUI = h.app.updateTeamUI = () => {};
    h.document.querySelector = () => element();
    if (hosted) {
      h.app.setFilters({ team: "Friday stack", team_side: "ct", bomb_sites: ["none", "A", "B"] });
      assert(h.els["f-team"].value === "Friday stack", "shared query lost team filter");
    }
    h.app.showError("<img src=x onerror=alert(1)>");
    assert(h.els["results-list"].children[0].textContent.startsWith("<img"), "error must use plain text"); passed++;
  }
  {
    const pending = deferred();
    const sketch = new Function("API", "document", sources.sketch + ";return Sketch;")(
      { get: () => pending.promise }, { getElementById: element });
    sketch.map = { map_name: "de_nuke" }; const load = sketch.loadNodes();
    sketch.map = { map_name: "de_mirage" }; pending.resolve({ nuke: true }); await load;
    assert(sketch.nodesData === null, "old map node overlay applied"); passed++;
  }
  {
    const images = []; const fakeImage = function() { images.push(this); };
    const load = new Function("Image", sources.api + ";return loadRadar;")(fakeImage);
    const first = load("de_nuke", "upper"); images[0].onerror();
    assert(await first === null, "failed radar did not settle");
    const second = load("de_nuke", "upper");
    assert(images.length === 2, "failed radar was permanently cached");
    images[1].onload(); assert(await second === images[1], "radar retry failed"); passed++;
  }
  {
    const images = [deferred(), deferred()]; let calls = 0; const draws = [];
    const sketch = new Function("loadRadar", "Image", "UTIL", sources.sketch + ";return Sketch;")(
      () => images[calls++].promise, function(){}, { SMOKE_RADIUS_U: 144, MOLLY_RADIUS_U: 100 });
    sketch.ctx = { clearRect() {}, drawImage: img => draws.push(img) };
    sketch.map = { map_name: "de_nuke", calibration: { scale: 1 } };
    const first = sketch.render(); sketch.map = { map_name: "de_mirage", calibration: { scale: 1 } };
    const second = sketch.render(); images[1].resolve("mirage"); await second;
    images[0].resolve("nuke"); await first;
    assert(draws.join() === "mirage", "late radar repainted the wrong map"); passed++;
  }
  if (hosted) {
    const els = {}, handlers = {}, timers = [];
    const frame = { postMessage() {} };
    const document = { getElementById: id => els[id] ||= element() };
    document.getElementById("tool").contentWindow = frame;
    const location = { href: "https://cs2analysis.com/demoquery/", search: "", hash: "", pathname: "/demoquery/" };
    const window = { addEventListener: (name, fn) => { handlers[name] = fn; } };
    new Function("document", "window", "location", "history", "setTimeout", "clearTimeout", "URL", "URLSearchParams", sources.wrapper)(
      document, window, location, { replaceState() {} }, fn => { timers.push(fn); return timers.length; }, () => {}, URL, URLSearchParams);
    timers[0](); assert(els["service-retry"].hidden === false, "timeout did not offer retry");
    const firstSrc = els.tool.src; els["service-retry"].events.click();
    assert(els.tool.src !== firstSrc && els["service-retry"].hidden, "retry did not reload the frame");
    handlers.message({ source: frame, origin: "https://untrusted.example", data: { dqReady: true } });
    assert(els.tool.style.display === "none", "untrusted frame could dismiss fallback");
    handlers.message({ source: frame, origin: "https://web-production-d75ca.up.railway.app", data: { dqReady: true } });
    assert(els.tool.style.display === "block" && els.fallback.style.display === "none", "ready handshake did not reveal app"); passed++;
  }
  return `${passed} frontend regression checks passed`;
}
if (typeof module !== "undefined") {
  module.exports = { runFrontendChecks };
  if (require.main === module) {
    const fs = require("node:fs"), path = require("node:path");
    const hosted = !process.argv.includes("--desktop");
    const sources = { hosted };
    for (const name of ["app", "sketch", "api"]) sources[name] = fs.readFileSync(
      path.resolve(__dirname, "../src/demoquerycs2/web/static", `${name}.js`), "utf8");
    if (hosted) sources.wrapper = [...fs.readFileSync(
      path.resolve(__dirname, "../../../demoquery/index.html"), "utf8")
      .matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
    runFrontendChecks(sources).then(console.log).catch(error => { console.error(error); process.exitCode = 1; });
  }
}
