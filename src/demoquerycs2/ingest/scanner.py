"""Demo folder scanning: dedupe, parse, DB insert, progress state.

The demos folder holds .dem files only - there is no archive extraction. Users
decompress any .zip/.rar/.zst themselves and drop the .dem in; the scan finds
.dem files recursively so existing subfolders still work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from typing import TYPE_CHECKING

from .. import config, mapdata
from . import tokenizer

if TYPE_CHECKING:
    from .demo_parser import ParsedDemo

# demo_parser (and through it pandas, ~55MB RSS) is imported lazily inside the
# functions that parse: the hosted demo never scans, so its server must not pay
# the import at boot. Worker processes import it on first parse anyway.

# demoparser2's Rust core raises pyo3_runtime.PanicException on malformed demos.
# It subclasses BaseException (not Exception) and its module isn't importable, so
# we catch BaseException in the parse path and re-raise only genuine interrupts -
# otherwise one bad demo aborts the whole scan instead of being logged and skipped.
_FATAL = (KeyboardInterrupt, SystemExit)


def default_workers() -> int:
    env = int(os.environ.get("CS2SF_WORKERS", "0") or 0)
    if env > 0:
        return env
    return max(1, min((os.cpu_count() or 4) - 3, 8))

FILENAME_TEAMS_RE = re.compile(r"^(.+?)-vs-(.+?)-m\d", re.IGNORECASE)


def teams_from_filename(name: str) -> tuple[str | None, str | None]:
    m = FILENAME_TEAMS_RE.match(name)
    if not m:
        return (None, None)
    return (m.group(1).replace("-", " "), m.group(2).replace("-", " "))


@dataclass
class ScanStatus:
    running: bool = False
    phase: str = "idle"
    current_file: str = ""
    files_done: int = 0
    files_total: int = 0
    files_skipped: int = 0
    states_written: int = 0
    errors: list[dict] = field(default_factory=list)
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {**self.__dict__}


STATUS = ScanStatus()
_lock = threading.Lock()
_on_maps_changed = []           # callbacks(map_names: set[str])


def on_maps_changed(cb) -> None:
    _on_maps_changed.append(cb)


def content_key(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(4 * 1024 * 1024))
    return f"{h.hexdigest()}:{path.stat().st_size}"


def _get_place_idx_factory(conn: sqlite3.Connection, map_name: str):
    cache: dict[str, int] = {
        row["place_name"]: row["place_idx"]
        for row in conn.execute("SELECT place_idx, place_name FROM places WHERE map_name=?", (map_name,))
    }

    def get(place: str) -> int:
        if place in cache:
            return cache[place]
        idx = len(cache)
        if idx >= 0xFF:
            return 0xFE
        conn.execute("INSERT OR IGNORE INTO places (map_name, place_idx, place_name) VALUES (?,?,?)",
                     (map_name, idx, place))
        cache[place] = idx
        return idx

    return get


def _parse_worker(path_str: str) -> ParsedDemo:
    """Pure parse step - no DB access, runs in worker processes."""
    from demoparser2 import DemoParser

    from .demo_parser import parse_demo
    map_name = DemoParser(path_str).parse_header().get("map_name", "unknown")
    nodes = mapdata.get_nodes(map_name)
    if nodes is None:
        raise ValueError(f"no node artifacts for map '{map_name}' (not bundled)")
    return parse_demo(Path(path_str), nodes)


def _reason_worker(path_str: str) -> dict[int, str]:
    """Events-only read for the win-reason backfill, runs in worker processes."""
    from .demo_parser import extract_win_reasons
    return extract_win_reasons(Path(path_str))


def _find_demo_files() -> list[Path]:
    # rglob("*.dem") also matches directories (e.g. an "X.dem/" folder left by an
    # old extraction) - keep only real files so content_key never opens a folder
    return sorted({p for p in config.DEMOS_DIR.rglob("*.dem") if p.is_file()})


def backfill_win_reasons(conn: sqlite3.Connection, demo_files: list[Path] | None = None) -> None:
    """Fill rounds.win_reason for demos parsed before reasons were stored.

    Events-only demo read; rounds pair with round_end events by end_tick.
    Rounds the demo genuinely cannot yield settle as terminal 'unknown';
    demos whose file is missing or unreadable right now (unplugged drive,
    AV lock, transient error) stay NULL and simply retry on the next scan."""
    need = conn.execute(
        "SELECT DISTINCT d.demo_id, d.filename, d.file_size, d.content_key FROM demos d "
        "JOIN rounds r ON r.demo_id = d.demo_id "
        "WHERE d.status = 'ok' AND r.win_reason IS NULL ORDER BY d.demo_id").fetchall()
    if not need:
        return
    if demo_files is None:
        demo_files = _find_demo_files()
    by_name: dict[str, list[Path]] = {}
    for f in demo_files:
        by_name.setdefault(f.name, []).append(f)

    def resolve(row: sqlite3.Row) -> Path | None:
        """The indexed demo's own file: basename + size must match (basenames can
        collide across subfolders); same-size ties break on the content hash."""
        try:
            candidates = [f for f in by_name.get(row["filename"], [])
                          if f.stat().st_size == row["file_size"]]
            if len(candidates) > 1:
                candidates = [f for f in candidates if content_key(f) == row["content_key"]]
        except OSError:
            return None
        return candidates[0] if candidates else None

    def apply(demo_id: int, reasons: dict[int, str]) -> None:
        # reached only after a successful events read: rounds the events could
        # not name settle as terminal 'unknown' (a re-read cannot yield them)
        conn.executemany(
            "UPDATE rounds SET win_reason = ? WHERE demo_id = ? AND end_tick = ?",
            [(reason, demo_id, tick) for tick, reason in reasons.items()])
        conn.execute(
            "UPDATE rounds SET win_reason = 'unknown' WHERE demo_id = ? AND win_reason IS NULL",
            (demo_id,))
        conn.commit()

    todo = [(row["demo_id"], f) for row in need if (f := resolve(row)) is not None]
    done = 0

    def handle(did: int, f: Path, get_result) -> None:
        nonlocal done
        try:
            apply(did, get_result())
        except _FATAL:
            raise
        except BaseException:  # noqa: BLE001 - incl. pyo3 PanicException; the demo
            pass               # parsed fine once, so treat as transient: retry next scan
        done += 1
        STATUS.phase = f"updating round win reasons ({done}/{len(todo)})"
        STATUS.current_file = f.name

    workers = min(default_workers(), max(1, len(todo)))
    if workers <= 1:
        for did, f in todo:
            handle(did, f, lambda f=f: _reason_worker(str(f)))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_reason_worker, str(f)): (did, f) for did, f in todo}
            for fut in as_completed(futs):
                did, f = futs[fut]
                handle(did, f, fut.result)


def _get_weapon_idx_factory(conn: sqlite3.Connection):
    cache: dict[str, int] = {
        row["name"]: row["weapon_idx"]
        for row in conn.execute("SELECT weapon_idx, name FROM weapons")
    }

    def get(name: str) -> int:
        if name in cache:
            return cache[name]
        idx = len(cache)
        if idx >= 0xFF:
            return 0xFE
        conn.execute("INSERT OR IGNORE INTO weapons (weapon_idx, name) VALUES (?,?)", (idx, name))
        cache[name] = idx
        return idx

    return get


def compact_grenade_paths(conn: sqlite3.Connection, chunk: int = 20000) -> int:
    """Rewrite float32 travel paths as int16 samples (half the bytes).

    Pure blob transform - no demo is re-read. Runs once: a meta flag records
    the format so later scans skip the table scan entirely."""
    row = conn.execute("SELECT value FROM meta WHERE key='path_format'").fetchone()
    if row and int(row["value"]) >= tokenizer.PATH_V2:
        return 0
    done = 0
    while True:
        rows = conn.execute(
            "SELECT grenade_id, path FROM grenades WHERE path IS NOT NULL "
            "AND LENGTH(path) % 2 = 0 LIMIT ?", (chunk,)).fetchall()   # v1 blobs are even-length
        if not rows:
            break
        conn.executemany(
            "UPDATE grenades SET path = ? WHERE grenade_id = ?",
            [(tokenizer.pack_path(tokenizer.unpack_path(r["path"])), r["grenade_id"])
             for r in rows])
        conn.commit()
        done += len(rows)
        STATUS.phase = f"compacting grenade paths ({done:,})"
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('path_format', ?)",
                 (str(tokenizer.PATH_V2),))
    conn.commit()
    if done:
        # the rewrite frees ~half of the path pages; hand them back to the OS
        # (once, right after the conversion - not on every scan)
        STATUS.phase = "reclaiming disk space"
        conn.execute("VACUUM")
        conn.commit()
    return done


def _insert_demo(conn: sqlite3.Connection, path: Path, key: str, parsed: ParsedDemo) -> int:
    """Insert a parsed demo, translating demo-local place and weapon indices to
    the global registries."""
    get_place_idx = _get_place_idx_factory(conn, parsed.map_name)
    lut = np.arange(256, dtype=np.uint8)
    dirty = False
    for local_idx, name in enumerate(parsed.places[:0xFE]):
        g = get_place_idx(name)
        if g != local_idx:
            dirty = True
        lut[local_idx] = g
    get_weapon_idx = _get_weapon_idx_factory(conn)
    wlut = np.arange(256, dtype=np.uint8)
    wdirty = False
    for local_idx, name in enumerate(parsed.weapons[:0xFE]):
        g = get_weapon_idx(name)
        if g != local_idx:
            wdirty = True
        wlut[local_idx] = g
    if dirty or wdirty:
        step = tokenizer.SLOT_SIZE
        off = tokenizer.PLACE_BYTE_OFFSET
        for s in parsed.states:
            arr = np.frombuffer(s.positions, dtype=np.uint8).copy()
            if dirty:
                arr[off::step] = lut[arr[off::step]]
            if wdirty:
                for woff in tokenizer.WEAPON_BYTE_OFFSETS:
                    arr[woff::step] = wlut[arr[woff::step]]
            s.positions = arr.tobytes()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    team1, team2 = parsed.teams
    if team1 is None:
        team1, team2 = teams_from_filename(path.name)
    cur = conn.execute(
        "INSERT INTO demos (filename, file_size, content_key, map_name, tickrate, demo_date, parsed_at, "
        "tokenizer_version, team1, team2) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path.name, path.stat().st_size, key, parsed.map_name, parsed.tickrate, mtime, now,
         config.TOKENIZER_VERSION, team1, team2))
    demo_id = cur.lastrowid

    round_ids: list[int] = []
    for r in parsed.rounds:
        cur = conn.execute(
            "INSERT INTO rounds (demo_id, round_num, freeze_end_tick, end_tick, winner, ct_buy, t_buy, "
            "ct_spend, t_spend, is_pistol, bomb_plant_tick, bomb_site, ct_team, win_reason, shots) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (demo_id, r.round_num, r.freeze_end_tick, r.end_tick, r.winner, r.ct_buy, r.t_buy,
             r.ct_spend, r.t_spend, r.is_pistol, r.bomb_plant_tick, r.bomb_site, r.ct_team,
             r.win_reason or "unknown", r.shots))
        round_ids.append(cur.lastrowid)

    conn.executemany(
        "INSERT INTO states (round_id, demo_id, map_name, tick, round_time_s, token, bomb_planted, "
        "alive_ct, alive_t, positions, bomb_x, bomb_y, bomb_z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(round_ids[s.round_index], demo_id, parsed.map_name, s.tick, s.round_time_s, s.token,
          s.bomb_planted, s.alive_ct, s.alive_t, s.positions,
          *(s.bomb_pos if s.bomb_pos else (None, None, None))) for s in parsed.states])

    conn.executemany("INSERT INTO demo_players (demo_id, slot, steamid, name) VALUES (?,?,?,?)",
                     [(demo_id, slot, sid, name) for slot, sid, name in parsed.roster])

    conn.executemany(
        "INSERT INTO kills (round_id, demo_id, tick, round_time_s, attacker, attacker_side, "
        "victim, victim_side, weapon, headshot, assister) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(round_ids[k.round_index], demo_id, k.tick, k.round_time_s, k.attacker, k.attacker_side,
          k.victim, k.victim_side, k.weapon, k.headshot, k.assister) for k in parsed.kills])

    conn.executemany(
        "INSERT INTO grenades (round_id, demo_id, type, tick, round_time_s, end_time_s, "
        "x, y, z, node_idx, thrower, thrower_side, path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(round_ids[g.round_index], demo_id, g.gtype, g.tick, g.round_time_s, g.end_time_s,
          g.x, g.y, g.z, g.node_idx, g.thrower, g.thrower_side, g.path) for g in parsed.grenades])

    conn.executemany(
        "INSERT INTO node_labels (map_name, node_idx, place_name, votes) VALUES (?,?,?,?) "
        "ON CONFLICT(map_name, node_idx, place_name) DO UPDATE SET votes = votes + excluded.votes",
        [(parsed.map_name, node, place, votes) for (node, place), votes in parsed.label_votes.items()])

    return len(parsed.states)


def apply_match_meta(conn: sqlite3.Connection) -> int:
    """Stamp event names + match ids from the optional assets/matchmeta.json
    overlay onto demos missing them. Pure DB update keyed by filename; demos
    absent from the mapping stay NULL and retry next scan."""
    path = config.PKG_DIR / "assets" / "matchmeta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for row in conn.execute(
            "SELECT demo_id, filename FROM demos "
            "WHERE status='ok' AND (event IS NULL OR hltv_id IS NULL)").fetchall():
        e = data.get(row["filename"])
        if not e:
            continue
        conn.execute("UPDATE demos SET event=?, hltv_id=? WHERE demo_id=?",
                     (e.get("event"), e.get("hltv_id"), row["demo_id"]))
        n += 1
    if n:
        conn.commit()
    return n


def run_scan(db_path: Path | None = None) -> None:
    """Blocking scan (call in a thread). Updates STATUS as it goes."""
    from .. import db as dbmod

    with _lock:
        if STATUS.running:
            return
        STATUS.__init__()  # reset
        STATUS.running = True

    changed_maps: set[str] = set()
    conn = dbmod.connect(db_path)
    try:
        config.ensure_dirs()
        STATUS.phase = "scanning demos folder"
        demo_files = _find_demo_files()      # walked once, shared by every phase

        # ---- phase 1: backfill team names for demos parsed before team support ----
        need_teams = conn.execute(
            "SELECT demo_id, filename FROM demos WHERE team1 IS NULL AND status='ok'").fetchall()
        if need_teams:
            from .demo_parser import extract_teams
            STATUS.phase = "backfilling team names"
            by_name = {f.name: f for f in demo_files}
            for row in need_teams:
                STATUS.current_file = row["filename"]
                t1 = t2 = None
                f = by_name.get(row["filename"])
                if f is not None:
                    try:
                        t1, t2 = extract_teams(f)
                    except Exception:  # noqa: BLE001
                        pass
                if t1 is None:
                    t1, t2 = teams_from_filename(row["filename"])
                conn.execute("UPDATE demos SET team1=?, team2=? WHERE demo_id=?",
                             (t1 or "unknown", t2, row["demo_id"]))
                conn.commit()

        # ---- phase 2: drop demos parsed with an older tokenizer so they reparse ----
        stale = conn.execute("SELECT demo_id, filename FROM demos WHERE tokenizer_version < ?",
                             (config.TOKENIZER_VERSION,)).fetchall()
        if stale:
            STATUS.phase = f"upgrading index ({len(stale)} demos to reparse)"
            for row in stale:
                conn.execute("DELETE FROM demos WHERE demo_id=?", (row["demo_id"],))
            conn.commit()

        # ---- phase 2b: backfill win reasons for demos parsed before they were stored ----
        # (after the stale purge, so soon-to-reparse demos aren't read twice)
        STATUS.phase = "updating round win reasons"
        backfill_win_reasons(conn, demo_files)

        # ---- phase 2c: shrink pre-v2 grenade paths in place (no demo re-read) ----
        compact_grenade_paths(conn)

        # ---- phase 3: parse .dem files ----
        STATUS.phase = "checking for new demos"
        known_demos = {row["content_key"] for row in conn.execute("SELECT content_key FROM demos")}
        todo = []
        for f in demo_files:
            key = content_key(f)
            if key not in known_demos:
                todo.append((f, key))
                known_demos.add(key)
        STATUS.files_total = len(todo)
        STATUS.files_skipped = len(demo_files) - len(todo)
        workers = min(default_workers(), max(1, len(todo)))
        STATUS.phase = f"parsing ({workers} worker{'s' if workers > 1 else ''})"

        def handle(f: Path, key: str, parsed: ParsedDemo | None, exc: BaseException | None) -> None:
            STATUS.current_file = f.name
            try:
                if exc is not None:
                    raise exc
                n = _insert_demo(conn, f, key, parsed)
                conn.commit()
                STATUS.states_written += n
                changed_maps.add(parsed.map_name)
            except _FATAL:
                raise
            except BaseException as e:  # noqa: BLE001 - incl. pyo3 PanicException
                conn.rollback()
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR IGNORE INTO demos (filename, file_size, content_key, map_name, tickrate, "
                    "parsed_at, tokenizer_version, status, error_msg) VALUES (?,?,?,?,?,?,?,'error',?)",
                    (f.name, f.stat().st_size, key, "unknown", 64.0, now, config.TOKENIZER_VERSION,
                     f"{e}\n{traceback.format_exc()[-400:]}"))
                conn.commit()
                STATUS.errors.append({"file": f.name, "msg": str(e)[:300]})
            STATUS.files_done += 1

        if workers <= 1:
            for f, key in todo:
                STATUS.current_file = f.name
                try:
                    parsed = _parse_worker(str(f))
                    handle(f, key, parsed, None)
                except _FATAL:
                    raise
                except BaseException as e:  # noqa: BLE001 - incl. pyo3 PanicException
                    handle(f, key, None, e)
        else:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_parse_worker, str(f)): (f, key) for f, key in todo}
                for fut in as_completed(futs):
                    f, key = futs[fut]
                    try:
                        parsed = fut.result()
                        handle(f, key, parsed, None)
                    except _FATAL:
                        raise
                    except BaseException as e:  # noqa: BLE001 - incl. pyo3 PanicException
                        handle(f, key, None, e)

        # Parser-local numbering restarts in -p2/-p3 recording files. Repair
        # pistol flags only after every parallel result has been inserted.
        changed_maps.update(dbmod.normalize_split_pistols(conn))
        conn.commit()

        # ---- phase 4: stamp event metadata (covers freshly parsed demos too) ----
        STATUS.phase = "applying event metadata"
        apply_match_meta(conn)

        STATUS.phase = "done"
    finally:
        conn.close()
        STATUS.running = False
        STATUS.current_file = ""
        STATUS.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for cb in _on_maps_changed:
            try:
                cb(changed_maps)
            except Exception:  # noqa: BLE001
                pass


def start_scan_thread(db_path: Path | None = None) -> bool:
    with _lock:
        if STATUS.running:
            return False
    t = threading.Thread(target=run_scan, args=(db_path,), daemon=True, name="cs2sf-scan")
    t.start()
    return True
