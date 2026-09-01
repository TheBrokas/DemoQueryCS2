"""Playback response repairs for legacy indexes."""
import sqlite3

from demoquerycs2.db import normalize_split_pistols
from demoquerycs2.web.app import _match_context, _normalize_kill_sides, _recording_parts


def test_normalize_terminal_kill_sides_from_round_frames():
    frames = [
        {"players": [{"name": "alpha", "side": "CT"},
                     {"name": "bravo", "side": "T"}]},
        {"players": [{"name": "alpha", "side": "CT"},
                     {"name": "bravo", "side": "T"}]},
    ]
    kills = [{"attacker": "alpha", "attacker_side": "T",
              "victim": "bravo", "victim_side": "CT"}]

    _normalize_kill_sides(frames, kills)

    assert kills[0]["attacker_side"] == "CT"
    assert kills[0]["victim_side"] == "T"


def test_normalize_preserves_event_side_for_unobserved_player():
    kills = [{"attacker": None, "attacker_side": None,
              "victim": "unknown", "victim_side": "T"}]

    _normalize_kill_sides([{"players": []}], kills)

    assert kills[0]["attacker_side"] is None
    assert kills[0]["victim_side"] == "T"


def test_restart_parts_form_one_round_navigation_and_score():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE demos (demo_id INTEGER, filename TEXT, map_name TEXT,
                            team1 TEXT, team2 TEXT, demo_date TEXT);
        CREATE TABLE rounds (round_id INTEGER, demo_id INTEGER, round_num INTEGER,
                             winner TEXT, ct_team TEXT);
        INSERT INTO demos VALUES
          (10, 'falcons-vs-vitality-m1-anubis-p1.dem', 'de_anubis', 'Falcons', 'Vitality', '2026-01-02'),
          (11, 'falcons-vs-vitality-m1-anubis-p2.dem', 'de_anubis', 'Falcons', 'Vitality', '2026-01-02');
        INSERT INTO rounds VALUES
          (101, 10, 1, 'CT', 'Falcons'), (102, 10, 2, 'T', 'Falcons'),
          (201, 11, 1, 'T', 'Falcons'), (202, 11, 2, 'CT', 'Vitality');
    """)
    current = conn.execute("""
        SELECT r.*, d.filename, d.map_name mn, d.team1, d.team2, d.demo_date
        FROM rounds r JOIN demos d ON d.demo_id=r.demo_id WHERE r.round_id=201
    """).fetchone()

    assert _recording_parts(conn, current) == [10, 11]
    context = _match_context(conn, current)
    assert context["round_num"] == 3
    assert (context["ct_score"], context["t_score"]) == (1, 1)


def test_restart_navigation_survives_metadata_drift():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE demos (demo_id INTEGER, filename TEXT, map_name TEXT,
                            team1 TEXT, team2 TEXT, demo_date TEXT);
        INSERT INTO demos VALUES
          (1, 'alpha-vs-beta-m1-nuke-p1.dem', 'de_nuke', 'Alpha', 'Beta', '2026-01-02'),
          (2, 'alpha-vs-beta-m1-nuke-p2.dem', 'de_nuke', 'ALPHA', NULL, '2026-01-02'),
          (3, 'alpha-vs-beta-m1-nuke-p1.dem', 'de_nuke', 'Alpha', 'Beta', '2026-02-03');
    """)
    current = conn.execute("""
        SELECT demo_id, filename, map_name mn, team1, team2, demo_date FROM demos WHERE demo_id=1
    """).fetchone()

    assert _recording_parts(conn, current) == [1, 2]


def test_split_pistol_flags_use_global_round_numbers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE demos (demo_id INTEGER, filename TEXT, map_name TEXT, status TEXT,
                            demo_date TEXT);
        CREATE TABLE rounds (round_id INTEGER, demo_id INTEGER, round_num INTEGER,
                             is_pistol INTEGER);
        INSERT INTO demos VALUES
          (1, 'alpha-vs-beta-m1-anubis-p1.dem', 'de_anubis', 'ok', '2026-01-02'),
          (2, 'alpha-vs-beta-m1-anubis-p2.dem', 'de_anubis', 'ok', '2026-01-02');
    """)
    rows = []
    round_id = 1
    for demo_id, count in ((1, 7), (2, 17)):
        for local_round in range(1, count + 1):
            rows.append((round_id, demo_id, local_round,
                         int(local_round in (1, 13))))
            round_id += 1
    conn.executemany("INSERT INTO rounds VALUES (?,?,?,?)", rows)

    assert normalize_split_pistols(conn) == {"de_anubis"}
    pistols = conn.execute(
        "SELECT round_id FROM rounds WHERE is_pistol=1 ORDER BY round_id").fetchall()
    assert [r["round_id"] for r in pistols] == [1, 13]
    assert normalize_split_pistols(conn) == set()
