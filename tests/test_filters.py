"""Filter-mask semantics: bomb-site chips, pistol buy category."""
import numpy as np

from demoquerycs2.search import engine


def _stub_index(n=6):
    idx = engine.MapIndex(
        state_id=np.arange(n, dtype=np.int64),
        round_id=np.arange(n, dtype=np.int64),
        demo_id=np.zeros(n, dtype=np.int64),
        tick=np.arange(n, dtype=np.int64),
        sec=np.zeros(n, dtype=np.int32),
        bomb=np.array([False, True, True, True, False, True]),
        alive_ct=np.full(n, 5, dtype=np.int8),
        alive_t=np.full(n, 5, dtype=np.int8),
        ct_nodes=np.zeros((n, 5), dtype=np.uint8),
        t_nodes=np.zeros((n, 5), dtype=np.uint8),
        #        pistol eco  semi full pistol full
        ct_buy=np.array([3, 0, 1, 2, 3, 2], dtype=np.int8),
        t_buy=np.array([3, 0, 0, 2, 3, 1], dtype=np.int8),
        #          -  A  B  A  -  unknown-site plant
        bomb_site=np.array([0, 1, 2, 1, 0, 0], dtype=np.int8),
        #            CT CT  T CT  T  T round winners
        winner_code=np.array([1, 1, 2, 1, 2, 2], dtype=np.int8),
        # Alpha, Alpha, Beta, unknown, Alpha, Beta
        ct_team_code=np.array([0, 0, 1, -1, 0, 1], dtype=np.int32),
        team_names=["Alpha", "Beta"],
        #                 smoke active in states 1 and 2, molly in state 3
        smoke=engine.UtilCSR.from_dense(
            _util_nodes(n, engine.MAX_ACTIVE_SMOKES, {1: 7, 2: 9}),
            np.zeros((n, engine.MAX_ACTIVE_SMOKES, 3), dtype=np.float32)),
        molly=engine.UtilCSR.from_dense(
            _util_nodes(n, engine.MAX_ACTIVE_MOLLIES, {3: 4}),
            np.zeros((n, engine.MAX_ACTIVE_MOLLIES, 3), dtype=np.float32)),
    )
    return idx


def _util_nodes(n, cap, active):
    arr = np.full((n, cap), 0xFF, dtype=np.uint8)
    for row, node in active.items():
        arr[row, 0] = node
    return arr


def test_bomb_sites_chips():
    idx = _stub_index()
    m = engine._filter_mask(idx, {"bomb_sites": ["A"]})
    assert list(np.nonzero(m)[0]) == [1, 3]
    m = engine._filter_mask(idx, {"bomb_sites": ["none"]})
    assert list(np.nonzero(m)[0]) == [0, 4]
    # A+B together means "any plant" - unknown-site plants (state 5) included
    m = engine._filter_mask(idx, {"bomb_sites": ["A", "B"]})
    assert list(np.nonzero(m)[0]) == [1, 2, 3, 5]


def test_bomb_sites_all_or_none_means_no_filter():
    idx = _stub_index()
    for sites in (["A", "B", "none"], []):
        m = engine._filter_mask(idx, {"bomb_sites": sites})
        assert m.all()


def test_bomb_legacy_fields_still_work():
    idx = _stub_index()
    m = engine._filter_mask(idx, {"bomb_planted": True, "bomb_site": "B"})
    assert list(np.nonzero(m)[0]) == [2]


def test_time_left_filters_preplant_only():
    idx = _stub_index()
    #                    1:55  1:00  0:30  0:05 elapsed on the un-planted states 0/4
    idx.sec = np.array([0, 20, 90, 60, 110, 130], dtype=np.int32)
    # states 0,4: pre-plant at 1:55 / 0:05 left; 1,2,3,5 planted (clock gone)
    m = engine._filter_mask(idx, {"time_left": [115, 0]})
    assert m.all()                                   # full window = no-op
    m = engine._filter_mask(idx, {"time_left": [30, 0]})
    assert list(np.nonzero(m)[0]) == [1, 2, 3, 4, 5]  # planted pass; state 0 too early
    m = engine._filter_mask(idx, {"time_left": [115, 60]})
    assert list(np.nonzero(m)[0]) == [0, 1, 2, 3, 5]  # state 4 (0:05 left) drops
    # combined with phase chips: pre-plant only + late window
    m = engine._filter_mask(idx, {"time_left": [30, 0], "bomb_sites": ["none"]})
    assert list(np.nonzero(m)[0]) == [4]
    # elapsed past the clock (timeout slop) counts as 0:00 left, not negative
    idx.sec = np.array([118, 0, 0, 0, 0, 0], dtype=np.int32)
    m = engine._filter_mask(idx, {"time_left": [10, 0]})
    assert m[0]


def test_legacy_time_range_unchanged():
    idx = _stub_index()
    idx.sec = np.array([0, 20, 90, 60, 110, 130], dtype=np.int32)
    m = engine._filter_mask(idx, {"time_range": [0, 60]})
    assert list(np.nonzero(m)[0]) == [0, 1, 3]       # elapsed window, planted included


def test_pistol_is_exclusive_buy_category():
    idx = _stub_index()
    m = engine._filter_mask(idx, {"ct_buy": ["pistol"]})
    assert list(np.nonzero(m)[0]) == [0, 4]
    # eco selected without pistol must not include pistol rounds
    m = engine._filter_mask(idx, {"ct_buy": ["eco"]})
    assert list(np.nonzero(m)[0]) == [1]
    m = engine._filter_mask(idx, {"t_buy": ["pistol", "eco", "semi", "full"]})
    assert m.all()


def test_smoke_molly_active_chips():
    idx = _stub_index()
    m = engine._filter_mask(idx, {"smoke_active": True})
    assert list(np.nonzero(m)[0]) == [1, 2]
    m = engine._filter_mask(idx, {"molly_active": True})
    assert list(np.nonzero(m)[0]) == [3]
    m = engine._filter_mask(idx, {"smoke_active": False})
    assert list(np.nonzero(m)[0]) == [0, 3, 4, 5]
    # None = no filter
    m = engine._filter_mask(idx, {"smoke_active": None, "molly_active": None})
    assert m.all()
    # combined: smoke AND molly both active never happens in the stub
    m = engine._filter_mask(idx, {"smoke_active": True, "molly_active": True})
    assert not m.any()
