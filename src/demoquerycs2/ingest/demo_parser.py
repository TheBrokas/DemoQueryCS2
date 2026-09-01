"""Parse one CS2 demo into rounds + 1 Hz states using demoparser2.

Pure function of (demo file, map nodes): no DB access, safe to run in worker
processes. Place names get demo-local indices; ParsedDemo.places lists them in
index order and the scanner translates to the global registry on insert.
"""
from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from ..mapdata import MapNodes
from ..navcluster import EMPTY_NODE
from . import buytypes, tokenizer
from .tokenizer import pack_positions, pack_token

TICKRATE = 64
BUY_EVAL_SECONDS = 5          # sample carried equipment after last-second purchases
MIN_ROUND_SECONDS = 10
SMOKE_FALLBACK_S = 20         # smoke lifetime when no paired smokegrenade_expired event
MOLLY_FALLBACK_S = 7          # inferno lifetime when no paired inferno_expire event
PATH_SAMPLE_TICKS = 4         # projectile path resolution (~16 Hz)
PATH_MATCH_DIST = 300.0       # max units between track end and detonation to pair them
PATH_MATCH_TICKS = 144        # track must end within this many ticks of the detonation

TICK_PROPS = ["X", "Y", "Z", "health", "is_alive", "team_num", "last_place_name",
              "current_equip_value", "is_bomb_planted", "team_clan_name", "yaw", "inventory",
              "armor_value", "balance", "flash_duration", "has_helmet", "has_defuser"]

EVENTS = ["round_announce_match_start", "round_freeze_end", "round_end",
          "bomb_planted", "player_death", "weapon_fire",
          "smokegrenade_detonate", "smokegrenade_expired",
          "flashbang_detonate", "hegrenade_detonate",
          "inferno_startburn", "inferno_expire"]
EVENT_PLAYER_PROPS = ["last_place_name", "X", "Y", "Z", "team_num"]
EVENT_OTHER_PROPS = ["is_warmup_period"]

T_SIDE, CT_SIDE = 2, 3

# inventory display names -> icon/engine names (the killfeed's weapon strings and
# the bundled SVG filenames). Knives are deliberately absent: everyone has one.
PRIMARY_WEAPONS = {
    # icon filenames are the engine's, so the M4A4 is "m4a1" (weapon_m4a1)
    "AK-47": "ak47", "M4A4": "m4a1", "M4A1-S": "m4a1_silencer", "FAMAS": "famas",
    "Galil AR": "galilar", "AUG": "aug", "SG 553": "sg556", "AWP": "awp",
    "SSG 08": "ssg08", "SCAR-20": "scar20", "G3SG1": "g3sg1",
    "MAC-10": "mac10", "MP9": "mp9", "MP7": "mp7", "MP5-SD": "mp5sd",
    "UMP-45": "ump45", "P90": "p90", "PP-Bizon": "bizon",
    "Nova": "nova", "XM1014": "xm1014", "Sawed-Off": "sawedoff", "MAG-7": "mag7",
    "M249": "m249", "Negev": "negev",
}
SECONDARY_WEAPONS = {
    "USP-S": "usp_silencer", "P2000": "hkp2000", "Glock-18": "glock", "P250": "p250",
    "Five-SeveN": "fiveseven", "Tec-9": "tec9", "CZ75-Auto": "cz75a",
    "Dual Berettas": "elite", "Desert Eagle": "deagle", "R8 Revolver": "revolver",
    "Zeus x27": "taser",
}
UTIL_BITS = {
    "Smoke Grenade": tokenizer.UTIL_SMOKE, "High Explosive Grenade": tokenizer.UTIL_HE,
    "Molotov": tokenizer.UTIL_MOLLY, "Incendiary Grenade": tokenizer.UTIL_MOLLY,
    "Decoy Grenade": tokenizer.UTIL_DECOY,
}
# weapon_fire weapons that get no muzzle animation (throws, knives, the bomb)
_NO_MUZZLE = {"flashbang", "smokegrenade", "hegrenade", "molotov", "incgrenade",
              "decoy", "c4"}


