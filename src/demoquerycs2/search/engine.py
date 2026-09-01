"""Retrieval engine: geodesic Chamfer recall over node tokens + exact assignment re-rank.

Distance between a drawn point q and a stored player p:
    cost(q, p) = max(geo(node_q, node_p), euclid2d(q, p) if same level else 0)
Both terms lower-bound walking distance, so max() is the tighter bound: geo handles
walls/floors that Euclidean ignores; Euclidean refines within/near a single node.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from itertools import permutations

import numpy as np

from .. import config
from .. import db as dbmod
from .. import mapdata
from ..ingest.tokenizer import TOKEN_LEN, unpack_positions

BUY_CODE = {"eco": 0, "semi": 1, "full": 2, "pistol": 3}
WINNER_CODE = {"CT": 1, "T": 2}
STATS_BIN_S = 10           # timing-histogram bin width (pre-plant round clock)
# round_end reasons, split by winning side (rounds.win_reason; 'unknown' = not backfilled)
CT_WIN_REASONS = ("t_killed", "bomb_defused", "time_ran_out")
T_WIN_REASONS = ("ct_killed", "bomb_exploded")
STAGE2_CAP = 1500          # max stage-1 candidates that get exact re-ranking
MOMENT_GAP_S = 2
MAX_ACTIVE_SMOKES = 10     # per-state slots; every player can hold one smoke/molly,
MAX_ACTIVE_MOLLIES = 10    # so a full execute + retake can stack up to 10 of each
# Utility markers use a fixed match radius, not the player-tolerance slider: a smoke
# is a fixed-size object (~145u wide), so "smoke here" is near-binary. The exact
# nearest distance still feeds the ranking score so tighter placements sort first.
UTIL_STAGE1_GEO = 400.0    # loose node-level recall gate
UTIL_MATCH_RADIUS = 250.0  # stage-2 hard gate on exact distance


class UtilCSR:
    """Per-state active-utility slots, stored sparse (CSR): only occupied slots.

    Replaces the dense (N,cap) node + (N,cap,3) xyz pair - >90% of slots are
    empty, so this cuts the utility arrays by an order of magnitude. Slot order
    within a state matches the dense builder (insertion order, capped)."""

    __slots__ = ("row_ptr", "nodes", "xyz")

    def __init__(self, row_ptr: np.ndarray, nodes: np.ndarray, xyz: np.ndarray):
        self.row_ptr = row_ptr      # (N+1,) int32
        self.nodes = nodes          # (K,) uint8
        self.xyz = xyz              # (K,3) float32

    @classmethod
    def from_dense(cls, dense_nodes: np.ndarray, dense_xyz: np.ndarray) -> "UtilCSR":
        occ = dense_nodes != 0xFF                    # builder packs slots from 0, row-major
        counts = occ.sum(axis=1, dtype=np.int64)
        row_ptr = np.zeros(len(counts) + 1, dtype=np.int64)
        np.cumsum(counts, out=row_ptr[1:])
        return cls(row_ptr.astype(np.int32), dense_nodes[occ].copy(), dense_xyz[occ].copy())

    def has_any(self) -> np.ndarray:
        return self.row_ptr[1:] > self.row_ptr[:-1]

    def min_geo(self, geo_row: np.ndarray) -> np.ndarray:
        """Per-state min geodesic distance from a query node to any active slot.

        geo_row: one row of geo_padded for the query node. States with no
        active utility get the pad value geo_row[0xFF] (= geo max cap), matching
        what the dense 0xFF gather produced."""
        n = len(self.row_ptr) - 1
        out = np.full(n, geo_row[0xFF], dtype=np.float32)
        if len(self.nodes) == 0:
            return out
        d = geo_row[self.nodes]
        nz = self.row_ptr[:-1] < self.row_ptr[1:]
        # non-empty segments are contiguous in d, so their starts are reduceat bounds
        out[nz] = np.minimum.reduceat(d, self.row_ptr[:-1][nz])
        return out

    def row(self, si: int) -> tuple[np.ndarray, np.ndarray]:
        a, b = self.row_ptr[si], self.row_ptr[si + 1]
        return self.nodes[a:b], self.xyz[a:b]

    def nbytes_total(self) -> int:
        return self.row_ptr.nbytes + self.nodes.nbytes + self.xyz.nbytes


@dataclass
class MapIndex:
    state_id: np.ndarray
    round_id: np.ndarray
    demo_id: np.ndarray
    tick: np.ndarray
    sec: np.ndarray
    bomb: np.ndarray
    alive_ct: np.ndarray
    alive_t: np.ndarray
    ct_nodes: np.ndarray       # (N,5) uint8, 0xFF pad
    t_nodes: np.ndarray
    ct_buy: np.ndarray         # int8 codes; pistol rounds are coded 3 regardless of value
    t_buy: np.ndarray
    bomb_site: np.ndarray      # 0 none, 1 A, 2 B
    winner_code: np.ndarray    # (N,) int8 round winner: 0 unknown, 1 CT, 2 T
    ct_team_code: np.ndarray   # (N,) int32 index into team_names; -1 = unknown
    team_names: list[str]      # distinct clan names seen on CT, code order
    smoke: UtilCSR
    molly: UtilCSR
    # positions blobs deliberately NOT held in RAM (~240B/state): stage 2 and
    # hydration read them back from SQLite for just the candidate states.


_indexes: dict[str, MapIndex] = {}
_ilock = threading.Lock()


def invalidate(map_names: set[str] | None = None) -> None:
    with _ilock:
        if map_names is None:
            _indexes.clear()
        else:
            for m in map_names:
                _indexes.pop(m, None)


def _build_active_utility(round_id: np.ndarray, sec: np.ndarray,
                          rows: list[tuple], cap: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-state active-utility slots. rows: (round_id, t_start, t_end, node_idx, x, y, z).

    Returns (nodes (N,cap) uint8 0xFF-padded, xyz (N,cap,3) float32)."""
    n = len(round_id)
    nodes = np.full((n, cap), 0xFF, dtype=np.uint8)
    xyz = np.zeros((n, cap, 3), dtype=np.float32)
    counts = np.zeros(n, dtype=np.int32)
    state_rows: dict[int, np.ndarray] = {}
    for rid, t0, t1, node, x, y, z in rows:
        idxs = state_rows.get(rid)
        if idxs is None:
            idxs = state_rows[rid] = np.nonzero(round_id == rid)[0]
        sel = idxs[(sec[idxs] >= t0) & (sec[idxs] <= t1)]
        sel = sel[counts[sel] < cap]               # drop overflow beyond cap
        nodes[sel, counts[sel]] = node
        xyz[sel, counts[sel]] = (x, y, z)
        counts[sel] += 1
    return nodes, xyz


