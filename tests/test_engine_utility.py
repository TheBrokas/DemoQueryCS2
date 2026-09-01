"""Active-utility precompute and stage-2 utility scoring."""
import numpy as np

from demoquerycs2.search import engine


class StubNodes:
    """3-node line: 0 -100u- 1 -100u- 2, plus padded table (mirrors test_engine_math)."""
    def __init__(self):
        geo = np.array([[0, 100, 200], [100, 0, 100], [200, 100, 0]], dtype=np.float32)
        self.geo_padded = np.full((256, 256), 1000.0, dtype=np.float32)
        self.geo_padded[:3, :3] = geo
        self.lower_max = None

    def is_lower(self, z):
        return False


def test_build_active_utility_windows():
    round_id = np.array([1, 1, 1, 2], dtype=np.int64)
    sec = np.array([0, 10, 25, 10], dtype=np.int32)
    rows = [(1, 5, 25, 7, 10.0, 20.0, 0.0)]        # smoke active seconds 5..25 of round 1
    nodes, xyz = engine._build_active_utility(round_id, sec, rows, cap=6)
    assert nodes.shape == (4, 6)
    assert nodes[0, 0] == 0xFF                     # sec 0: before detonate
    assert nodes[1, 0] == 7 and nodes[2, 0] == 7   # sec 10 and boundary-inclusive sec 25
    assert nodes[3, 0] == 0xFF                     # other round untouched
    assert tuple(xyz[1, 0]) == (10.0, 20.0, 0.0)


def test_build_active_utility_cap_overflow_and_empty():
    round_id = np.array([1], dtype=np.int64)
    sec = np.array([10], dtype=np.int32)
    rows = [(1, 0, 20, n, 0.0, 0.0, 0.0) for n in range(3)]
    nodes, _ = engine._build_active_utility(round_id, sec, rows, cap=2)
    assert list(nodes[0]) == [0, 1]                # third smoke dropped without error
    # grenade in a round with no states is a no-op; empty index works
    nodes, _ = engine._build_active_utility(round_id, sec, [(99, 0, 20, 5, 0, 0, 0)], cap=2)
    assert (nodes == 0xFF).all()
    nodes, xyz = engine._build_active_utility(
        np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int32), [], cap=2)
    assert nodes.shape == (0, 2) and xyz.shape == (0, 2, 3)


def test_csr_matches_dense_semantics():
    # states: [empty, 2 slots, empty, 1 slot, empty] - exercises reduceat with
    # empty first/last rows and an empty row between occupied ones
    dense_nodes = np.full((5, 3), 0xFF, dtype=np.uint8)
    dense_xyz = np.zeros((5, 3, 3), dtype=np.float32)
    dense_nodes[1, :2] = [2, 0]
    dense_xyz[1, 0] = (1, 2, 3)
    dense_xyz[1, 1] = (4, 5, 6)
    dense_nodes[3, 0] = 1
    dense_xyz[3, 0] = (7, 8, 9)
    csr = engine.UtilCSR.from_dense(dense_nodes, dense_xyz)

    assert list(csr.has_any()) == [False, True, False, True, False]
    assert list(csr.row_ptr) == [0, 0, 2, 2, 3, 3]
    n1, x1 = csr.row(1)
    assert list(n1) == [2, 0] and tuple(x1[0]) == (1, 2, 3)
    n0, _ = csr.row(0)
    assert len(n0) == 0

    geo_row = np.full(256, 1000.0, dtype=np.float32)          # pad cap 1000
    geo_row[:3] = [0.0, 100.0, 200.0]                          # dist from query node 0
    d = csr.min_geo(geo_row)
    # dense equivalent: min over slots, 0xFF gathers the cap
    dense_d = geo_row[dense_nodes].min(axis=1)
    assert list(d) == list(dense_d) == [1000.0, 0.0, 1000.0, 100.0, 1000.0]


def test_csr_all_empty_and_zero_states():
    empty = engine.UtilCSR.from_dense(np.full((4, 2), 0xFF, dtype=np.uint8),
                                      np.zeros((4, 2, 3), dtype=np.float32))
    geo_row = np.full(256, 750.0, dtype=np.float32)
    assert list(empty.min_geo(geo_row)) == [750.0] * 4
    assert not empty.has_any().any()
    zero = engine.UtilCSR.from_dense(np.full((0, 2), 0xFF, dtype=np.uint8),
                                     np.zeros((0, 2, 3), dtype=np.float32))
    assert len(zero.min_geo(geo_row)) == 0


def _row(entries, cap=6):
    nodes = np.full(cap, 0xFF, dtype=np.uint8)
    xyz = np.zeros((cap, 3), dtype=np.float32)
    for i, (n, x, y, z) in enumerate(entries):
        nodes[i] = n
        xyz[i] = (x, y, z)
    return nodes, xyz


def test_utility_cost_euclid_refinement():
    stub = StubNodes()
    q = [{"node": 0, "x": 0.0, "y": 0.0, "level": None}]
    nodes, xyz = _row([(0, 30.0, 40.0, 0.0)])      # same node, 50u away
    assert engine._utility_cost(q, nodes, xyz, stub) == 50.0


def test_utility_cost_nearest_of_several():
    stub = StubNodes()
    q = [{"node": 0, "x": 0.0, "y": 0.0, "level": None}]
    nodes, xyz = _row([(2, 200.0, 0.0, 0.0), (0, 0.0, 100.0, 0.0)])
    assert engine._utility_cost(q, nodes, xyz, stub) == 100.0   # picks the closer smoke


def test_utility_cost_rejects_when_none_close():
    stub = StubNodes()
    q = [{"node": 0, "x": 0.0, "y": 0.0, "level": None}]
    empty, exyz = _row([])
    assert engine._utility_cost(q, empty, exyz, stub) == float("inf")
    far, fxyz = _row([(0, 300.0, 400.0, 0.0)])     # same node but 500u > UTIL_MATCH_RADIUS
    assert engine._utility_cost(q, far, fxyz, stub) == float("inf")


def test_utility_cost_mean_over_markers():
    stub = StubNodes()
    q = [{"node": 0, "x": 0.0, "y": 0.0, "level": None},
         {"node": 2, "x": 200.0, "y": 0.0, "level": None}]
    nodes, xyz = _row([(0, 0.0, 100.0, 0.0), (2, 200.0, 0.0, 0.0)])
    assert engine._utility_cost(q, nodes, xyz, stub) == 50.0    # mean of 100 and 0
