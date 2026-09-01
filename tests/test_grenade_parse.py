"""Grenade event parsing: detonate/expire pairing, fallbacks, round mapping, paths."""
import struct

import numpy as np
import pandas as pd

from demoquerycs2.ingest import tokenizer
from demoquerycs2.ingest.demo_parser import (
    MOLLY_FALLBACK_S,
    SMOKE_FALLBACK_S,
    TICKRATE,
    ParsedRound,
    _build_tracks,
    _match_track,
    _pack_path,
    _parse_grenades,
)
from demoquerycs2.navcluster import EMPTY_NODE

FE0, END0 = 1000, 1000 + 100 * TICKRATE      # round 1: 100s live
FE1, END1 = 8000, 8000 + 60 * TICKRATE       # round 2: 60s live


def _rounds():
    return [ParsedRound(round_num=1, freeze_end_tick=FE0, end_tick=END0, winner="CT"),
            ParsedRound(round_num=2, freeze_end_tick=FE1, end_tick=END1, winner="T")]


def _det(tick, entityid=None, x=100.0, y=200.0, z=0.0, team=2, name="p1"):
    return {"tick": tick, "entityid": entityid, "x": x, "y": y, "z": z,
            "user_team_num": team, "user_name": name}


def test_smoke_expire_pairing_and_fallback():
    det = pd.DataFrame([_det(FE0 + 10 * TICKRATE, entityid=5),
                        _det(FE0 + 40 * TICKRATE, entityid=6)])
    exp = pd.DataFrame([{"tick": FE0 + 28 * TICKRATE, "entityid": 5}])   # only 5 expires
    out = _parse_grenades({"smokegrenade_detonate": det, "smokegrenade_expired": exp},
                          _rounds(), None)
    assert [g.gtype for g in out] == ["smoke", "smoke"]
    assert out[0].round_time_s == 10 and out[0].end_time_s == 28        # paired
    assert out[1].end_time_s == 40 + SMOKE_FALLBACK_S                   # fallback


def test_end_clamped_to_round_end():
    det = pd.DataFrame([_det(END0 - 5 * TICKRATE, entityid=1)])          # 5s before round end
    out = _parse_grenades({"smokegrenade_detonate": det}, _rounds(), None)
    assert out[0].end_time_s == 100                                      # clamped, not 95+20


def test_molly_pair_and_out_of_round_skips():
    det = pd.DataFrame([
        _det(FE1 + 5 * TICKRATE, entityid=9),    # in round 2
        _det(FE0 - 500),                         # before first round: skipped
        _det(END0 + 100),                        # between rounds: skipped
    ])
    exp = pd.DataFrame([{"tick": FE1 + 9 * TICKRATE, "entityid": 9}])
    out = _parse_grenades({"inferno_startburn": det, "inferno_expire": exp}, _rounds(), None)
    assert len(out) == 1
    assert out[0].round_index == 1
    assert out[0].end_time_s - out[0].round_time_s == 4


def test_molly_fallback_without_expire():
    det = pd.DataFrame([_det(FE0 + 20 * TICKRATE, entityid=3)])
    out = _parse_grenades({"inferno_startburn": det}, _rounds(), None)
    assert out[0].end_time_s == 20 + MOLLY_FALLBACK_S


def test_flash_he_have_no_end_time():
    flash = pd.DataFrame([_det(FE0 + 15 * TICKRATE, team=3, name="cp")])
    he = pd.DataFrame([_det(FE0 + 16 * TICKRATE, team=2)])
    out = _parse_grenades({"flashbang_detonate": flash, "hegrenade_detonate": he},
                          _rounds(), None)
    assert {g.gtype for g in out} == {"flash", "he"}
    assert all(g.end_time_s is None for g in out)
    sides = {g.gtype: g.thrower_side for g in out}
    assert sides == {"flash": "CT", "he": "T"}


def test_missing_events_and_columns_are_graceful():
    assert _parse_grenades({}, _rounds(), None) == []
    no_xyz = pd.DataFrame([{"tick": FE0 + TICKRATE, "entityid": 1}])
    assert _parse_grenades({"smokegrenade_detonate": no_xyz}, _rounds(), None) == []
    nan_pos = pd.DataFrame([_det(FE0 + TICKRATE, x=np.nan)])
    assert _parse_grenades({"smokegrenade_detonate": nan_pos}, _rounds(), None) == []


def test_stale_expire_never_precedes_detonate():
    # entityid reuse: expire recorded before this detonate falls back to fixed lifetime
    det = pd.DataFrame([_det(FE0 + 30 * TICKRATE, entityid=5)])
    exp = pd.DataFrame([{"tick": FE0 + 10 * TICKRATE, "entityid": 5}])
    out = _parse_grenades({"smokegrenade_detonate": det, "smokegrenade_expired": exp},
                          _rounds(), None)
    assert out[0].end_time_s == 30 + SMOKE_FALLBACK_S


