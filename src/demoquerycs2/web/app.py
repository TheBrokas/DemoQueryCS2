"""FastAPI backend: map assets, ingest control, search, playback."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import numpy as np
from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, config, db as dbmod, mapdata, updatecheck
from ..ingest import scanner
from ..ingest.tokenizer import unpack_path, unpack_positions
from ..search import engine

app = FastAPI(title="DemoQueryCS2", version=__version__)
scanner.on_maps_changed(lambda maps: engine.invalidate(maps))
updatecheck.start()


def _start_malloc_trim(interval_s: float = 300.0) -> None:
    """Periodically hand glibc's freed-but-retained arena pages back to the OS.

    Search allocates large short-lived numpy arrays; glibc keeps the arenas, so
    RSS ratchets up until restart (the Railway sawtooth). No-op off glibc
    (Windows/macOS desktop builds)."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim
    except (OSError, AttributeError):
        return

    def loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                libc.malloc_trim(0)
            except Exception:  # noqa: BLE001 - trim is best-effort
                return

    threading.Thread(target=loop, daemon=True, name="malloc-trim").start()


def _prewarm_indexes() -> None:
    """Hosted demo: build all map indexes right after boot instead of on each
    map's first search, so no visitor eats a multi-second cold build."""
    def loop() -> None:
        for m in sorted(config.ACTIVE_DUTY_MAPS):
            try:
                if mapdata.get_nodes(m) is not None:
                    engine.get_index(m)
            except Exception:  # noqa: BLE001 - prewarm must never kill the server
                pass

    threading.Thread(target=loop, daemon=True, name="index-prewarm").start()


_start_malloc_trim()
if config.DEMO_MODE:
    _prewarm_indexes()


@app.on_event("startup")
async def _cap_worker_threads() -> None:
    # sync endpoints run on the AnyIO threadpool (default 40 threads); a handful
    # is plenty here and every extra thread is another glibc arena of RSS creep
    to_thread.current_default_thread_limiter().total_tokens = 8


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Make browsers revalidate static files on every load (cheap 304s via ETag).

    Without this, updates to the hosted demo don't reach browsers that cached the
    app shell - refreshing the embedding site never refreshes iframe subresources."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

_rate_buckets: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _check_rate(request: Request, key: str, limit: int, window_s: float = 60.0) -> None:
    """Per-IP sliding-window limit; only enforced on the public demo instance."""
    if not config.DEMO_MODE:
        return
    if len(_rate_buckets) > 512:                     # sweep dead IPs so the dict can't grow forever
        now = time.time()
        for k in [k for k, b in _rate_buckets.items() if not b or now - b[-1] > window_s]:
            del _rate_buckets[k]
    bucket = _rate_buckets[f"{key}:{_client_ip(request)}"]
    now = time.time()
    while bucket and now - bucket[0] > window_s:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "Rate limit exceeded - please slow down.")
    bucket.append(now)


def _demo_forbidden() -> None:
    if config.DEMO_MODE:
        raise HTTPException(403, "Not available in the web demo - install DemoQueryCS2 to use your own demos.")


class SearchPoint(BaseModel):
    x: float
    y: float
    level: str | None = None


class SearchFilters(BaseModel):
    bomb_planted: bool | None = None
    bomb_site: str | None = None
    bomb_sites: list[str] | None = None       # chip UI: subset of {"A", "B", "none"}
    ct_buy: list[str] = Field(default_factory=list)
    t_buy: list[str] = Field(default_factory=list)
    alive_ct: list[int] | None = None
    alive_t: list[int] | None = None
    time_range: list[int] | None = None       # legacy: elapsed seconds, all states
    time_left: list[int] | None = None        # pre-plant round clock, seconds remaining
    smoke_active: bool | None = None
    molly_active: bool | None = None
    team: str | None = None                   # clan name; restrict to rounds this team plays
    team_side: str | None = None              # "ct" | "t" | None/"any" — which side the team is on


class SearchRequest(BaseModel):
    map_name: str
    ct_points: list[SearchPoint] = Field(default_factory=list)
    t_points: list[SearchPoint] = Field(default_factory=list)
    smoke_points: list[SearchPoint] = Field(default_factory=list)
    molly_points: list[SearchPoint] = Field(default_factory=list)
    max_distance: float = 300.0
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = 50