@dataclass
class ParsedRound:
    round_num: int
    freeze_end_tick: int
    end_tick: int
    winner: str | None
    win_reason: str | None = None
    ct_buy: str = "full"
    t_buy: str = "full"
    ct_spend: int = 0
    t_spend: int = 0
    is_pistol: int = 0
    bomb_plant_tick: int | None = None
    bomb_site: str | None = None
    plant_pos: tuple[float, float, float] | None = None
    ct_team: str | None = None
    shots: bytes | None = None      # packed (uint16 rel_tick, uint8 slot) * n, version prefix 1


@dataclass
class ParsedState:
    round_index: int
    tick: int
    round_time_s: int
    token: bytes
    bomb_planted: int
    alive_ct: int
    alive_t: int
    positions: bytes
    bomb_pos: tuple[float, float, float] | None = None


@dataclass
class ParsedKill:
    round_index: int
    tick: int
    round_time_s: int
    attacker: str | None
    attacker_side: str | None
    victim: str
    victim_side: str | None
    weapon: str | None
    headshot: int
    assister: str | None = None


@dataclass
class ParsedGrenade:
    round_index: int
    gtype: str                      # 'smoke' | 'molly' | 'flash' | 'he'
    tick: int
    round_time_s: int
    end_time_s: int | None          # None for flash/he
    x: float
    y: float
    z: float
    node_idx: int                   # uint8 node id, EMPTY_NODE off-grid
    thrower: str | None
    thrower_side: str | None
    path: bytes | None = None       # tokenizer.pack_path samples, throw -> detonation


@dataclass
class ParsedDemo:
    map_name: str
    tickrate: float
    rounds: list[ParsedRound]
    states: list[ParsedState]
    roster: list[tuple[int, str, str]]              # (slot, steamid, name)
    places: list[str] = field(default_factory=list)  # demo-local place registry, idx order
    teams: tuple[str | None, str | None] = (None, None)
    kills: list[ParsedKill] = field(default_factory=list)
    grenades: list[ParsedGrenade] = field(default_factory=list)
    label_votes: Counter = field(default_factory=Counter)   # (node_idx, place_name) -> count
    weapons: list[str] = field(default_factory=list)  # demo-local weapon registry, idx order


def extract_teams_from_df(df: pd.DataFrame) -> tuple[str | None, str | None]:
    if "team_clan_name" not in df.columns:
        return (None, None)
    names = [str(n).strip() for n in df["team_clan_name"].dropna().unique()]
    names = sorted({n for n in names if n})[:2]
    return (names[0] if names else None, names[1] if len(names) > 1 else None)


def extract_teams(path: Path) -> tuple[str | None, str | None]:
    """Lightweight team-name extraction for backfilling already-parsed demos."""
    parser = DemoParser(str(path))
    df = parser.parse_ticks(["team_clan_name", "team_num"], ticks=[10000, 40000, 80000, 120000])
    df = df[df["team_num"].isin([T_SIDE, CT_SIDE])]
    return extract_teams_from_df(df)


# raw round_end reasons: CT wins by t_killed | bomb_defused | time_ran_out,
# T wins by ct_killed | bomb_exploded (verified against IEM Cologne 2026 demos)
WIN_REASONS = {"t_killed", "bomb_defused", "time_ran_out", "ct_killed", "bomb_exploded"}


def _reason_of(value) -> str | None:
    return value if isinstance(value, str) and value in WIN_REASONS else None


def extract_win_reasons(path: Path) -> dict[int, str]:
    """round_end tick -> reason, for backfilling already-parsed demos (events-only read)."""
    parser = DemoParser(str(path))
    df = parser.parse_event("round_end")
    if df is None or "reason" not in df.columns or "tick" not in df.columns:
        return {}
    out: dict[int, str] = {}
    for _, row in df.iterrows():
        reason = _reason_of(row.get("reason"))
        if reason is not None:
            out[int(row["tick"])] = reason
    return out