def test_node_idx_without_map_nodes():
    det = pd.DataFrame([_det(FE0 + TICKRATE, entityid=1)])
    out = _parse_grenades({"smokegrenade_detonate": det}, _rounds(), None)
    assert out[0].node_idx == EMPTY_NODE


def _proj_rows(eid, ticks, xs, cls="CSmokeGrenadeProjectile"):
    return [{"grenade_type": cls, "grenade_entity_id": eid, "tick": t,
             "x": x, "y": 0.0, "z": 0.0} for t, x in zip(ticks, xs)]


def test_build_tracks_splits_on_entity_reuse():
    ticks1 = list(range(FE0, FE0 + 64, 8))
    ticks2 = list(range(FE0 + 3000, FE0 + 3064, 8))     # same entity id, 40s later
    df = pd.DataFrame(_proj_rows(7, ticks1, [float(t) for t in ticks1]) +
                      _proj_rows(7, ticks2, [float(t) for t in ticks2]))
    tracks = _build_tracks(df)
    assert len(tracks["smoke"]) == 2
    assert all(len(tk) >= 2 for tk, _ in tracks["smoke"])
    assert tracks["molly"] == []


def test_match_track_nearest_in_space_and_time():
    near = (np.array([100, 164]), np.array([[0, 0, 0], [50.0, 0, 0]], dtype=np.float32))
    far = (np.array([100, 164]), np.array([[0, 0, 0], [900.0, 0, 0]], dtype=np.float32))
    old = (np.array([100, 164]), np.array([[0, 0, 0], [55.0, 0, 0]], dtype=np.float32))
    tracks = [far, near]
    got = _match_track(tracks, 170, 60.0, 0.0, 0.0)
    assert got is not None and got[1][-1][0] == 50.0    # picked the near track
    assert len(tracks) == 1                             # popped
    # a track ending long before the detonation is ignored
    assert _match_track([old], 170 + 10 * TICKRATE, 55.0, 0.0, 0.0) is None


def test_match_track_truncates_lingering_entity():
    # smoke projectile entity lives on ~20s after detonation: track must be cut at det tick
    ticks = np.arange(100, 100 + 20 * TICKRATE, 8)
    xyz = np.zeros((len(ticks), 3), dtype=np.float32)
    xyz[:, 0] = np.minimum(np.arange(len(ticks)) * 10.0, 500.0)   # flies then rests at 500
    det_tick = int(ticks[60])                                     # mid-track detonation
    got = _match_track([(ticks, xyz)], det_tick, float(xyz[60, 0]), 0.0, 0.0)
    assert got is not None
    assert int(got[0][-1]) <= det_tick                            # truncated at detonation


def test_pack_path_keeps_endpoints_and_round_time():
    ticks = np.array([FE0, FE0 + 8, FE0 + 16, FE0 + 24, FE0 + 30])
    xyz = np.array([[float(i), 2.0, 3.0] for i in range(5)], dtype=np.float32)
    blob = _pack_path(ticks, xyz, FE0)
    assert len(blob) % 8 == 1                           # v2: version byte + 8B samples
    pts = tokenizer.unpack_path(blob)
    assert pts[0][0] == 0.0 and pts[0][1] == 0.0        # first point, t_rel 0
    assert pts[-1][1] == 4.0                            # last point always kept
    assert abs(pts[-1][0] - 30 / TICKRATE) < 0.01       # centisecond quantization


def test_path_v1_blobs_still_readable():
    v1 = struct.pack("<8f", 0.0, 1.0, 2.0, 3.0, 1.5, 4.0, 5.0, 6.0)
    pts = tokenizer.unpack_path(v1)
    assert pts == [[0.0, 1.0, 2.0, 3.0], [1.5, 4.0, 5.0, 6.0]]
    # v1 blobs are even-length, v2 odd - that is what the migration scans on
    assert len(v1) % 2 == 0 and len(tokenizer.pack_path(pts)) % 2 == 1


def test_pack_path_roundtrip_precision():
    pts = [[0.0, -1825.4, 2314.6, 1586.2], [12.34, 3305.0, -1524.0, 1906.9]]
    got = tokenizer.unpack_path(tokenizer.pack_path(pts))
    for a, b in zip(pts, got):
        assert abs(a[0] - b[0]) <= 0.01                 # 10 ms
        assert all(abs(x - y) <= 0.5 for x, y in zip(a[1:], b[1:]))   # 1 unit


def test_detonate_gets_matching_path():
    det_tick = FE0 + 10 * TICKRATE
    flight = list(range(det_tick - 96, det_tick, 8))
    df = pd.DataFrame(_proj_rows(9, flight, [float(i) for i in range(len(flight))]))
    det = pd.DataFrame([_det(det_tick, entityid=99, x=float(len(flight) - 1), y=0.0, z=0.0)])
    out = _parse_grenades({"smokegrenade_detonate": det}, _rounds(), None, df)
    assert out[0].path is not None
    assert len(tokenizer.unpack_path(out[0].path)) >= 2
