"""SQLite connection + schema."""
from __future__ import annotations

import sqlite3
import re
from collections import defaultdict
from pathlib import Path

from . import config

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS archives (
  content_key  TEXT PRIMARY KEY,
  filename     TEXT NOT NULL,
  extracted_at TEXT NOT NULL,
  n_files      INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'ok',
  error_msg    TEXT
);

CREATE TABLE IF NOT EXISTS demos (
  demo_id     INTEGER PRIMARY KEY,
  filename    TEXT NOT NULL,
  file_size   INTEGER NOT NULL,
  content_key TEXT NOT NULL UNIQUE,
  map_name    TEXT NOT NULL,
  tickrate    REAL NOT NULL,
  demo_date   TEXT,
  parsed_at   TEXT NOT NULL,
  tokenizer_version INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'ok',
  error_msg   TEXT
);

CREATE TABLE IF NOT EXISTS places (
  map_name   TEXT NOT NULL,
  place_idx  INTEGER NOT NULL,
  place_name TEXT NOT NULL,
  PRIMARY KEY (map_name, place_idx),
  UNIQUE (map_name, place_name)
);

CREATE TABLE IF NOT EXISTS weapons (
  weapon_idx INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS node_labels (
  map_name  TEXT NOT NULL,
  node_idx  INTEGER NOT NULL,
  place_name TEXT NOT NULL,
  votes     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (map_name, node_idx, place_name)
);

CREATE TABLE IF NOT EXISTS rounds (
  round_id        INTEGER PRIMARY KEY,
  demo_id         INTEGER NOT NULL REFERENCES demos(demo_id) ON DELETE CASCADE,
  round_num       INTEGER NOT NULL,
  freeze_end_tick INTEGER NOT NULL,
  end_tick        INTEGER NOT NULL,
  winner          TEXT,
  ct_buy          TEXT NOT NULL,
  t_buy           TEXT NOT NULL,
  ct_spend        INTEGER NOT NULL DEFAULT 0,
  t_spend         INTEGER NOT NULL DEFAULT 0,
  is_pistol       INTEGER NOT NULL DEFAULT 0,
  bomb_plant_tick INTEGER,
  bomb_site       TEXT,
  ct_team         TEXT,
  win_reason      TEXT,
  shots           BLOB,
  UNIQUE (demo_id, round_num)
);

CREATE TABLE IF NOT EXISTS states (
  state_id     INTEGER PRIMARY KEY,
  round_id     INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
  demo_id      INTEGER NOT NULL REFERENCES demos(demo_id) ON DELETE CASCADE,
  map_name     TEXT NOT NULL,
  tick         INTEGER NOT NULL,
  round_time_s INTEGER NOT NULL,
  token        BLOB NOT NULL,
  bomb_planted INTEGER NOT NULL,
  alive_ct     INTEGER NOT NULL,
  alive_t      INTEGER NOT NULL,
  positions    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_states_round ON states(round_id, tick);
CREATE INDEX IF NOT EXISTS idx_states_map ON states(map_name);

CREATE TABLE IF NOT EXISTS kills (
  kill_id       INTEGER PRIMARY KEY,
  round_id      INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
  demo_id       INTEGER NOT NULL REFERENCES demos(demo_id) ON DELETE CASCADE,
  tick          INTEGER NOT NULL,
  round_time_s  INTEGER NOT NULL,
  attacker      TEXT,
  attacker_side TEXT,
  victim        TEXT NOT NULL,
  victim_side   TEXT,
  weapon        TEXT,
  headshot      INTEGER NOT NULL DEFAULT 0,
  assister      TEXT
);
CREATE INDEX IF NOT EXISTS idx_kills_round ON kills(round_id, tick);

CREATE TABLE IF NOT EXISTS grenades (
  grenade_id   INTEGER PRIMARY KEY,
  round_id     INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
  demo_id      INTEGER NOT NULL REFERENCES demos(demo_id) ON DELETE CASCADE,
  type         TEXT NOT NULL,              -- 'smoke' | 'molly' | 'flash' | 'he'
  tick         INTEGER NOT NULL,
  round_time_s INTEGER NOT NULL,
  end_time_s   INTEGER,                    -- NULL for flash/he
  x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
  node_idx     INTEGER NOT NULL DEFAULT 255,
  thrower      TEXT,
  thrower_side TEXT,
  path         BLOB                        -- tokenizer.pack_path: (t_cs, x, y, z) int16 samples
);
CREATE INDEX IF NOT EXISTS idx_grenades_round ON grenades(round_id, tick);

CREATE TABLE IF NOT EXISTS demo_players (
  demo_id INTEGER NOT NULL REFERENCES demos(demo_id) ON DELETE CASCADE,
  slot    INTEGER NOT NULL,
  steamid TEXT NOT NULL,
  name    TEXT NOT NULL,
  PRIMARY KEY (demo_id, slot)
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    # Spatial matching reads every token for a map into NumPy; no query looks
    # up a state by token in SQLite.  The compound index was therefore a large
    # write/storage cost without a reader.  Keep the token column and the
    # standalone map index, which also preserves state_id order for map loads.
    conn.execute("DROP INDEX IF EXISTS idx_states_map_token")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(demos)")}
    if "team1" not in cols:
        conn.execute("ALTER TABLE demos ADD COLUMN team1 TEXT")
        conn.execute("ALTER TABLE demos ADD COLUMN team2 TEXT")
        conn.commit()
    if "event" not in cols:
        # optional match metadata (assets/matchmeta.json overlay, applied by
        # the scanner): tournament short name and external match id; NULL
        # until a mapping for the filename exists
        conn.execute("ALTER TABLE demos ADD COLUMN event TEXT")
        conn.execute("ALTER TABLE demos ADD COLUMN hltv_id INTEGER")
        conn.commit()
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(rounds)")}
    if "ct_team" not in rcols:
        conn.execute("ALTER TABLE rounds ADD COLUMN ct_team TEXT")
        conn.commit()
    if "win_reason" not in rcols:
        # raw round_end reason: t_killed | bomb_defused | time_ran_out (CT wins),
        # ct_killed | bomb_exploded (T wins); 'unknown' = unrecoverable, NULL = backfill pending
        conn.execute("ALTER TABLE rounds ADD COLUMN win_reason TEXT")
        conn.commit()
    if "shots" not in rcols:
        conn.execute("ALTER TABLE rounds ADD COLUMN shots BLOB")
        conn.commit()
    kcols = {r[1] for r in conn.execute("PRAGMA table_info(kills)")}
    if "assister" not in kcols:
        conn.execute("ALTER TABLE kills ADD COLUMN assister TEXT")
        conn.commit()
    scols = {r[1] for r in conn.execute("PRAGMA table_info(states)")}
    if "bomb_x" not in scols:
        conn.execute("ALTER TABLE states ADD COLUMN bomb_x REAL")
        conn.execute("ALTER TABLE states ADD COLUMN bomb_y REAL")
        conn.execute("ALTER TABLE states ADD COLUMN bomb_z REAL")
        conn.commit()
    gcols = {r[1] for r in conn.execute("PRAGMA table_info(grenades)")}
    if gcols and "path" not in gcols:
        conn.execute("ALTER TABLE grenades ADD COLUMN path BLOB")
        conn.commit()
    pistol_fix = conn.execute(
        "SELECT value FROM meta WHERE key='split_pistols_v2'").fetchone()
    if pistol_fix is None:
        normalize_split_pistols(conn)
        conn.execute("INSERT INTO meta (key, value) VALUES ('split_pistols_v2', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def normalize_split_pistols(conn: sqlite3.Connection) -> set[str]:
    """Correct pistol flags using map-global rounds across ``-pN`` files.

    Each parser process only sees its recording part, so its local rounds 1/13
    are not necessarily the match's pistol rounds. This repair is deterministic
    regardless of the order parallel parser workers finished.
    """
    groups: dict[tuple[str, str, str | None], list[tuple[int, int, str]]] = defaultdict(list)
    for row in conn.execute(
            "SELECT demo_id, filename, map_name, substr(demo_date, 1, 10) day "
            "FROM demos WHERE status='ok'"):
        match = re.match(r"^(.*)-p(\d+)\.dem$", row["filename"], re.IGNORECASE)
        if match:
            groups[(match.group(1).lower(), row["map_name"], row["day"])].append(
                (int(match.group(2)), row["demo_id"], row["map_name"]))

    changed_maps: set[str] = set()
    for parts in groups.values():
        global_round = 0
        for _, demo_id, map_name in sorted(parts):
            for rr in conn.execute(
                    "SELECT round_id, is_pistol FROM rounds WHERE demo_id=? ORDER BY round_num",
                    (demo_id,)).fetchall():
                global_round += 1
                want = int(global_round in (1, 13))
                if rr["is_pistol"] != want:
                    conn.execute("UPDATE rounds SET is_pistol=? WHERE round_id=?",
                                 (want, rr["round_id"]))
                    changed_maps.add(map_name)
    return changed_maps