def _side_of(team_num) -> str | None:
    try:
        return {CT_SIDE: "CT", T_SIDE: "T"}.get(int(team_num))
    except (TypeError, ValueError):
        return None


def _parse_all_events(parser: DemoParser) -> dict[str, pd.DataFrame]:
    """One batched pass over the demo for every event we need."""
    try:
        raw = parser.parse_events(EVENTS, player=EVENT_PLAYER_PROPS, other=EVENT_OTHER_PROPS)
        return {name: df for name, df in raw}
    except Exception:  # noqa: BLE001 - fall back to per-event parsing
        out = {}
        for ev in EVENTS:
            try:
                out[ev] = parser.parse_event(ev, player=EVENT_PLAYER_PROPS, other=EVENT_OTHER_PROPS)
            except Exception:  # noqa: BLE001
                pass
        return out


def _build_rounds(evs: dict[str, pd.DataFrame]) -> list[ParsedRound]:
    starts = evs.get("round_announce_match_start")
    live_start = int(starts["tick"].max()) if starts is not None and len(starts) else 0

    freeze = evs.get("round_freeze_end")
    freeze_ticks: list[int] = []
    if freeze is not None and len(freeze):
        warm = freeze.get("is_warmup_period")
        keep = (~warm.fillna(False)) if warm is not None else pd.Series(True, index=freeze.index)
        f = freeze[(freeze["tick"] >= live_start) & keep]
        freeze_ticks = sorted(int(t) for t in f["tick"].unique())

    ends = evs.get("round_end")
    rounds: list[ParsedRound] = []
    if ends is None or "winner" not in ends.columns:
        return rounds
    ends = ends[ends["winner"].isin(["CT", "T"]) & (ends["tick"] > live_start)].sort_values("tick")

    prev_end = live_start
    for _, row in ends.iterrows():
        end_tick = int(row["tick"])
        candidates = [t for t in freeze_ticks if prev_end <= t < end_tick]
        if not candidates:
            continue
        fe = max(candidates)
        if end_tick - fe < MIN_ROUND_SECONDS * TICKRATE:
            prev_end = end_tick
            continue
        rounds.append(ParsedRound(
            round_num=len(rounds) + 1,
            freeze_end_tick=fe,
            end_tick=end_tick,
            winner=str(row["winner"]),
            win_reason=_reason_of(row.get("reason")),
        ))
        prev_end = end_tick

    for r in rounds:
        r.is_pistol = 1 if r.round_num in (1, 13) else 0
    return rounds


def _attach_plants(plants: pd.DataFrame | None, rounds: list[ParsedRound]) -> None:
    if plants is None or len(plants) == 0:
        return
    for _, row in plants.iterrows():
        tick = int(row["tick"])
        for r in rounds:
            if r.freeze_end_tick <= tick <= r.end_tick and r.bomb_plant_tick is None:
                r.bomb_plant_tick = tick
                place = str(row.get("user_last_place_name") or "")
                if "A" in place and "Bombsite" in place:
                    r.bomb_site = "A"
                elif "B" in place and "Bombsite" in place:
                    r.bomb_site = "B"
                try:
                    r.plant_pos = (float(row["user_X"]), float(row["user_Y"]), float(row["user_Z"]))
                except (KeyError, TypeError, ValueError):
                    pass
                break


