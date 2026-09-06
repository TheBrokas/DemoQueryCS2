"""Offline team cores: identity, ingestion, backfill, side filters and Settings API."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from demoquerycs2 import config, db as dbmod, team_cores
from demoquerycs2.ingest import scanner
from demoquerycs2.ingest.demo_parser import ParsedDemo, ParsedRound, ParsedState
from demoquerycs2.ingest.tokenizer import pack_positions, pack_token
from demoquerycs2.search import engine
from demoquerycs2.web.app import app, _match_context


IDS = [str(76561198000000000 + n) for n in range(12)]


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "cores.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DEMO_MODE", False)
    engine.invalidate()
    conn = dbmod.connect(path)
    yield conn
    conn.close()
    engine.invalidate()


def add_demo(conn, demo_id=1, ids=None, date="2026-09-01", names=None, ct_sides=(True, False), recorded=False):
    ids = ids or IDS[:10]
    conn.execute(
        "INSERT INTO demos(demo_id,filename,file_size,content_key,map_name,tickrate,demo_date,parsed_at,tokenizer_version,team1,team2) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (demo_id, f"scrim-{demo_id}.dem", 1, str(demo_id), "de_mirage", 64,
                                          date, "2026-09-06", 3, "Recorded" if recorded else None, "Opponent" if recorded else None))
    conn.executemany("INSERT INTO demo_players VALUES (?,?,?,?)",
                     [(demo_id, slot, sid, (names or {}).get(sid, f"Player {slot}")) for slot, sid in enumerate(ids)])
    for n, core_ct in enumerate(ct_sides, 1):
        rid = demo_id * 100 + n
        conn.execute(
            "INSERT INTO rounds(round_id,demo_id,round_num,freeze_end_tick,end_tick,winner,ct_buy,t_buy,ct_team,win_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, demo_id, n, 0, 640, "CT", "full", "full",
                                            ("Recorded" if core_ct else "Opponent") if recorded else None, "t_killed"))
        # Third core player is dead: side membership must not require being alive.
        blob = pack_positions([dict(x=i, y=i, z=0, alive=i != 2, ct=(i < 5) == core_ct,
                                    health=0 if i == 2 else 100, place_idx=0, node_idx=0) for i in range(len(ids))])
        conn.execute(
            "INSERT INTO states(round_id,demo_id,map_name,tick,round_time_s,token,bomb_planted,alive_ct,alive_t,positions) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, demo_id, "de_mirage", 64, 1, pack_token([0]*5, [0]*5), 0, 5, 5, blob))
    conn.commit()


def test_latest_name_uses_recording_date_and_stable_id(database):
    add_demo(database, 1, date="2026-09-05", names={IDS[0]: "Renamed player"})
    add_demo(database, 2, date="2026-08-01", names={IDS[0]: "Old name"})
    p = next(p for p in team_cores.players(database) if p["steamid"] == IDS[0])
    assert p["name"] == "Renamed player" and p["demos"] == 2
    assert len(team_cores.players(database)) == 10


def test_existing_demos_substitutes_halftime_and_recorded_override(database):
    add_demo(database, recorded=True)
    add_demo(database, 2, ids=IDS[:3] + IDS[10:12] + IDS[5:10])
    team_cores.save(database, "Friday stack", IDS[:3])
    database.commit()
    expected = {(101, "CT"): "Friday stack", (102, "T"): "Friday stack",
                (201, "CT"): "Friday stack", (202, "T"): "Friday stack"}
    assert team_cores.overrides(database) == expected
    idx = engine.get_index("de_mirage")
    for side, want in [("ct", [101, 201]), ("t", [102, 202]), ("any", [101, 102, 201, 202])]:
        mask = engine._filter_mask(idx, {"team": "Friday stack", "team_side": side})
        assert idx.round_id[mask].tolist() == want
    assert database.execute("SELECT team1 FROM demos WHERE demo_id=1").fetchone()[0] == "Recorded"
    current = database.execute("SELECT r.*,d.team1,d.team2,d.filename,d.map_name mn,d.demo_date "
                               "FROM rounds r JOIN demos d ON d.demo_id=r.demo_id WHERE round_id=102").fetchone()
    context = _match_context(database, current)
    assert context["t_team"] == "Friday stack" and context["t_score"] == 1


def test_core_requires_all_players_on_same_side_and_reports_overlaps(database):
    add_demo(database)
    # All three exist in the match but one belongs to the opposition.
    team_cores.save(database, "Split core", [IDS[0], IDS[1], IDS[5]])
    assert team_cores.overrides(database) == {}
    team_cores.save(database, "Core A", IDS[:3])
    second = team_cores.save(database, "Core B", IDS[1:4])
    assert team_cores.overrides(database) == {}
    assert [c["conflicts"] for c in team_cores.list_cores(database) if c["name"] == "Core A"] == [2]
    database.execute("DELETE FROM team_cores WHERE core_id=?", (second,))
    assert team_cores.overrides(database)[(101, "CT")] == "Core A"


def test_matchup_order_survives_halftime_and_partial_recorded_names(database):
    add_demo(database, recorded=True)
    idx = engine.get_index("de_mirage")
    moments = [dict(round_id=int(rid), best_state=i, t_start=1, t_end=1, pos_score=0)
               for i, rid in enumerate(idx.round_id)]
    results = engine._hydrate_moments(idx, moments, database)
    assert [(m["team1"], m["team2"]) for m in results] == [("Recorded", "Opponent")] * 2
    team_cores.save(database, "Stack", IDS[:3])
    database.commit()
    engine.invalidate()
    results = engine._hydrate_moments(engine.get_index("de_mirage"), moments, database)
    assert [(m["team1"], m["team2"]) for m in results] == [("Stack", "Opponent")] * 2
    database.execute("DELETE FROM team_cores")
    database.execute("UPDATE demos SET team1=NULL")
    database.execute("UPDATE rounds SET ct_team=NULL WHERE round_id=101")
    database.commit()
    engine.invalidate()
    results = engine._hydrate_moments(engine.get_index("de_mirage"), moments, database)
    assert [(m["team1"], m["team2"]) for m in results] == [(None, "Opponent")] * 2


def test_playback_preserves_prior_wins_when_core_member_was_absent(database):
    add_demo(database, recorded=True)
    team_cores.save(database, "Stack", IDS[:3])
    database.execute("DELETE FROM round_players WHERE round_id=101 AND steamid=?", (IDS[2],))
    current = database.execute("SELECT r.*,d.team1,d.team2,d.filename,d.map_name mn,d.demo_date "
                               "FROM rounds r JOIN demos d ON d.demo_id=r.demo_id WHERE round_id=102").fetchone()
    context = _match_context(database, current)
    assert context["t_team"] == "Stack"
    assert context["t_score"] == 1


def test_per_demo_membership_backfill_leaves_other_demos_unprocessed(database):
    add_demo(database, 1)
    add_demo(database, 2)
    team_cores.index_rounds(database, 2)
    assert [r[0] for r in database.execute("SELECT round_id FROM round_rosters_indexed ORDER BY round_id")] == [201, 202]


def test_unnamed_opponent_score_follows_core_at_halftime(database):
    add_demo(database)
    database.execute("UPDATE rounds SET winner='T' WHERE round_id=101")
    team_cores.save(database, "Stack", IDS[:3])
    current = database.execute("SELECT r.*,d.team1,d.team2,d.filename,d.map_name mn,d.demo_date "
                               "FROM rounds r JOIN demos d ON d.demo_id=r.demo_id WHERE round_id=102").fetchone()
    context = _match_context(database, current)
    assert context["t_team"] == "Stack"
    assert context["ct_score"] == 1 and context["t_score"] == 0
    assert context["t_custom"] and not context["ct_custom"]


def test_round_backfill_is_idempotent_and_core_survives_clear(database):
    add_demo(database)
    core = team_cores.save(database, "Stack", IDS[:3])
    team_cores.index_rounds(database)
    assert database.execute("SELECT COUNT(*) FROM round_players").fetchone()[0] == 20
    database.execute("DELETE FROM demos")
    assert database.execute("SELECT COUNT(*) FROM round_players").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM round_rosters_indexed").fetchone()[0] == 0
    team_cores.save(database, "Renamed stack", IDS[:3], core)
    assert team_cores.list_cores(database)[0]["name"] == "Renamed stack"


@pytest.mark.parametrize("ids", [IDS[:2], IDS[:6], [IDS[0]]*3, IDS[:2]+["invalid"], IDS[:2]+[IDS[11]]])
def test_invalid_core_does_not_create_definition(database, ids):
    add_demo(database)
    with pytest.raises(ValueError):
        team_cores.save(database, "Stack", ids)
    assert database.execute("SELECT COUNT(*) FROM team_cores").fetchone()[0] == 0


def test_new_ingestion_applies_saved_core_without_reparsing_old_demos(database, tmp_path):
    add_demo(database)
    team_cores.save(database, "Stack", IDS[:3])
    database.commit()
    path = tmp_path / "new.dem"
    path.write_bytes(b"fixture")
    blob = pack_positions([dict(x=i, y=i, z=0, alive=True, ct=i<5, health=100,
                                place_idx=0, node_idx=0) for i in range(10)])
    state = ParsedState(round_index=0, tick=64, round_time_s=1, token=pack_token([0]*5,[0]*5),
                        bomb_planted=0, alive_ct=5, alive_t=5, positions=blob)
    parsed = ParsedDemo("de_mirage", 64, [ParsedRound(1,0,640,"CT")], [state],
                        [(i,sid,f"New {i}") for i,sid in enumerate(IDS[:10])])
    scanner._insert_demo(database, path, "new", parsed)
    database.commit()
    rid = database.execute("SELECT MAX(round_id) FROM rounds").fetchone()[0]
    assert team_cores.overrides(database)[(rid,"CT")] == "Stack"


def test_api_save_edit_delete_refreshes_team_filters_and_hosted_is_read_only(database, monkeypatch):
    add_demo(database)
    with TestClient(app) as client:
        before = engine.get_index("de_mirage")
        result = client.post("/api/settings/team-cores", json={"name":"Stack", "steamids":IDS[:3]})
        assert result.status_code == 200, result.text
        core = result.json()["core_id"]
        after = engine.get_index("de_mirage")
        assert after is not before and "Stack" in after.team_names
        assert "Stack" in client.get("/api/teams?map_name=de_mirage").json()
        result = client.post("/api/settings/team-cores", json={"core_id":core,"name":"Renamed", "steamids":IDS[:3]})
        assert result.status_code == 200
        assert "Renamed" in client.get("/api/teams?map_name=de_mirage").json()
        state = client.get("/api/settings/team-cores").json()
        assert state["cores"][0]["rounds"] == 2
        monkeypatch.setattr(config, "DEMO_MODE", True)
        assert client.get("/api/settings/team-cores").status_code == 403
        assert client.post("/api/settings/team-cores", json={"name":"Other","steamids":IDS[:3]}).status_code == 403
        assert client.post("/api/settings/team-cores/delete", json={"core_id":core}).status_code == 403
        assert team_cores.overrides(database) == {}
        monkeypatch.setattr(config, "DEMO_MODE", False)
        assert client.post("/api/settings/team-cores/delete", json={"core_id":core}).status_code == 200
        assert "Renamed" not in engine.get_index("de_mirage").team_names