_BUILD_CHUNK = 16384       # rows materialized at a time; bounds the build's transient RAM


def _load_index(map_name: str) -> MapIndex:
    site_code = {"A": 1, "B": 2}
    conn = dbmod.connect()
    try:
        # one read transaction so COUNT, states and grenades see the same snapshot
        conn.execute("BEGIN")
        n = conn.execute("SELECT COUNT(*) FROM states WHERE map_name = ?", (map_name,)).fetchone()[0]
        state_id = np.empty(n, dtype=np.int64)
        round_id = np.empty(n, dtype=np.int64)
        demo_id = np.empty(n, dtype=np.int64)
        tick = np.empty(n, dtype=np.int64)
        sec = np.empty(n, dtype=np.int32)
        bomb = np.empty(n, dtype=bool)
        alive_ct = np.empty(n, dtype=np.int8)
        alive_t = np.empty(n, dtype=np.int8)
        ct_buy = np.empty(n, dtype=np.int8)
        t_buy = np.empty(n, dtype=np.int8)
        is_pistol = np.empty(n, dtype=bool)
        bomb_site = np.empty(n, dtype=np.int8)
        winner_code = np.empty(n, dtype=np.int8)
        ct_team_code = np.empty(n, dtype=np.int32)
        tok_buf = bytearray(n * TOKEN_LEN)
        team_codes: dict[str, int] = {}
        team_names: list[str] = []

        # positions blobs are intentionally not selected: they dominate row size
        # and the index never stores them (fetched on demand at search time)
        cur = conn.execute(
            "SELECT s.state_id, s.round_id, s.demo_id, s.tick, s.round_time_s, s.token, "
            "s.bomb_planted, s.alive_ct, s.alive_t, r.ct_buy, r.t_buy, r.bomb_site, "
            "r.is_pistol, r.ct_team, r.winner "
            "FROM states s JOIN rounds r ON r.round_id = s.round_id "
            "WHERE s.map_name = ? ORDER BY s.state_id", (map_name,))
        i = 0
        while True:
            rows = cur.fetchmany(_BUILD_CHUNK)
            if not rows:
                break
            j = i + len(rows)
            state_id[i:j] = [r["state_id"] for r in rows]
            round_id[i:j] = [r["round_id"] for r in rows]
            demo_id[i:j] = [r["demo_id"] for r in rows]
            tick[i:j] = [r["tick"] for r in rows]
            sec[i:j] = [r["round_time_s"] for r in rows]
            bomb[i:j] = [r["bomb_planted"] for r in rows]
            alive_ct[i:j] = [r["alive_ct"] for r in rows]
            alive_t[i:j] = [r["alive_t"] for r in rows]
            ct_buy[i:j] = [BUY_CODE.get(r["ct_buy"], 2) for r in rows]
            t_buy[i:j] = [BUY_CODE.get(r["t_buy"], 2) for r in rows]
            is_pistol[i:j] = [bool(r["is_pistol"]) for r in rows]
            bomb_site[i:j] = [site_code.get(r["bomb_site"], 0) for r in rows]
            winner_code[i:j] = [WINNER_CODE.get(r["winner"], 0) for r in rows]
            tok_buf[i * TOKEN_LEN:j * TOKEN_LEN] = b"".join(r["token"] for r in rows)
            codes = []
            for r in rows:
                name = r["ct_team"]
                if not name:
                    codes.append(-1)
                    continue
                code = team_codes.get(name)
                if code is None:
                    code = team_codes[name] = len(team_names)
                    team_names.append(name)
                codes.append(code)
            ct_team_code[i:j] = codes
            i = j

        gren = conn.execute(
            "SELECT g.round_id, g.type, g.round_time_s, g.end_time_s, g.node_idx, g.x, g.y, g.z "
            "FROM grenades g JOIN demos d ON d.demo_id = g.demo_id "
            "WHERE d.map_name = ? AND g.type IN ('smoke','molly')", (map_name,)).fetchall()
    finally:
        conn.rollback()
        conn.close()

    util_rows: dict[str, list[tuple]] = {"smoke": [], "molly": []}
    for g in gren:
        t1 = g["end_time_s"] if g["end_time_s"] is not None else g["round_time_s"]
        util_rows[g["type"]].append(
            (g["round_id"], g["round_time_s"], t1, g["node_idx"], g["x"], g["y"], g["z"]))
    smoke = UtilCSR.from_dense(*_build_active_utility(round_id, sec, util_rows["smoke"], MAX_ACTIVE_SMOKES))
    molly = UtilCSR.from_dense(*_build_active_utility(round_id, sec, util_rows["molly"], MAX_ACTIVE_MOLLIES))
    tok = np.frombuffer(bytes(tok_buf), dtype=np.uint8).reshape(n, TOKEN_LEN) if n else np.zeros((0, TOKEN_LEN), np.uint8)
    ct_buy[is_pistol] = BUY_CODE["pistol"]     # pistol is its own category, not a spend class
    t_buy[is_pistol] = BUY_CODE["pistol"]
    return MapIndex(
        state_id=state_id,
        round_id=round_id,
        demo_id=demo_id,
        tick=tick,
        sec=sec,
        bomb=bomb,
        alive_ct=alive_ct,
        alive_t=alive_t,
        ct_nodes=tok[:, :5].copy(),
        t_nodes=tok[:, 5:].copy(),
        ct_buy=ct_buy,
        t_buy=t_buy,
        bomb_site=bomb_site,
        winner_code=winner_code,
        ct_team_code=ct_team_code,
        team_names=team_names,
        smoke=smoke,
        molly=molly,
    )