def _parse_kills(df: pd.DataFrame | None, rounds: list[ParsedRound],
                 round_sides: dict[tuple[int, str], str] | None = None) -> list[ParsedKill]:
    if df is None or len(df) == 0:
        return []
    kills: list[ParsedKill] = []
    fe = [r.freeze_end_tick for r in rounds]
    for row in df.itertuples(index=False):
        tick = int(row.tick)
        ri = int(np.searchsorted(fe, tick, side="right")) - 1
        if ri < 0 or tick > rounds[ri].end_tick:
            continue
        victim = getattr(row, "user_name", None)
        if not isinstance(victim, str) or not victim:
            continue
        attacker = getattr(row, "attacker_name", None)
        assister = getattr(row, "assister_name", None)
        # demoparser2 can expose the next round's team_num on a terminal death.
        # Prefer the side observed for that player during this round.
        sides = round_sides or {}
        kills.append(ParsedKill(
            round_index=ri,
            tick=tick,
            round_time_s=max(0, int(round((tick - fe[ri]) / TICKRATE))),
            attacker=attacker if isinstance(attacker, str) and attacker else None,
            attacker_side=sides.get((ri, attacker),
                                    _side_of(getattr(row, "attacker_team_num", None))),
            victim=victim,
            victim_side=sides.get((ri, victim),
                                  _side_of(getattr(row, "user_team_num", None))),
            weapon=str(getattr(row, "weapon", "") or "") or None,
            headshot=int(bool(getattr(row, "headshot", False))),
            assister=assister if isinstance(assister, str) and assister else None,
        ))
    return kills


def _round_player_sides(df: pd.DataFrame, rounds: list[ParsedRound]) -> dict[tuple[int, str], str]:
    """Return each player's stable side per round from sampled state data."""
    if df.empty or "name" not in df.columns or "team_num" not in df.columns:
        return {}
    fe = np.array([r.freeze_end_tick for r in rounds])
    en = np.array([r.end_tick for r in rounds])
    ticks = df["tick"].to_numpy()
    ris = np.searchsorted(fe, ticks, side="right") - 1
    valid = (ris >= 0) & (ris < len(rounds))
    valid &= ticks < en[np.clip(ris, 0, len(rounds) - 1)]
    work = df.loc[valid, ["name", "team_num"]].copy()
    work["round_index"] = ris[valid]
    out: dict[tuple[int, str], str] = {}
    for (ri, name), teams in work.groupby(["round_index", "name"])["team_num"]:
        if not isinstance(name, str) or not name:
            continue
        modes = teams.mode()
        side = _side_of(modes.iloc[0]) if len(modes) else None
        if side:
            out[(int(ri), name)] = side
    return out


def _attach_shots(evs: dict[str, pd.DataFrame], rounds: list[ParsedRound],
                  slot_of: dict[str, int]) -> None:
    """Pack per-round gun shots (rel_tick, roster slot) for muzzle animation."""
    df = evs.get("weapon_fire")
    if df is None or len(df) == 0 or "user_steamid" not in df.columns:
        return
    fe = [r.freeze_end_tick for r in rounds]
    per_round: dict[int, list[tuple[int, int]]] = {}
    for row in df.itertuples(index=False):
        wpn = str(getattr(row, "weapon", "") or "").removeprefix("weapon_")
        if wpn in _NO_MUZZLE or "knife" in wpn or "bayonet" in wpn:
            continue
        slot = slot_of.get(str(getattr(row, "user_steamid", "")))
        if slot is None:
            continue
        tick = int(row.tick)
        ri = int(np.searchsorted(fe, tick, side="right")) - 1
        if ri < 0 or tick > rounds[ri].end_tick:
            continue
        rel = tick - fe[ri]
        if 0 <= rel <= 0xFFFF:
            per_round.setdefault(ri, []).append((rel, slot))
    for ri, shots in per_round.items():
        rounds[ri].shots = b"\x01" + b"".join(
            struct.pack("<HB", rel, slot) for rel, slot in sorted(shots))


# (gtype, detonate event, expire event, fallback lifetime seconds)
_GRENADE_SPECS = [
    ("smoke", "smokegrenade_detonate", "smokegrenade_expired", SMOKE_FALLBACK_S),
    ("molly", "inferno_startburn", "inferno_expire", MOLLY_FALLBACK_S),
    ("flash", "flashbang_detonate", None, None),
    ("he", "hegrenade_detonate", None, None),
]

# projectile entity classes per grenade type (parse_grenades grenade_type column)
_PROJ_CLASSES = {
    "smoke": ("CSmokeGrenadeProjectile",),
    "molly": ("CMolotovProjectile", "CIncendiaryProjectile"),
    "flash": ("CFlashbangProjectile",),
    "he": ("CHEGrenadeProjectile",),
}


