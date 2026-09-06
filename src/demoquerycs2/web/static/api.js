"use strict";
/* Shared utility (grenade) rendering constants. */
const UTIL = {
  SMOKE_RADIUS_U: 144,     // game units (~290u across, the real CS2 smoke footprint)
  MOLLY_RADIUS_U: 100,
  FLASH_POP_S: 0.7,        // flash/he pop ring lifetime, seconds
  PATH_FADE_S: 1.5,        // travel path linger after detonation
  COLORS: { smoke: "rgba(200,205,215,0.55)", molly: "rgba(255,120,30,0.45)" },
  icon(type, side) {       // circle-center icon per grenade type
    if (type === "molly") return side === "CT" ? "incgrenade" : "molotov";
    return { smoke: "smokegrenade", flash: "flashbang", he: "hegrenade" }[type];
  },
};

const API = {
  async request(path, options = {}) {
    const controller = new AbortController();
    // A timed-out mutation may already have committed; allow Settings saves
    // and scans to finish while bounding reads and searches.
    const timeout = !options.method ? 30000 : path === "/api/search" ? 90000 : 0;
    const timer = timeout ? setTimeout(() => controller.abort(), timeout) : null;
    try {
      let r;
      try { r = await fetch(path, { ...options, signal: controller.signal }); }
      catch (e) {
        throw new Error(e.name === "AbortError" ? "The request took too long."
          : "Could not connect to DemoQuery.");
      }
      if (!r.ok) {
        let msg = r.status === 429 ? "Too many requests. Wait a moment and try again."
          : r.status === 422 ? "Check the entered values and try again."
          : "DemoQuery could not complete the request.";
        try { const data = await r.json(); if (typeof data.detail === "string") msg = data.detail; }
        catch (e) { /* proxy error pages are not user-facing messages */ }
        throw new Error(msg);
      }
      return await r.json();
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  },
  get(path) { return this.request(path); },
  post(path, body) {
    return this.request(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
  },
};

/* mm:ss for round-clock seconds; negatives clamp to 0:00 (timeout slop) */
function fmtClock(seconds) {
  const s = Math.max(0, seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* Tournaments configure different clan-name spellings for the same org; the
   alias table folds them into one display name so match cards, logos and team
   search treat them as a single team. Verified against filename slugs —
   notably "LG" is Luminosity (NOT Legacy) and HOTU is not The Huns. */
const TEAM_ALIASES = {
  "3D": "3DMAX",
  "9z Globant": "9z",
  "B8 Esports": "B8",
  "BB Team": "BetBoom Team",
  "BCG": "BC Game Esports",
  "FLC": "Team Falcons",
  "FUT": "FUT Esports",
  "FaZe": "FaZe Clan",
  "G2": "G2 Esports",
  "LEGACY": "Legacy",
  "LGCY": "Legacy",
  "LVG": "Lynn Vision Gaming",
  "MNGLZ": "The MongolZ",
  "MongolZ": "The MongolZ",
  "NaVi": "Natus Vincere",
  "PV": "PARIVISION",
  "Spirit": "Team Spirit",
  "Liquid": "Team Liquid",
  "Vitality": "Team Vitality",
  "paiN": "paiN Gaming",
};

function canonicalTeam(name) {
  return TEAM_ALIASES[name] || name;
}

const TEAM_DISPLAY_NAMES = {
  "Lynn Vision Gaming": "Lynn Vision",
  "BetBoom Team": "BetBoom",
  "BC Game Esports": "BC Game",
};

function displayTeam(name) {
  const canonical = canonicalTeam(name);
  return TEAM_DISPLAY_NAMES[canonical] || canonical;
}

const radarCache = new Map();
function loadRadar(mapName, level) {
  const key = `${mapName}:${level}`;
  if (!radarCache.has(key)) {
    const img = new Image();
    radarCache.set(key, new Promise((res) => {
      img.onload = () => res(img);
      img.onerror = () => { radarCache.delete(key); res(null); };
    }));
    img.src = `/api/maps/${mapName}/radar?level=${level}`;
  }
  return radarCache.get(key);
}
