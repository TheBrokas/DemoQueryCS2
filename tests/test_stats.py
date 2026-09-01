"""Scenario-stats aggregation and the win-reason backfill."""
import numpy as np
import pytest

from demoquerycs2 import config, db as dbmod
from demoquerycs2.ingest import scanner
from demoquerycs2.ingest.demo_parser import _reason_of
from demoquerycs2.search import engine


def _empty_csr(n, cap):
    return engine.UtilCSR.from_dense(
        np.full((n, cap), 0xFF, dtype=np.uint8),
        np.zeros((n, cap, 3), dtype=np.float32))


def _stats_index():
    """10 states across 5 rounds / 3 demos; states are round-contiguous, sec-ascending."""
    n = 10
    #             round: 1  1  1  2  2  3  3  4   4    5
    round_id = np.array([1, 1, 1, 2, 2, 3, 3, 4, 4, 5], dtype=np.int64)
    demo_id = np.array([1, 1, 1, 1, 1, 3, 3, 2, 2, 2], dtype=np.int64)
    sec = np.array([5, 6, 30, 50, 80, 10, 12, 100, 118, 0], dtype=np.int32)
    bomb = np.array([False, False, False, False, True, False, False, False, False, False])
    #        winners: r1 CT, r2 T, r3 CT, r4 T, r5 CT
    winner = np.array([1, 1, 1, 2, 2, 1, 1, 2, 2, 1], dtype=np.int8)
    #        CT buys: r1 full, r2 eco, r3 semi, r4 pistol, r5 full
    ct_buy = np.array([2, 2, 2, 0, 0, 1, 1, 3, 3, 2], dtype=np.int8)
    #        T buys:  r1 full, r2 semi, r3 eco, r4 pistol, r5 full
    t_buy = np.array([2, 2, 2, 1, 1, 0, 0, 3, 3, 2], dtype=np.int8)
    return engine.MapIndex(
        state_id=np.arange(n, dtype=np.int64),
        round_id=round_id,
        demo_id=demo_id,
        tick=sec.astype(np.int64) * 64,
        sec=sec,
        bomb=bomb,
        alive_ct=np.full(n, 5, dtype=np.int8),
        alive_t=np.full(n, 5, dtype=np.int8),
        ct_nodes=np.zeros((n, 5), dtype=np.uint8),
        t_nodes=np.zeros((n, 5), dtype=np.uint8),
        ct_buy=ct_buy,
        t_buy=t_buy,
        bomb_site=np.zeros(n, dtype=np.int8),
        winner_code=winner,
        ct_team_code=np.full(n, -1, dtype=np.int32),
        team_names=[],
        smoke=_empty_csr(n, engine.MAX_ACTIVE_SMOKES),
        molly=_empty_csr(n, engine.MAX_ACTIVE_MOLLIES),
    )


@pytest.fixture()
def stats_conn(tmp_path):
    conn = dbmod.connect(tmp_path / "stats.sqlite3")
    conn.executemany(
        "INSERT INTO demos (demo_id, filename, file_size, content_key, map_name, tickrate, "
        "parsed_at, tokenizer_version, team1, team2) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(1, "d1.dem", 1, "k1", "de_test", 64.0, "now", 7, "Alpha", "Beta"),
         (2, "d2.dem", 1, "k2", "de_test", 64.0, "now", 7, "Gamma", "Delta"),
         (3, "d3.dem", 1, "k3", "de_test", 64.0, "now", 7, "Alpha", "Beta")])
    #        (rid, demo, num, winner, reason, ct_team)   reason NULL on r2 = unknown bucket
    rounds = [(1, 1, 1, "CT", "t_killed", "Alpha"),
              (2, 1, 2, "T", None, "Alpha"),
              (3, 3, 1, "CT", "time_ran_out", "Beta"),
              (4, 2, 1, "T", "ct_killed", "Gamma"),
              (5, 2, 2, "CT", "bomb_defused", "Gamma")]
    conn.executemany(
        "INSERT INTO rounds (round_id, demo_id, round_num, freeze_end_tick, end_tick, winner, "
        "ct_buy, t_buy, win_reason, ct_team) VALUES (?,?,?,0,9999,?,'full','full',?,?)",
        rounds)
    #       r1: pre-anchor kill, same-second kill, unknown-side death, then a T death
    kills = [(1, 1, 5 * 64, 5, "T"), (1, 1, 6 * 64, 6, "T"),
             (1, 1, 9 * 64, 9, None), (1, 1, 12 * 64, 12, "T"),
             (2, 1, 85 * 64, 85, "CT"),
             (4, 2, 90 * 64, 90, "CT")]           # r4: only kill is before the anchor (100)
    conn.executemany(
        "INSERT INTO kills (round_id, demo_id, tick, round_time_s, victim, victim_side) "
        "VALUES (?,?,?,?,'x',?)", kills)
    conn.commit()
    yield conn
    conn.close()