def get_index(map_name: str) -> MapIndex:
    with _ilock:
        if map_name not in _indexes:
            _indexes[map_name] = _load_index(map_name)
        return _indexes[map_name]


def _filter_mask(idx: MapIndex, f: dict) -> np.ndarray:
    mask = np.ones(len(idx.state_id), dtype=bool)
    sites = f.get("bomb_sites")
    if sites is not None:
        # chip UI: any subset of {A, B, none}; all or none selected = no bomb filter
        sel = {s for s in sites if s in ("A", "B", "none")}
        if sel and sel != {"A", "B", "none"}:
            m = np.zeros(len(mask), dtype=bool)
            if {"A", "B"} <= sel:
                m |= idx.bomb          # any plant - keeps site-unknown plants too
            elif "A" in sel:
                m |= idx.bomb & (idx.bomb_site == 1)
            elif "B" in sel:
                m |= idx.bomb & (idx.bomb_site == 2)
            if "none" in sel:
                m |= ~idx.bomb
            mask &= m
    else:                                         # legacy single-select clients
        if f.get("bomb_planted") is not None:
            mask &= idx.bomb == bool(f["bomb_planted"])
        if f.get("bomb_site") in ("A", "B"):
            mask &= idx.bomb_site == (1 if f["bomb_site"] == "A" else 2)
    for side, arr in (("ct_buy", idx.ct_buy), ("t_buy", idx.t_buy)):
        sel = f.get(side)
        if sel:
            codes = [BUY_CODE[b] for b in sel if b in BUY_CODE]
            mask &= np.isin(arr, codes)
    for side, arr in (("alive_ct", idx.alive_ct), ("alive_t", idx.alive_t)):
        rng = f.get(side)
        if rng and len(rng) == 2:
            mask &= (arr >= rng[0]) & (arr <= rng[1])
    rng = f.get("time_range")
    if rng and len(rng) == 2:
        mask &= (idx.sec >= rng[0]) & (idx.sec <= rng[1])
    rng = f.get("time_left")
    if rng and len(rng) == 2:
        # the round clock only exists pre-plant: planted states pass untouched
        # (their inclusion is governed by the phase/site chips)
        remaining = np.clip(config.ROUND_CLOCK_S - idx.sec, 0, None)
        lo, hi = min(rng), max(rng)
        mask &= idx.bomb | ((remaining >= lo) & (remaining <= hi))
    for key, csr in (("smoke_active", idx.smoke), ("molly_active", idx.molly)):
        want = f.get(key)
        if want is not None:
            mask &= csr.has_any() == bool(want)
    return mask