def _build_tracks(proj_df: pd.DataFrame | None) -> dict[str, list[tuple[np.ndarray, np.ndarray]]]:
    """Group projectile positions into flight tracks: gtype -> [(ticks, xyz), ...].

    Entity ids are reused across a demo, so a gap of >2s in one entity's samples
    splits it into separate throws."""
    out: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {g: [] for g in _PROJ_CLASSES}
    need = {"grenade_type", "grenade_entity_id", "x", "y", "z", "tick"}
    if proj_df is None or len(proj_df) == 0 or not need.issubset(proj_df.columns):
        return out
    cls_of = {c: g for g, classes in _PROJ_CLASSES.items() for c in classes}
    df = proj_df[proj_df["grenade_type"].isin(cls_of) & proj_df["x"].notna()]
    for (cls, _eid), g in df.groupby(["grenade_type", "grenade_entity_id"], sort=False):
        g = g.sort_values("tick")
        ticks = g["tick"].to_numpy(dtype=np.int64)
        xyz = g[["x", "y", "z"]].to_numpy(dtype=np.float32)
        splits = np.nonzero(np.diff(ticks) > 2 * TICKRATE)[0] + 1
        for tk, xy in zip(np.split(ticks, splits), np.split(xyz, splits)):
            if len(tk) >= 2:
                out[cls_of[cls]].append((tk, xy))
    return out


def _match_track(tracks: list[tuple[np.ndarray, np.ndarray]], tick: int,
                 x: float, y: float, z: float) -> tuple[np.ndarray, np.ndarray] | None:
    """Pop the track passing nearest the detonation and truncate it there.

    Matched spatially rather than by entityid (inferno entities have different ids
    than the molotov projectiles that spawn them), and against the position at the
    detonation tick rather than the track end: smoke projectile entities live on
    for the whole ~20s the cloud burns, so their tracks end long after detonation."""
    best_i, best_d, best_cut = -1, PATH_MATCH_DIST, 0
    for i, (tk, xyz) in enumerate(tracks):
        if tick < int(tk[0]) or tick > int(tk[-1]) + PATH_MATCH_TICKS:
            continue
        cut = min(int(np.searchsorted(tk, tick, side="right")), len(tk)) - 1
        if cut < 1:
            continue
        d = float(np.hypot(xyz[cut, 0] - x, xyz[cut, 1] - y))
        if d < best_d:
            best_d, best_i, best_cut = d, i, cut
    if best_i < 0:
        return None
    tk, xyz = tracks.pop(best_i)
    return tk[:best_cut + 1], xyz[:best_cut + 1]


def _pack_path(ticks: np.ndarray, xyz: np.ndarray, freeze_end_tick: int) -> bytes:
    keep = list(range(0, len(ticks) - 1, PATH_SAMPLE_TICKS)) + [len(ticks) - 1]
    return tokenizer.pack_path(
        ((float(int(ticks[i]) - freeze_end_tick) / TICKRATE,
          xyz[i, 0], xyz[i, 1], xyz[i, 2]) for i in keep))