def test_compute_stats_full(stats_conn):
    idx = _stats_index()
    sel_all = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int64)   # round 5 filter-only
    sel_exact = np.array([1, 2, 4, 7, 8], dtype=np.int64)             # rounds 1, 2, 4
    filter_mask = np.ones(10, dtype=bool)
    s = engine._compute_stats(idx, sel_all, sel_exact, filter_mask, stats_conn)

    # demos spans ALL matched rounds (exact 1,2,4 -> demos 1,2 PLUS close 3 -> demo 3)
    assert s["rounds"] == {"exact": 3, "close": 1, "baseline": 5, "demos": 3}
    # exact: r1 CT, r2 T, r4 T; close: r3 CT; baseline: 3 CT / 2 T
    assert s["winrate"]["exact"] == {"ct": 1, "t": 2}
    assert s["winrate"]["close"] == {"ct": 1, "t": 0}
    assert s["winrate"]["baseline"] == {"ct": 3, "t": 2}

    # exact pre-plant states at sec 6, 30, 100, 118 -> bins 0, 3, 10, 11 (118 clamps);
    # post-plant: round 2 only
    t = s["timing"]
    assert t["bin_s"] == engine.STATS_BIN_S and t["clock_s"] == config.ROUND_CLOCK_S
    expect = [0] * 12
    expect[0] = expect[3] = expect[10] = expect[11] = 1
    assert t["pre"] == expect
    assert t["post_plant"] == 1

    # buy mix over exact rounds (r1 full, r2 eco, r4 pistol for CT)
    assert s["economy"]["ct"] == {"eco": 1, "semi": 0, "full": 1, "pistol": 1}
    assert s["economy"]["t"] == {"eco": 0, "semi": 1, "full": 1, "pistol": 1}

    # reasons: r1 CT by t_killed; r2 T with NULL reason -> unknown; r4 T by ct_killed
    assert s["win_reasons"]["ct"] == {"t_killed": 1, "bomb_defused": 0, "time_ran_out": 0, "unknown": 0}
    assert s["win_reasons"]["t"] == {"ct_killed": 1, "bomb_exploded": 0, "unknown": 1}

    # ct_team per exact round: Alpha (r1), Alpha (r2), Gamma (r4); T side is the other name
    assert s["teams"]["ct"] == [{"name": "Alpha", "rounds": 2}, {"name": "Gamma", "rounds": 1}]
    assert s["teams"]["t"] == [{"name": "Beta", "rounds": 2}, {"name": "Delta", "rounds": 1}]

    # next kill: r1 anchor 6 -> pre/at-anchor and unknown-side kills skipped, T dies
    # at 12 (CT credited, +6s); r2 anchor 80 -> CT dies at 85 (T credited, +5s);
    # r4 anchor 100 -> only kill was at 90 -> none
    assert s["next_kill"]["ct"] == 1 and s["next_kill"]["t"] == 1 and s["next_kill"]["none"] == 1
    assert s["next_kill"]["median_s"] == 5          # median of [6, 5] -> 5.5, int-truncated


def test_compute_stats_empty_selection(stats_conn):
    idx = _stats_index()
    empty = np.zeros(0, dtype=np.int64)
    s = engine._compute_stats(idx, empty, empty.copy(), np.zeros(10, dtype=bool), stats_conn)
    assert s["rounds"] == {"exact": 0, "close": 0, "baseline": 0, "demos": 0}
    assert s["winrate"]["exact"] == {"ct": 0, "t": 0}
    assert s["timing"]["pre"] == [0] * 12 and s["timing"]["post_plant"] == 0
    assert s["next_kill"] == {"ct": 0, "t": 0, "none": 0, "median_s": None}
    assert s["teams"] == {"ct": [], "t": []}


def test_compute_stats_filter_only_shortcut(stats_conn):
    """sel_exact IS sel_all (filter-only search): one population serves all buckets."""
    idx = _stats_index()
    sel = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int64)
    mask = np.ones(10, dtype=bool)
    mask[9] = False                                  # real usage: sel == nonzero(mask)
    s = engine._compute_stats(idx, sel, sel, mask, stats_conn)
    assert s["rounds"]["exact"] == 4 and s["rounds"]["close"] == 0
    assert s["rounds"]["baseline"] == 4
    assert s["winrate"]["exact"] == s["winrate"]["baseline"] == {"ct": 2, "t": 2}