def _chamfer(nodes_mx: mapdata.MapNodes, q_nodes: list[int], side_tokens: np.ndarray) -> np.ndarray:
    """Mean over query points of min geodesic distance to any of the side's players. (N,)"""
    costs = np.zeros(len(side_tokens), dtype=np.float32)
    for qn in q_nodes:
        d = nodes_mx.geo_padded[qn][side_tokens]     # (N,5); pad id 255 -> max cap
        costs += d.min(axis=1)
    return costs / max(1, len(q_nodes))


def _assignment_cost(q_pts: list[dict], players: list[dict],
                     nodes_mx: mapdata.MapNodes) -> float:
    """Optimal pairing cost between drawn points and alive players of one side."""
    if not q_pts:
        return 0.0
    if not players:
        return float(nodes_mx.geo_padded.max())
    k = len(q_pts)
    cost = np.empty((k, len(players)), dtype=np.float32)
    for i, q in enumerate(q_pts):
        for j, p in enumerate(players):
            geo = nodes_mx.geo_padded[q["node"], p["node_idx"]]
            same_level = (q.get("level") is None or nodes_mx.lower_max is None
                          or (q["level"] == "lower") == nodes_mx.is_lower(p["z"]))
            eu = float(np.hypot(q["x"] - p["x"], q["y"] - p["y"])) if same_level else 0.0
            cost[i, j] = max(geo, eu)
    m = cost.shape[1]
    best = np.inf
    for perm in permutations(range(m), min(k, m)):
        c = sum(cost[i, perm[i]] for i in range(min(k, m)))
        if c < best:
            best = c
    if k > m:                                        # more drawn than alive: heavy penalty
        best += (k - m) * float(nodes_mx.geo_padded.max())
    return float(best) / k


def _fetch_positions(conn: sqlite3.Connection, state_ids: np.ndarray) -> dict[int, bytes]:
    """Positions blobs for a candidate set, read back by primary key (chunked IN)."""
    return {r["state_id"]: bytes(r["positions"]) for r in _rows_in(
        conn, "SELECT state_id, positions FROM states WHERE state_id IN (%s)",
        [int(s) for s in state_ids])}