def _parse_grenades(evs: dict[str, pd.DataFrame], rounds: list[ParsedRound],
                    map_nodes: MapNodes | None,
                    proj_df: pd.DataFrame | None = None) -> list[ParsedGrenade]:
    out: list[ParsedGrenade] = []
    fe = [r.freeze_end_tick for r in rounds]
    tracks = _build_tracks(proj_df)
    for gtype, det_ev, exp_ev, fallback_s in _GRENADE_SPECS:
        df = evs.get(det_ev)
        if df is None or len(df) == 0 or not {"x", "y", "z"}.issubset(df.columns):
            continue
        # detonate<->expire pairing by projectile/inferno entityid; missing pairs
        # (event absent in old demos, demo cut short) fall back to a fixed lifetime
        expire_of: dict[int, int] = {}
        edf = evs.get(exp_ev) if exp_ev else None
        if edf is not None and len(edf) and "entityid" in edf.columns and "entityid" in df.columns:
            for erow in edf.itertuples(index=False):
                eid = getattr(erow, "entityid", None)
                if eid is not None and not pd.isna(eid):
                    expire_of.setdefault(int(eid), int(erow.tick))
        for row in df.itertuples(index=False):
            tick = int(row.tick)
            ri = int(np.searchsorted(fe, tick, side="right")) - 1
            if ri < 0 or tick > rounds[ri].end_tick:
                continue
            if pd.isna(row.x) or pd.isna(row.y) or pd.isna(row.z):
                continue
            rt = max(0, int(round((tick - fe[ri]) / TICKRATE)))
            end_s = None
            if fallback_s is not None:
                eid = getattr(row, "entityid", None)
                end_tick = expire_of.get(int(eid)) if eid is not None and not pd.isna(eid) else None
                if end_tick is None or end_tick <= tick:
                    end_tick = tick + fallback_s * TICKRATE
                end_tick = min(end_tick, rounds[ri].end_tick)
                end_s = max(rt, int(round((end_tick - fe[ri]) / TICKRATE)))
            thrower = getattr(row, "user_name", None)
            trk = _match_track(tracks[gtype], tick, float(row.x), float(row.y), float(row.z))
            out.append(ParsedGrenade(
                round_index=ri, gtype=gtype, tick=tick, round_time_s=rt, end_time_s=end_s,
                x=float(row.x), y=float(row.y), z=float(row.z),
                node_idx=EMPTY_NODE,
                thrower=thrower if isinstance(thrower, str) and thrower else None,
                thrower_side=_side_of(getattr(row, "user_team_num", None)),
                path=_pack_path(trk[0], trk[1], fe[ri]) if trk is not None else None,
            ))
    if out and map_nodes is not None:
        xyz = np.array([[g.x, g.y, g.z] for g in out], dtype=np.float64)
        for g, n in zip(out, map_nodes.nodes_at_bulk(xyz)):
            g.node_idx = int(n)
    return out


def select_roster(df: pd.DataFrame) -> list[str]:
    """Steamids of the up-to-10 actual players, ranked by samples spent alive.

    FACEIT-style coaches occupy a team slot (team_num 2/3) for the whole match
    but are killed into spectating at round start, so raw presence cannot
    separate them from players - a coach has exactly as many rows as everyone
    else. Never-alive candidates are dropped outright so a coach can't claim a
    slot even in short rosters."""
    alive = df["is_alive"].fillna(False).astype(bool)
    per_id = pd.DataFrame({"sid": df["steamid"], "alive": alive}).groupby("sid")["alive"].agg(
        alive_rows="sum", rows="size")
    per_id = per_id[per_id["alive_rows"] > 0]
    # stable sort + steamid-sorted groupby index = deterministic tie-break
    ranked = per_id.sort_values(["alive_rows", "rows"], ascending=False, kind="mergesort")
    return sorted(ranked.index[:10])


