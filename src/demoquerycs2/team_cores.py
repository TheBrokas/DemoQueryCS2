"""Local team identities inferred from stable player IDs, without rewriting demos."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

from . import config
from .ingest.tokenizer import unpack_positions


def players(conn: sqlite3.Connection) -> list[dict]:
    """One entry per Steam ID, named by its most recent indexed recording.

    demo_date is the recording's file timestamp; parsed_at is only a fallback.
    Re-importing an older recording must not replace a newer observed username.
    """
    rows = conn.execute(
        "SELECT p.steamid, p.name, d.demo_id, COALESCE(d.demo_date, d.parsed_at) seen "
        "FROM demo_players p JOIN demos d ON d.demo_id=p.demo_id "
        "WHERE d.status='ok' ORDER BY seen DESC, d.demo_id DESC, p.slot")
    found: dict[str, dict] = {}
    seen_demos: dict[str, set[int]] = defaultdict(set)
    for r in rows:
        sid = r["steamid"]
        if not re.fullmatch(r"[0-9]{17}", sid) or int(sid) == 0:
            continue
        seen_demos[sid].add(r["demo_id"])
        if sid not in found:
            found[sid] = {"steamid": sid, "name": r["name"] or sid, "last_seen": r["seen"]}
    for sid, p in found.items():
        p["demos"] = len(seen_demos[sid])
    return sorted(found.values(), key=lambda p: (p["name"].casefold(), p["steamid"]))


def index_rounds(conn: sqlite3.Connection, demo_id: int | None = None) -> None:
    """Recover round-side membership from existing position blobs, once per round.

    Count present players even after death. A majority of sampled frames decides
    their side, avoiding a stray terminal frame that already reflects halftime.
    A tied side is ambiguous and is left unassigned. The caller owns the transaction.
    """
    rosters: dict[int, dict[int, str]] = defaultdict(dict)
    clause = " WHERE demo_id=?" if demo_id is not None else ""
    args = (demo_id,) if demo_id is not None else ()
    for r in conn.execute("SELECT demo_id, slot, steamid FROM demo_players" + clause, args):
        rosters[r["demo_id"]][r["slot"]] = r["steamid"]
    current = None
    votes: dict[str, Counter] = defaultdict(Counter)

    def flush():
        if current is None:
            return
        for sid, counts in votes.items():
            ct, t = counts["CT"], counts["T"]
            if ct != t:
                conn.execute("INSERT OR REPLACE INTO round_players VALUES (?,?,?)",
                             (current, sid, "CT" if ct > t else "T"))
        conn.execute("INSERT OR IGNORE INTO round_rosters_indexed VALUES (?)", (current,))

    rows = conn.execute(
        "SELECT s.round_id, s.demo_id, s.positions FROM states s "
        "WHERE NOT EXISTS (SELECT 1 FROM round_rosters_indexed x WHERE x.round_id=s.round_id) "
        + ("AND s.round_id IN (SELECT round_id FROM rounds WHERE demo_id=?) " if demo_id is not None else "")
        + "ORDER BY s.round_id, s.tick", args)
    for r in rows:
        if current != r["round_id"]:
            flush()
            current = r["round_id"]
            votes = defaultdict(Counter)
        roster = rosters[r["demo_id"]]
        for slot, p in enumerate(unpack_positions(r["positions"])):
            sid = roster.get(slot)
            if sid and p is not None:
                votes[sid]["CT" if p["ct"] else "T"] += 1
    flush()


def matches(conn: sqlite3.Connection, map_name: str | None = None) -> dict[tuple[int, str], list[dict]]:
    """All cores whose complete membership plays on one side of a round."""
    if config.DEMO_MODE:
        return {}
    # Also supports pre-feature, read-only snapshots used by playback utilities.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='team_cores'").fetchone():
        return {}
    rows = conn.execute(
        "SELECT rp.round_id, rp.side, c.core_id, c.name FROM round_players rp "
        "JOIN team_core_players cp ON cp.steamid=rp.steamid "
        "JOIN team_cores c ON c.core_id=cp.core_id "
        "JOIN rounds r ON r.round_id=rp.round_id JOIN demos d ON d.demo_id=r.demo_id "
        "WHERE d.status='ok' " + ("AND d.map_name=? " if map_name is not None else "") +
        "GROUP BY rp.round_id, rp.side, c.core_id "
        "HAVING COUNT(*)=(SELECT COUNT(*) FROM team_core_players p WHERE p.core_id=c.core_id)",
        (map_name,) if map_name is not None else ())
    out: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rows:
        out[(r["round_id"], r["side"])].append({"core_id": r["core_id"], "name": r["name"]})
    return out


def overrides(conn: sqlite3.Connection, map_name: str | None = None) -> dict[tuple[int, str], str]:
    # Never pick an arbitrary winner when overlapping cores match the same side.
    return {key: found[0]["name"] for key, found in matches(conn, map_name).items() if len(found) == 1}


def names(row, resolved: dict[tuple[int, str], str]) -> tuple[str | None, str | None]:
    ct = row["ct_team"]
    t1, t2 = row["team1"], row["team2"]
    t = t1 if ct and ct == t2 else t2 if ct and ct == t1 else None
    return (resolved.get((row["round_id"], "CT"), ct),
            resolved.get((row["round_id"], "T"), t))


def list_cores(conn: sqlite3.Connection) -> list[dict]:
    counts, conflicts = Counter(), Counter()
    for found in matches(conn).values():
        target = counts if len(found) == 1 else conflicts
        for c in found:
            target[c["core_id"]] += 1
    members: dict[int, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT core_id, steamid FROM team_core_players ORDER BY steamid"):
        members[r["core_id"]].append(r["steamid"])
    return [{"core_id": r["core_id"], "name": r["name"], "steamids": members[r["core_id"]],
             "rounds": counts[r["core_id"]], "conflicts": conflicts[r["core_id"]]}
            for r in conn.execute("SELECT * FROM team_cores ORDER BY name COLLATE NOCASE")]


def save(conn: sqlite3.Connection, name: str, steamids: list[str], core_id: int | None = None) -> int:
    """Validate a core; transaction/commit and cache invalidation belong to the caller."""
    name = " ".join(name.split())
    if not name or len(name) > 60:
        raise ValueError("Enter a team name between 1 and 60 characters.")
    if not 3 <= len(steamids) <= 5 or len(set(steamids)) != len(steamids):
        raise ValueError("Select 3 to 5 different players. Every selected player must match.")
    if any(not re.fullmatch(r"[0-9]{17}", sid) for sid in steamids):
        raise ValueError("Select players from your indexed demos.")
    old = set()
    if core_id is not None:
        if not conn.execute("SELECT 1 FROM team_cores WHERE core_id=?", (core_id,)).fetchone():
            raise ValueError("This team core no longer exists. Reload Settings.")
        old = {r[0] for r in conn.execute("SELECT steamid FROM team_core_players WHERE core_id=?", (core_id,))}
    known = {p["steamid"] for p in players(conn)} | old
    if not set(steamids) <= known:
        raise ValueError("Some selected players are no longer indexed. Reload Settings.")
    if conn.execute("SELECT 1 FROM team_cores WHERE name=? COLLATE NOCASE AND core_id<>?",
                    (name, core_id or -1)).fetchone():
        raise ValueError("A team core with this name already exists.")
    if core_id is None:
        core_id = conn.execute("INSERT INTO team_cores(name) VALUES (?)", (name,)).lastrowid
    else:
        conn.execute("UPDATE team_cores SET name=? WHERE core_id=?", (name, core_id))
        conn.execute("DELETE FROM team_core_players WHERE core_id=?", (core_id,))
    conn.executemany("INSERT INTO team_core_players VALUES (?,?)", [(core_id, sid) for sid in steamids])
    index_rounds(conn)
    return core_id