def _round_reps(idx: MapIndex, sel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distinct rounds in a state selection + one representative state per round.

    States are round-contiguous and time-ascending in the index, so the
    representative is each round's earliest matching state."""
    rids, first = np.unique(idx.round_id[sel], return_index=True)
    return rids, sel[first]


def _win_counts(idx: MapIndex, reps: np.ndarray) -> dict:
    c = np.bincount(idx.winner_code[reps], minlength=3)
    return {"ct": int(c[1]), "t": int(c[2])}


def _rows_in(conn: sqlite3.Connection, sql_tmpl: str, ids: list[int]) -> list[sqlite3.Row]:
    """Run an `IN (%s)` query in chunks that stay under SQLite's variable limit."""
    out: list[sqlite3.Row] = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        out.extend(conn.execute(sql_tmpl % ",".join("?" * len(chunk)), chunk))
    return out


def _compute_stats(idx: MapIndex, sel_all: np.ndarray, sel_exact: np.ndarray,
                   filter_mask: np.ndarray, conn: sqlite3.Connection) -> dict:
    """Round-level scenario stats over the full match population (pre-cap, so
    they are not biased toward the best-scoring result cards).

    Winrate reports exact and close buckets (a round with any exact state is
    exact, mirroring the results-list split) plus the filters-only baseline;
    every other block uses exact rounds only. Counts go over the wire, not
    percentages - the UI derives those and shows sample sizes honestly."""
    all_rids, all_reps = _round_reps(idx, sel_all)
    if sel_exact is sel_all:
        # filter-only search: everything matches "exactly" and the mask IS the
        # filter mask, so one population serves all three buckets
        exact_rids, exact_reps = all_rids, all_reps
        close_reps = all_reps[:0]
        base_rids, base_reps = all_rids, all_reps
    else:
        exact_rids, exact_reps = _round_reps(idx, sel_exact)
        close_reps = all_reps[~np.isin(all_rids, exact_rids)]
        base_rids, base_reps = _round_reps(idx, np.nonzero(filter_mask)[0])

    stats: dict = {
        "rounds": {"exact": int(exact_rids.size), "close": int(close_reps.size),
                   "baseline": int(base_rids.size),
                   "demos": int(np.unique(idx.demo_id[all_reps]).size)},
        "winrate": {"exact": _win_counts(idx, exact_reps),
                    "close": _win_counts(idx, close_reps),
                    "baseline": _win_counts(idx, base_reps)},
    }

    # timing: rounds covered per pre-plant clock bin (a round counts once per
    # bin its exact states touch, so long holds fill bins without over-voting
    # inside one) + a single post-plant bucket
    n_bins = -(-config.ROUND_CLOCK_S // STATS_BIN_S)
    pre = sel_exact[~idx.bomb[sel_exact]]
    bins = np.minimum(idx.sec[pre] // STATS_BIN_S, n_bins - 1)      # timeout slop -> last bin
    pairs = np.unique(idx.round_id[pre] * n_bins + bins)
    counts = np.bincount(pairs % n_bins, minlength=n_bins)
    post = np.unique(idx.round_id[sel_exact[idx.bomb[sel_exact]]])
    stats["timing"] = {"bin_s": STATS_BIN_S, "clock_s": int(config.ROUND_CLOCK_S),
                       "pre": [int(v) for v in counts], "post_plant": int(post.size)}

    # economy: buy mix of the exact rounds; pistol is its own category
    econ = {}
    for side, arr in (("ct", idx.ct_buy), ("t", idx.t_buy)):
        c = np.bincount(arr[exact_reps], minlength=4)
        econ[side] = {"eco": int(c[0]), "semi": int(c[1]), "full": int(c[2]), "pistol": int(c[3])}
    stats["economy"] = econ

    # win conditions + who plays it, from the rounds table (exact rounds only)
    reasons_ct = dict.fromkeys(CT_WIN_REASONS + ("unknown",), 0)
    reasons_t = dict.fromkeys(T_WIN_REASONS + ("unknown",), 0)
    ct_teams: Counter = Counter()
    t_teams: Counter = Counter()
    rows = _rows_in(
        conn,
        "SELECT r.round_id, r.winner, r.win_reason, r.ct_team, d.team1, d.team2 "
        "FROM rounds r JOIN demos d ON d.demo_id = r.demo_id WHERE r.round_id IN (%s)",
        [int(r) for r in exact_rids])
    for r in rows:
        tally = reasons_ct if r["winner"] == "CT" else reasons_t if r["winner"] == "T" else None
        if tally is not None:
            reason = r["win_reason"]
            tally[reason if reason in tally else "unknown"] += 1
        ct_name = r["ct_team"]
        if ct_name:
            ct_teams[ct_name] += 1
            t_name = (r["team2"] if ct_name == r["team1"]
                      else r["team1"] if ct_name == r["team2"] else None)
            if t_name:
                t_teams[t_name] += 1
    stats["win_reasons"] = {"ct": reasons_ct, "t": reasons_t}
    stats["teams"] = {side: [{"name": n, "rounds": c} for n, c in teams.most_common(3)]
                      for side, teams in (("ct", ct_teams), ("t", t_teams))}

    # next kill after the scenario first forms (per-round anchor = earliest
    # exact state). Credited to the side that did NOT lose the player, which
    # stays correct through team kills and fall deaths. Kills stamped with the
    # anchor second itself are excluded on purpose: at 1 Hz resolution they may
    # be the very kill that created the queried state.
    anchor = {int(r): int(s) for r, s in zip(exact_rids, idx.sec[exact_reps])}
    credited: dict[int, tuple[str, int]] = {}
    for k in _rows_in(
            conn,
            "SELECT round_id, round_time_s, victim_side FROM kills WHERE round_id IN (%s) "
            "ORDER BY round_id, tick",
            [int(r) for r in exact_rids]):
        rid = k["round_id"]
        if rid in credited or k["round_time_s"] <= anchor[rid]:
            continue
        if k["victim_side"] == "CT":
            credited[rid] = ("t", k["round_time_s"] - anchor[rid])
        elif k["victim_side"] == "T":
            credited[rid] = ("ct", k["round_time_s"] - anchor[rid])
    nk_ct = sum(1 for side, _ in credited.values() if side == "ct")
    deltas = sorted(d for _, d in credited.values())
    stats["next_kill"] = {"ct": nk_ct, "t": len(credited) - nk_ct,
                          "none": int(exact_rids.size) - len(credited),
                          "median_s": int(np.median(deltas)) if deltas else None}
    return stats


def _utility_cost(q_pts: list[dict], unodes_row: np.ndarray, uxyz_row: np.ndarray,
                  nodes_mx: mapdata.MapNodes) -> float:
    """Mean nearest-active-utility distance; inf if any marker has none within UTIL_MATCH_RADIUS.

    Markers are independent (two smokes can share a spot), so nearest-match per
    marker replaces the permutation assignment used for players."""
    total = 0.0
    for q in q_pts:
        best = np.inf
        for j in range(len(unodes_row)):
            un = int(unodes_row[j])
            if un == 0xFF:
                continue
            geo = float(nodes_mx.geo_padded[q["node"], un])
            ux, uy, uz = (float(v) for v in uxyz_row[j])
            same_level = (q.get("level") is None or nodes_mx.lower_max is None
                          or (q["level"] == "lower") == nodes_mx.is_lower(uz))
            eu = float(np.hypot(q["x"] - ux, q["y"] - uy)) if same_level else 0.0
            best = min(best, max(geo, eu))
        if best > UTIL_MATCH_RADIUS:
            return float("inf")
        total += best
    return total / max(1, len(q_pts))


def search(map_name: str, ct_points: list[dict], t_points: list[dict],
           max_dist: float, filters: dict, limit: int = 50,
           smoke_points: list[dict] | None = None,
           molly_points: list[dict] | None = None) -> dict:
    t0 = time.time()
    nodes_mx = mapdata.get_nodes(map_name)
    if nodes_mx is None:
        raise ValueError(f"unknown map {map_name}")
    idx = get_index(map_name)
    smoke_points = smoke_points or []
    molly_points = molly_points or []
    if len(idx.state_id) == 0:
        return {"moments": [], "resolved": {"ct": [], "t": [], "smoke": [], "molly": []},
                "n_scanned": 0, "stats": None,
                "elapsed_ms": 0, "error": "no data for this map - scan demos first"}

    # resolve drawn points to nodes
    for pts in (ct_points, t_points, smoke_points, molly_points):
        for p in pts:
            p["node"] = nodes_mx.node_at(p["x"], p["y"], level=p.get("level"))
    ct_q = [p for p in ct_points if p["node"] is not None]
    t_q = [p for p in t_points if p["node"] is not None]
    smoke_q = [p for p in smoke_points if p["node"] is not None][:10]
    molly_q = [p for p in molly_points if p["node"] is not None][:10]
    spatial = bool(ct_q or t_q or smoke_q or molly_q)

    mask = _filter_mask(idx, filters or {})
    conn = dbmod.connect()
    try:
        # filters-only population, before the sketch's utility gate narrows the
        # mask: the baseline the scenario winrate is compared against (only the
        # utility gate mutates mask after this point, so alias when there is none)
        filter_mask = mask.copy() if (smoke_q or molly_q) else mask

        # utility markers: hard node-level recall gate (fixed radius, not the slider);
        # zero-utility states resolve to the geo max cap, so they fail automatically
        util_node_dist = np.zeros(len(idx.state_id), dtype=np.float32)
        for q_list, csr in ((smoke_q, idx.smoke), (molly_q, idx.molly)):
            for q in q_list:
                d = csr.min_geo(nodes_mx.geo_padded[q["node"]])
                mask &= d <= UTIL_STAGE1_GEO
                np.maximum(util_node_dist, d, out=util_node_dist)

        cost = np.zeros(len(idx.state_id), dtype=np.float32)
        n_sides = 0
        if ct_q:
            cost += _chamfer(nodes_mx, [p["node"] for p in ct_q], idx.ct_nodes)
            n_sides += 1
        if t_q:
            cost += _chamfer(nodes_mx, [p["node"] for p in t_q], idx.t_nodes)
            n_sides += 1
        if n_sides:
            cost /= n_sides
        cost[~mask] = np.inf

        if spatial:
            # stats population: every in-tolerance state BEFORE the stage-2 cap,
            # so round stats cover the full match set, not the best-ranked slice
            sel_all = np.nonzero(cost <= max_dist)[0]
            sel_exact = sel_all[np.maximum(cost[sel_all], util_node_dist[sel_all]) <= 1e-6]
            if len(sel_exact) and (smoke_q or molly_q):
                # same-node utility can still sit > UTIL_MATCH_RADIUS euclidean away,
                # which stage 2 rejects outright - apply the identical rule (via the
                # same function) so "exact" never claims rounds the list cannot show
                ok = np.ones(len(sel_exact), dtype=bool)
                for i, si in enumerate(sel_exact):
                    for q_list, csr in ((smoke_q, idx.smoke), (molly_q, idx.molly)):
                        if q_list and not np.isfinite(_utility_cost(q_list, *csr.row(int(si)), nodes_mx)):
                            ok[i] = False
                            break
                sel_exact = sel_exact[ok]
            hit = sel_all
            if len(hit) > STAGE2_CAP:
                hit = hit[np.argsort(cost[hit], kind="stable")[:STAGE2_CAP]]
            # stage 2: exact assignment on candidates
            positions = _fetch_positions(conn, idx.state_id[hit])
            pos_scores = np.full(len(hit), np.inf, dtype=np.float32)
            for i, si in enumerate(hit):
                players = [p for p in unpack_positions(positions[int(idx.state_id[si])])
                           if p and p["alive"]]
                ct_players = [p for p in players if p["ct"]]
                t_players = [p for p in players if not p["ct"]]
                s = c = 0.0
                if ct_q:
                    s += _assignment_cost(ct_q, ct_players, nodes_mx); c += 1
                if t_q:
                    s += _assignment_cost(t_q, t_players, nodes_mx); c += 1
                if smoke_q:
                    s += _utility_cost(smoke_q, *idx.smoke.row(si), nodes_mx); c += 1
                if molly_q:
                    s += _utility_cost(molly_q, *idx.molly.row(si), nodes_mx); c += 1
                pos_scores[i] = s / c if c else 0.0
            # utility gating can reject candidates outright (inf) - drop them
            keep = np.isfinite(pos_scores)
            hit, pos_scores = hit[keep], pos_scores[keep]
        else:
            # no markers drawn: pure filter query (alive counts, bomb, buys, time);
            # every filtered state matches, moments rank by duration
            hit = np.nonzero(mask)[0]
            pos_scores = np.zeros(len(hit), dtype=np.float32)
            sel_all = sel_exact = hit                # a filter-only match is exact by definition

        # group into moments per round (consecutive seconds)
        order = np.lexsort((idx.tick[hit], idx.round_id[hit]))
        moments: list[dict] = []
        cur = None
        for oi in order:
            si, score = int(hit[oi]), float(pos_scores[oi])
            node_dist = float(max(cost[si], util_node_dist[si])) if spatial else 0.0
            rid, sec = int(idx.round_id[si]), int(idx.sec[si])
            if cur is not None and cur["round_id"] == rid and sec - cur["t_end"] <= MOMENT_GAP_S:
                cur["t_end"] = sec
                cur["node_dist"] = min(cur["node_dist"], node_dist)
                if score < cur["pos_score"]:
                    cur.update(pos_score=score, best_state=si)
            else:
                if cur is not None:
                    moments.append(cur)
                cur = {"round_id": rid, "t_start": sec, "t_end": sec, "pos_score": score,
                       "best_state": si, "node_dist": node_dist}
        if cur is not None:
            moments.append(cur)

        # exact moments sort ahead of close ones so the result cap can never drop
        # an exact round while keeping better-scoring close moments - the list
        # splits into Exact/Close sections and the stats strip counts exact
        # rounds over the full population, so both must agree on what survives
        moments.sort(key=lambda m: (m["node_dist"] > 1e-6, m["pos_score"],
                                    -(m["t_end"] - m["t_start"])))
        moments = moments[:limit]

        stats = _compute_stats(idx, sel_all, sel_exact, filter_mask, conn)
        out_moments = _hydrate_moments(idx, moments, conn)
    finally:
        conn.close()
    return {
        "spatial": spatial,
        "stats": stats,
        "resolved": {
            "ct": [{"node": p.get("node")} for p in ct_points],
            "t": [{"node": p.get("node")} for p in t_points],
            "smoke": [{"node": p.get("node")} for p in smoke_points],
            "molly": [{"node": p.get("node")} for p in molly_points],
        },
        "n_scanned": int(mask.sum()),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "moments": out_moments,
    }


def _hydrate_moments(idx: MapIndex, moments: list[dict], conn: sqlite3.Connection) -> list[dict]:
    if not moments:
        return []
    positions = _fetch_positions(conn, idx.state_id[[m["best_state"] for m in moments]])
    bombs = {r["state_id"]: r for r in _rows_in(
        conn, "SELECT state_id, bomb_x, bomb_y FROM states WHERE state_id IN (%s)",
        [int(idx.state_id[m["best_state"]]) for m in moments])}
    out = []
    for m in moments:
        si = m["best_state"]
        r = conn.execute(
            "SELECT r.round_num, r.winner, r.ct_buy, r.t_buy, r.bomb_site, r.is_pistol, "
            "d.filename, d.demo_id, d.team1, d.team2 FROM rounds r JOIN demos d ON d.demo_id = r.demo_id "
            "WHERE r.round_id = ?", (m["round_id"],)).fetchone()
        names = {row["slot"]: row["name"] for row in conn.execute(
            "SELECT slot, name FROM demo_players WHERE demo_id = ?", (r["demo_id"],))}
        players = []
        for slot, p in enumerate(unpack_positions(positions[int(idx.state_id[si])])):
            if p is None:
                continue
            players.append({
                "x": round(p["x"], 1), "y": round(p["y"], 1), "z": round(p["z"], 1),
                "side": "CT" if p["ct"] else "T", "alive": p["alive"],
                "health": p["health"], "name": names.get(slot, "?"),
            })
        bomb_row = bombs.get(int(idx.state_id[si]))
        bomb = ({"x": round(bomb_row["bomb_x"], 1), "y": round(bomb_row["bomb_y"], 1)}
                if bomb_row is not None and bomb_row["bomb_x"] is not None else None)
        utility = []
        for gtype, csr in (("smoke", idx.smoke), ("molly", idx.molly)):
            unodes, uxyz = csr.row(si)
            for j in range(len(unodes)):
                utility.append({"type": gtype,
                                "x": round(float(uxyz[j, 0]), 1),
                                "y": round(float(uxyz[j, 1]), 1),
                                "z": round(float(uxyz[j, 2]), 1)})
        out.append({
            "round_id": m["round_id"],
            "exact": m.get("node_dist", 0.0) <= 1e-6,
            "demo": r["filename"],
            "team1": r["team1"], "team2": r["team2"],
            "round_num": r["round_num"],
            "winner": r["winner"],
            "ct_buy": r["ct_buy"], "t_buy": r["t_buy"],
            "bomb_site": r["bomb_site"], "is_pistol": bool(r["is_pistol"]),
            "t_start": m["t_start"], "t_end": m["t_end"],
            "pos_score": round(m["pos_score"], 1),
            "snapshot": {
                "round_time_s": int(idx.sec[si]),
                "bomb_planted": bool(idx.bomb[si]),
                "bomb": bomb,
                "players": players,
                "utility": utility,
            },
        })
    return out