def _node_labels(map_name: str) -> dict[int, str]:
    conn = dbmod.connect()
    try:
        rows = conn.execute(
            "SELECT node_idx, place_name, votes FROM node_labels WHERE map_name=? ORDER BY votes", (map_name,))
        return {row["node_idx"]: row["place_name"] for row in rows}   # last write = max votes
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "db": str(config.DB_PATH),
            "demos_dir": str(config.DEMOS_DIR),
            "demo_mode": config.DEMO_MODE, "download_url": config.DOWNLOAD_URL,
            "share_base": config.SHARE_BASE,
            "round_clock_s": config.ROUND_CLOCK_S}


@app.post("/api/open-demos-folder")
def open_demos_folder() -> dict:
    _demo_forbidden()
    config.ensure_dirs()
    try:
        os.startfile(config.DEMOS_DIR)  # noqa: S606 - local desktop app
    except OSError as e:
        raise HTTPException(500, f"could not open folder: {e}") from e
    return {"opened": str(config.DEMOS_DIR)}


class DemosDir(BaseModel):
    path: str | None = None


def _demos_dir_state() -> dict:
    return {"demos_dir": str(config.DEMOS_DIR), "is_default": config.is_default_demos_dir()}


@app.get("/api/settings/demos-dir")
def get_demos_dir() -> dict:
    return _demos_dir_state()


@app.post("/api/settings/demos-dir")
def set_demos_dir(body: DemosDir) -> dict:
    """Point the app at a folder typed/pasted into the path field (empty = reset)."""
    _demo_forbidden()
    try:
        config.set_demos_dir(body.path)
    except (OSError, ValueError) as e:   # ValueError: malformed path (e.g. illegal chars)
        raise HTTPException(400, f"can't use that folder: {e}") from e
    return _demos_dir_state()


@app.post("/api/pick-demos-folder")
def pick_demos_folder() -> dict:
    """Open the native folder picker; on selection, persist and switch to it."""
    _demo_forbidden()
    from ..folderpick import pick_folder
    try:
        chosen = pick_folder()
    except OSError as e:
        raise HTTPException(500, f"folder picker failed: {e}") from e
    if not chosen:
        return {"cancelled": True, **_demos_dir_state()}
    try:
        config.set_demos_dir(chosen)
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"can't use that folder: {e}") from e
    return {"cancelled": False, **_demos_dir_state()}


class UISettings(BaseModel):
    hide_tutorial: bool | None = None
    check_updates: bool | None = None


@app.get("/api/settings/ui")
def get_ui_settings() -> dict:
    if config.DEMO_MODE:
        return {}
    return config.get_ui_settings()


@app.post("/api/settings/ui")
def set_ui_settings(body: UISettings) -> dict:
    _demo_forbidden()
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    return config.update_ui_settings(values)


@app.get("/api/update")
def update_status() -> dict:
    """Cached result of the launch-time release check (never blocks)."""
    return updatecheck.status()


@app.post("/api/index/clear")
def clear_index() -> dict:
    """Drop all indexed demo data (rounds/states/kills). Never touches .dem files."""
    _demo_forbidden()
    if scanner.STATUS.running:
        raise HTTPException(409, "A scan is running - wait for it to finish first.")
    conn = dbmod.connect()
    try:
        with conn:
            conn.execute("DELETE FROM demos")        # cascades rounds/states/kills/players
            conn.execute("DELETE FROM archives")
            conn.execute("DELETE FROM node_labels")
        # actually give the space back: without this the file keeps its size
        # (freelist pages) and the settings size readout would look untouched
        conn.execute("VACUUM")
    finally:
        conn.close()
    engine.invalidate(None)
    return {"cleared": True}


@app.get("/api/index/size")
def index_size() -> dict:
    """On-disk size of the index database — what "Clear indexed maps" frees."""
    _demo_forbidden()
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += Path(f"{config.DB_PATH}{suffix}").stat().st_size
        except OSError:
            pass
    return {"total_bytes": total}


_maps_cache: list[dict] | None = None    # demo mode only: the DB never changes after boot


@app.get("/api/maps")
def maps() -> list[dict]:
    global _maps_cache
    if config.DEMO_MODE and _maps_cache is not None:
        return _maps_cache
    conn = dbmod.connect()
    try:
        stats = {row["map_name"]: dict(row) for row in conn.execute(
            "SELECT map_name, COUNT(DISTINCT demo_id) n_demos, COUNT(*) n_states "
            "FROM states GROUP BY map_name")}
    finally:
        conn.close()
    cals = mapdata.calibrations()
    out = []
    for m in mapdata.available_maps():
        if m not in config.SEARCHABLE_MAPS:
            continue
        nodes = mapdata.get_nodes(m)
        cal = cals.get(m, {})
        lm = cal.get("lower_level_max_units")
        out.append({
            "map_name": m,
            "active_duty": m in config.ACTIVE_DUTY_MAPS,
            "n_demos": stats.get(m, {}).get("n_demos", 0),
            "n_states": stats.get(m, {}).get("n_states", 0),
            "has_radar": mapdata.radar_path(m) is not None,
            "has_lower": mapdata.radar_path(m, "lower") is not None,
            "calibration": {
                "pos_x": cal.get("pos_x"), "pos_y": cal.get("pos_y"), "scale": cal.get("scale"),
                "lower_level_max_units": lm if lm is not None and lm > -999999 else None,
            },
            "k": nodes.k if nodes else 0,
        })
    if config.DEMO_MODE:
        _maps_cache = out
    return out