def test_round_reps_picks_earliest_state():
    idx = _stats_index()
    rids, reps = engine._round_reps(idx, np.array([2, 1, 4, 3], dtype=np.int64))
    assert list(rids) == [1, 2]
    # representative = first occurrence in the selection = earliest matching state
    assert list(reps) == [2, 4] or list(reps) == [1, 3]


def test_reason_of_normalization():
    assert _reason_of("bomb_defused") == "bomb_defused"
    assert _reason_of("Round_Draw") is None          # not a known reason
    assert _reason_of(float("nan")) is None
    assert _reason_of(None) is None


def _bf_db(tmp_path, demos, rounds):
    conn = dbmod.connect(tmp_path / "bf.sqlite3")
    conn.executemany(
        "INSERT INTO demos (demo_id, filename, file_size, content_key, map_name, tickrate, "
        "parsed_at, tokenizer_version) VALUES (?,?,?,?,?,?,?,?)",
        [(did, name, size, key, "de_test", 64.0, "now", 7) for did, name, size, key in demos])
    conn.executemany(
        "INSERT INTO rounds (round_id, demo_id, round_num, freeze_end_tick, end_tick, winner, "
        "ct_buy, t_buy) VALUES (?,?,?,0,?,'CT','full','full')", rounds)
    conn.commit()
    return conn


def _reasons(conn):
    return {r["round_id"]: r["win_reason"]
            for r in conn.execute("SELECT round_id, win_reason FROM rounds")}


def test_backfill_win_reasons(tmp_path, monkeypatch):
    demos_dir = tmp_path / "demos"
    demos_dir.mkdir()
    (demos_dir / "a.dem").write_bytes(b"stub")       # 4 bytes; demo B has no file on disk
    conn = _bf_db(tmp_path,
                  demos=[(1, "a.dem", 4, "ka"), (2, "b.dem", 4, "kb")],
                  rounds=[(1, 1, 1, 1000), (2, 1, 2, 2000), (3, 2, 1, 500)])
    monkeypatch.setattr(config, "DEMOS_DIR", demos_dir)
    #                       end_tick 2000 has no event -> settles as terminal 'unknown'
    monkeypatch.setattr(scanner, "_reason_worker", lambda p: {1000: "bomb_defused"})
    scanner.backfill_win_reasons(conn)
    # missing file (demo B) stays NULL: it retries once the file is back
    assert _reasons(conn) == {1: "bomb_defused", 2: "unknown", 3: None}
    scanner.backfill_win_reasons(conn)               # demo B still absent: still NULL, no crash
    assert _reasons(conn)[3] is None
    conn.close()


def test_backfill_resolves_basename_collisions_by_size(tmp_path, monkeypatch):
    demos_dir = tmp_path / "demos"
    (demos_dir / "eventA").mkdir(parents=True)
    (demos_dir / "eventB").mkdir()
    (demos_dir / "eventA" / "m.dem").write_bytes(b"xx")        # 2 bytes - the indexed demo
    (demos_dir / "eventB" / "m.dem").write_bytes(b"yyyy")      # 4 bytes - different match
    conn = _bf_db(tmp_path, demos=[(1, "m.dem", 2, "k1")], rounds=[(1, 1, 1, 1000)])
    monkeypatch.setattr(config, "DEMOS_DIR", demos_dir)
    seen = []
    monkeypatch.setattr(scanner, "_reason_worker",
                        lambda p: seen.append(p) or {1000: "t_killed"})
    scanner.backfill_win_reasons(conn)
    assert _reasons(conn) == {1: "t_killed"}
    assert seen == [str(demos_dir / "eventA" / "m.dem")]       # size picked the right file
    conn.close()


def test_backfill_transient_failure_stays_null(tmp_path, monkeypatch):
    demos_dir = tmp_path / "demos"
    demos_dir.mkdir()
    (demos_dir / "a.dem").write_bytes(b"stub")
    conn = _bf_db(tmp_path, demos=[(1, "a.dem", 4, "ka")], rounds=[(1, 1, 1, 1000)])
    monkeypatch.setattr(config, "DEMOS_DIR", demos_dir)

    def boom(p):
        raise OSError("drive hiccup")
    monkeypatch.setattr(scanner, "_reason_worker", boom)
    scanner.backfill_win_reasons(conn)
    assert _reasons(conn) == {1: None}               # NULL survives -> retried next scan
    monkeypatch.setattr(scanner, "_reason_worker", lambda p: {1000: "bomb_exploded"})
    scanner.backfill_win_reasons(conn)
    assert _reasons(conn) == {1: "bomb_exploded"}    # and the retry heals it
    conn.close()