def parse_demo(path: Path, map_nodes: MapNodes | None) -> ParsedDemo:
    parser = DemoParser(str(path))
    header = parser.parse_header()
    map_name = header.get("map_name", "unknown")

    evs = _parse_all_events(parser)
    rounds = _build_rounds(evs)
    if not rounds:
        raise ValueError("no live rounds found in demo")
    _attach_plants(evs.get("bomb_planted"), rounds)

    try:
        proj_df = parser.parse_grenades()      # projectile tracks for travel paths
    except Exception:  # noqa: BLE001 - paths are optional decoration
        proj_df = None

    # sample grid: every second of every live round + one buy-eval tick per round
    sample_ticks: list[int] = []
    buy_ticks: dict[int, int] = {}
    for i, r in enumerate(rounds):
        sample_ticks.extend(range(r.freeze_end_tick, r.end_tick, TICKRATE))
        bt = min(r.freeze_end_tick + BUY_EVAL_SECONDS * TICKRATE, r.end_tick - 1)
        buy_ticks[i] = bt
        sample_ticks.append(bt)
    sample_ticks = sorted(set(sample_ticks))

    df = parser.parse_ticks(TICK_PROPS, ticks=sample_ticks)
    df = df[df["team_num"].isin([T_SIDE, CT_SIDE])].copy()
    df["steamid"] = df["steamid"].astype(str)
    for col, default in (("armor_value", 0), ("balance", 0), ("flash_duration", 0.0)):
        df[col] = df[col].fillna(default) if col in df.columns else default
    for col in ("has_helmet", "has_defuser"):
        df[col] = df[col].fillna(False).astype(bool) if col in df.columns else False

    # roster: up to 10 real players (coaches sit on a team but are never alive)
    roster_ids = select_roster(df)
    slot_of = {sid: i for i, sid in enumerate(roster_ids)}
    names = df.groupby("steamid")["name"].last()
    roster = [(slot_of[sid], sid, str(names.get(sid, "?"))) for sid in roster_ids]

    # node assignment (vectorized over all rows)
    if map_nodes is not None:
        xyz = df[["X", "Y", "Z"]].to_numpy(dtype=np.float64)
        df["node_idx"] = map_nodes.nodes_at_bulk(xyz)
    else:
        df["node_idx"] = EMPTY_NODE

    # demo-local place registry
    place_of: dict[str, int] = {}

    def local_place_idx(name) -> int:
        if not isinstance(name, str) or not name:
            return 0xFF
        idx = place_of.get(name)
        if idx is None:
            idx = min(len(place_of), 0xFE)
            place_of[name] = idx
        return idx

    df["place_idx"] = [local_place_idx(p) for p in df["last_place_name"]]

    fe = np.array([r.freeze_end_tick for r in rounds])
    en = np.array([r.end_tick for r in rounds])

    # which clan plays CT each round (sides swap at halftime and in overtime)
    if "team_clan_name" in df.columns:
        ri_all = np.searchsorted(fe, df["tick"].to_numpy(), side="right") - 1
        for i, r in enumerate(rounds):
            sub = df.loc[(ri_all == i) & (df["team_num"] == CT_SIDE), "team_clan_name"].dropna()
            names = [s for s in (str(n).strip() for n in sub) if s]
            if names:
                r.ct_team = Counter(names).most_common(1)[0][0]

    states: list[ParsedState] = []
    label_votes: Counter = Counter()
    equipment_acc: dict[int, dict[int, int]] = {}
    last_bomb_pos: dict[int, tuple[float, float, float]] = {}

    def _has_c4(inv) -> bool:
        try:
            return any("C4" in str(item) for item in inv)   # item is named "C4 Explosive"
        except TypeError:
            return False

    # demo-local weapon registry (scanner translates to the global one on insert)
    weapon_of: dict[str, int] = {}

    def local_weapon_idx(icon_name: str) -> int:
        idx = weapon_of.get(icon_name)
        if idx is None:
            idx = min(len(weapon_of), 0xFE)
            weapon_of[icon_name] = idx
        return idx

    def classify_inventory(inv) -> tuple[int, int, int]:
        """(primary idx, secondary idx, util bitmask) from the inventory name list."""
        prim = sec = 0xFF
        util = flashes = 0
        try:
            items = list(inv)
        except TypeError:
            return prim, sec, util
        for item in items:
            name = str(item)
            if name == "Flashbang":
                flashes = min(3, flashes + 1)
            elif name in UTIL_BITS:
                util |= UTIL_BITS[name]
            elif name in PRIMARY_WEAPONS:
                prim = local_weapon_idx(PRIMARY_WEAPONS[name])
            elif name in SECONDARY_WEAPONS and (sec == 0xFF or name != "Zeus x27"):
                sec = local_weapon_idx(SECONDARY_WEAPONS[name])
        return prim, sec, util | flashes

    for tick, g in df.groupby("tick", sort=True):
        tick = int(tick)
        ri = int(np.searchsorted(fe, tick, side="right")) - 1
        if ri < 0 or tick >= en[ri]:
            continue
        if tick == buy_ticks[ri]:
            # Purchase spend omits saved weapons and can label a five-rifle
            # carry-over as an eco. Classify the loadout actually in play.
            equipment = g.groupby("team_num")["current_equip_value"].sum()
            equipment_acc[ri] = {int(k): int(v) for k, v in equipment.items()}
            if tick % TICKRATE != fe[ri] % TICKRATE:   # off-grid buy tick: not a 1 Hz sample
                continue

        slots: list[dict | None] = [None] * 10
        ct_nodes: list[int] = []
        t_nodes: list[int] = []
        alive_ct = alive_t = 0
        carrier_pos: tuple[float, float, float] | None = None
        for row in g.itertuples(index=False):
            slot = slot_of.get(row.steamid)
            if slot is None:
                continue
            is_ct = row.team_num == CT_SIDE
            alive = bool(row.is_alive)
            node = int(row.node_idx)
            if alive and _has_c4(row.inventory):
                carrier_pos = (float(row.X), float(row.Y), float(row.Z))
            prim, sec, util = classify_inventory(row.inventory)
            if row.has_helmet:
                util |= tokenizer.UTIL_HELMET
            if row.has_defuser:
                util |= tokenizer.UTIL_DEFUSER
            slots[slot] = {
                "x": float(row.X), "y": float(row.Y), "z": float(row.Z),
                "alive": alive, "ct": is_ct, "health": int(row.health or 0),
                "place_idx": int(row.place_idx), "node_idx": node,
                "yaw": None if pd.isna(row.yaw) else float(row.yaw),
                "armor": int(row.armor_value or 0),
                "money": int(row.balance or 0),
                "flash_s": float(row.flash_duration or 0.0),
                "primary": prim, "secondary": sec, "util": util,
            }
            if alive:
                if is_ct:
                    alive_ct += 1
                    if node != EMPTY_NODE and len(ct_nodes) < 5:
                        ct_nodes.append(node)
                else:
                    alive_t += 1
                    if node != EMPTY_NODE and len(t_nodes) < 5:
                        t_nodes.append(node)
                if node != EMPTY_NODE and isinstance(row.last_place_name, str) and row.last_place_name:
                    label_votes[(node, row.last_place_name)] += 1

        planted = bool(g["is_bomb_planted"].any())
        if planted and rounds[ri].plant_pos is not None:
            bomb_pos = rounds[ri].plant_pos
        elif carrier_pos is not None:
            bomb_pos = carrier_pos
            last_bomb_pos[ri] = carrier_pos
        else:
            bomb_pos = last_bomb_pos.get(ri)      # dropped: rests at last carried spot

        states.append(ParsedState(
            round_index=ri,
            tick=tick,
            round_time_s=int(round((tick - fe[ri]) / TICKRATE)),
            token=pack_token(ct_nodes, t_nodes),
            bomb_planted=int(planted),
            alive_ct=alive_ct,
            alive_t=alive_t,
            positions=pack_positions(slots),
            bomb_pos=bomb_pos,
        ))

    for ri, equipment in equipment_acc.items():
        r = rounds[ri]
        # Historical database column names are retained for schema compatibility.
        r.ct_spend = equipment.get(CT_SIDE, 0)
        r.t_spend = equipment.get(T_SIDE, 0)
        r.ct_buy = buytypes.classify(r.ct_spend)
        r.t_buy = buytypes.classify(r.t_spend)

    _attach_shots(evs, rounds, slot_of)
    places = [name for name, _ in sorted(place_of.items(), key=lambda kv: kv[1])]
    weapons = [name for name, _ in sorted(weapon_of.items(), key=lambda kv: kv[1])]
    return ParsedDemo(
        map_name=map_name,
        tickrate=float(TICKRATE),
        rounds=rounds,
        states=states,
        roster=roster,
        places=places,
        teams=extract_teams_from_df(df),
        kills=_parse_kills(evs.get("player_death"), rounds,
                           _round_player_sides(df, rounds)),
        grenades=_parse_grenades(evs, rounds, map_nodes, proj_df),
        label_votes=label_votes,
        weapons=weapons,
    )