@app.get("/api/teams")
def teams(map_name: str) -> list[str]:
    """Distinct team (clan) names indexed for a map — populates the team filter."""
    conn = dbmod.connect()
    try:
        names: set[str] = set()
        for col in ("team1", "team2"):        # col is a fixed literal, never user input
            for row in conn.execute(
                    f"SELECT DISTINCT {col} FROM demos "
                    f"WHERE map_name = ? AND status = 'ok' AND {col} IS NOT NULL AND {col} <> ''",
                    (map_name,)):
                names.add(row[col])
    finally:
        conn.close()
    return sorted(names, key=str.casefold)


@app.get("/api/maps/{map_name}/radar")
def radar(map_name: str, level: str = "upper") -> FileResponse:
    p = mapdata.radar_path(map_name, level)
    if p is None:
        raise HTTPException(404, "no radar for this map/level")
    return FileResponse(p, media_type="image/png")


@app.get("/api/maps/{map_name}/nodes")
def nodes_overlay(map_name: str) -> dict:
    nodes = mapdata.get_nodes(map_name)
    if nodes is None:
        raise HTTPException(404, "unknown map")
    labels = _node_labels(map_name)
    return {
        "k": nodes.k,
        "labels": {str(i): labels.get(i, f"node {i}") for i in range(nodes.k)},
        "centroids": nodes.node_centroid.round(1).tolist(),
        "quads": nodes.quad_xy.round(1).tolist(),
        "quad_node": nodes.quad_node.tolist(),
        "quad_z": nodes.quad_z.round(1).tolist(),
        "lower_level_max_units": nodes.lower_max,
    }


@app.get("/api/resolve")
def resolve(map_name: str, x: float, y: float, level: str | None = None) -> dict:
    nodes = mapdata.get_nodes(map_name)
    if nodes is None:
        raise HTTPException(404, "unknown map")
    node = nodes.node_at(x, y, level=level)
    labels = _node_labels(map_name)
    return {"node": node, "label": labels.get(node, f"node {node}") if node is not None else None}


@app.post("/api/ingest/scan")
def scan() -> dict:
    _demo_forbidden()
    started = scanner.start_scan_thread()
    if not started:
        return JSONResponse({"started": False, "reason": "scan already running"}, status_code=409)
    return {"started": True}


@app.get("/api/ingest/status")
def scan_status() -> dict:
    return scanner.STATUS.to_dict()


@app.get("/api/demos")
def demos() -> list[dict]:
    _demo_forbidden()
    conn = dbmod.connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT d.demo_id, d.filename, d.map_name, d.demo_date, d.status, d.error_msg, "
            "(SELECT COUNT(*) FROM rounds r WHERE r.demo_id=d.demo_id) n_rounds "
            "FROM demos d ORDER BY d.demo_id DESC")]
    finally:
        conn.close()


@app.get("/api/library-summary")
def library_summary() -> dict:
    """Count indexed demos without exposing the hosted library catalog."""
    conn = dbmod.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) n_demos FROM demos WHERE status='ok'").fetchone()
        return {"n_demos": row["n_demos"]}
    finally:
        conn.close()


@app.post("/api/search")
def search(req: SearchRequest, request: Request) -> dict:
    _check_rate(request, "search", limit=30)
    try:
        res = engine.search(
            req.map_name,
            [p.model_dump() for p in req.ct_points],
            [p.model_dump() for p in req.t_points],
            max_dist=req.max_distance,
            filters=req.filters.model_dump(),
            limit=req.limit,
            smoke_points=[p.model_dump() for p in req.smoke_points],
            molly_points=[p.model_dump() for p in req.molly_points],
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    labels = _node_labels(req.map_name)
    for side in ("ct", "t", "smoke", "molly"):
        for p in res["resolved"][side]:
            p["label"] = labels.get(p["node"], f"node {p['node']}") if p["node"] is not None else None
    return res


def _recording_parts(conn, r) -> list[int]:
    """Demo ids for all restart parts of this map, in playback order."""
    match = re.match(r"^(.*)-p(\d+)\.dem$", r["filename"], re.IGNORECASE)
    if not match:
        return [r["demo_id"]]
    prefix = match.group(1)
    # Filename identity is authoritative here. Metadata can legitimately vary
    # between restart files (missing overlay, alias spelling, side order), and
    # must not make a part disappear from navigation.
    candidates = conn.execute(
        "SELECT demo_id, filename FROM demos WHERE map_name=? "
        "AND substr(demo_date, 1, 10) IS ?",
        (r["mn"], (r["demo_date"] or "")[:10] or None)).fetchall()
    parts = []
    for demo in candidates:
        sibling = re.match(rf"^{re.escape(prefix)}-p(\d+)\.dem$", demo["filename"], re.IGNORECASE)
        if sibling:
            parts.append((int(sibling.group(1)), demo["demo_id"]))
    return [demo_id for _, demo_id in sorted(parts)] or [r["demo_id"]]


def _match_kda(conn, r, part_ids: list[int] | None = None) -> dict[str, dict]:
    """K/D/A totals from the rounds BEFORE this one, so the playback panels can
    show match-to-date numbers (the current round is tallied client-side as it
    plays). Team kills score -1, matching the live tally."""
    out: dict[str, dict] = {}

    def get(name: str) -> dict:
        return out.setdefault(name, {"k": 0, "d": 0, "a": 0})

    part_ids = part_ids or [r["demo_id"]]
    ordered = {demo_id: i for i, demo_id in enumerate(part_ids)}
    current_part = ordered[r["demo_id"]]
    placeholders = ",".join("?" for _ in part_ids)
    for k in conn.execute(
            "SELECT k.attacker, k.attacker_side, k.victim, k.victim_side, k.assister, "
            "rr.demo_id, rr.round_num FROM kills k JOIN rounds rr ON rr.round_id=k.round_id "
            f"WHERE rr.demo_id IN ({placeholders})", part_ids):
        if ordered[k["demo_id"]] > current_part or (ordered[k["demo_id"]] == current_part
                                                     and k["round_num"] >= r["round_num"]):
            continue
        if k["victim"]:
            get(k["victim"])["d"] += 1
        if k["attacker"] and k["attacker"] != k["victim"]:
            get(k["attacker"])["k"] += -1 if k["attacker_side"] == k["victim_side"] else 1
        if k["assister"]:
            get(k["assister"])["a"] += 1
    return out


def _match_context(conn, r) -> dict:
    """Team names by side and running match score entering this round."""
    part_ids = _recording_parts(conn, r)
    placeholders = ",".join("?" for _ in part_ids)
    raw_rows = conn.execute(
        "SELECT round_id, round_num, winner, ct_team, demo_id FROM rounds "
        f"WHERE demo_id IN ({placeholders}) ORDER BY demo_id, round_num", part_ids).fetchall()
    order = {demo_id: i for i, demo_id in enumerate(part_ids)}
    rows = sorted(raw_rows, key=lambda rr: (order[rr["demo_id"]], rr["round_num"]))
    current_index = next(i for i, rr in enumerate(rows) if rr["round_id"] == r["round_id"])
    t1, t2 = r["team1"], r["team2"]

    def other(name):
        return t2 if name == t1 else t1 if name == t2 else None

    wins: dict[str, int] = {}
    for i, rr in enumerate(rows):
        if i >= current_index or rr["winner"] not in ("CT", "T") or not rr["ct_team"]:
            continue
        winner_team = rr["ct_team"] if rr["winner"] == "CT" else other(rr["ct_team"])
        if winner_team:
            wins[winner_team] = wins.get(winner_team, 0) + 1
    ct_name = r["ct_team"]
    t_name = other(ct_name) if ct_name else None
    ctx = {
        "ct_team": ct_name or "CT", "t_team": t_name or "T",
        "ct_score": wins.get(ct_name, 0), "t_score": wins.get(t_name, 0),
        "has_teams": bool(ct_name),
    }
    ctx["round_num"] = current_index + 1
    return ctx


def _normalize_kill_sides(frames: list[dict], kills: list[dict]) -> None:
    """Repair event sides from stable per-round frame observations in place."""
    observed: dict[str, Counter] = defaultdict(Counter)
    for frame in frames:
        for player in frame["players"]:
            observed[player["name"]][player["side"]] += 1
    stable_side = {name: counts.most_common(1)[0][0]
                   for name, counts in observed.items() if counts}
    for kill in kills:
        if kill["attacker"] in stable_side:
            kill["attacker_side"] = stable_side[kill["attacker"]]
        if kill["victim"] in stable_side:
            kill["victim_side"] = stable_side[kill["victim"]]


@app.get("/api/rounds/{round_id}/playback")
def playback(round_id: int, request: Request) -> dict:
    _check_rate(request, "playback", limit=90)
    conn = dbmod.connect()
    try:
        r = conn.execute(
            "SELECT r.*, d.filename, d.map_name mn, d.team1, d.team2, d.tickrate, "
            "d.demo_date, d.event, d.hltv_id "
            "FROM rounds r JOIN demos d ON d.demo_id=r.demo_id "
            "WHERE r.round_id=?", (round_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown round")
        match_ctx = _match_context(conn, r)
        names = {row["slot"]: row["name"] for row in conn.execute(
            "SELECT slot, name FROM demo_players WHERE demo_id=?", (r["demo_id"],))}
        wnames = {row["weapon_idx"]: row["name"] for row in conn.execute(
            "SELECT weapon_idx, name FROM weapons")}
        frames = []
        for row in conn.execute(
                "SELECT round_time_s, bomb_planted, positions, bomb_x, bomb_y, bomb_z "
                "FROM states WHERE round_id=? ORDER BY tick", (round_id,)):
            players = []
            for slot, p in enumerate(unpack_positions(row["positions"])):
                if p is None:
                    continue
                d = {"x": round(p["x"], 1), "y": round(p["y"], 1), "z": round(p["z"], 1),
                     "side": "CT" if p["ct"] else "T", "alive": p["alive"],
                     "health": p["health"], "name": names.get(slot, "?"),
                     "yaw": round(p["yaw"], 1) if p.get("yaw") is not None else None}
                if "armor" in p:                     # v3 blobs: economy/inventory/flash
                    d.update({"armor": p["armor"], "money": p["money"],
                              "flash": p["flash_s"], "util": p["util"],
                              "prim": wnames.get(p["primary"]),
                              "sec": wnames.get(p["secondary"])})
                players.append(d)
            bomb = None
            if row["bomb_x"] is not None:
                bomb = {"x": round(row["bomb_x"], 1), "y": round(row["bomb_y"], 1),
                        "z": round(row["bomb_z"], 1)}
            frames.append({"t": row["round_time_s"], "bomb_planted": bool(row["bomb_planted"]),
                           "players": players, "bomb": bomb})
        kills = [dict(row) for row in conn.execute(
            "SELECT round_time_s t, attacker, attacker_side, victim, victim_side, weapon, "
            "headshot, assister FROM kills WHERE round_id=? ORDER BY tick", (round_id,))]
        # Old indexes may contain a terminal kill whose event-side properties
        # already reflect the next round. Reconstruct stable round sides from
        # the sampled frames so halftime colors remain CT blue / T orange.
        _normalize_kill_sides(frames, kills)
        shots = []
        blob = r["shots"]
        if blob and blob[0] == 1 and (len(blob) - 1) % 3 == 0:
            hz = float(r["tickrate"] or 64.0)
            for off in range(1, len(blob), 3):
                rel, slot = int.from_bytes(blob[off:off + 2], "little"), blob[off + 2]
                shots.append([round(rel / hz, 2), slot])
        grenades = []
        for row in conn.execute(
                "SELECT type, round_time_s t, end_time_s t_end, x, y, z, thrower_side, path "
                "FROM grenades WHERE round_id=? ORDER BY tick", (round_id,)):
            g = {k: row[k] for k in ("type", "t", "t_end", "x", "y", "z", "thrower_side")}
            if row["path"]:
                g["path"] = unpack_path(row["path"])
            grenades.append(g)
        return {
            "round": {"round_id": round_id, "demo": r["filename"], "map_name": r["mn"],
                      "round_num": r["round_num"], "winner": r["winner"],
                      "event": r["event"], "hltv_id": r["hltv_id"],
                      "win_reason": r["win_reason"],
                      "end_s": max(0, round((r["end_tick"] - r["freeze_end_tick"])
                                            / float(r["tickrate"] or 64.0))),
                      "ct_buy": r["ct_buy"], "t_buy": r["t_buy"], "bomb_site": r["bomb_site"],
                      **match_ctx},
            "match_kda": _match_kda(conn, r, _recording_parts(conn, r)),
            "tick_hz": 1,
            "frames": frames,
            "kills": kills,
            "grenades": grenades,
            "shots": shots,                    # [t_s, roster slot], guns only
            "slots": names,                    # roster slot -> player name
        }
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
