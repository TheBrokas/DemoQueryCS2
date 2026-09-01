"""Engine math unit tests with a stub MapNodes."""
import numpy as np

from demoquerycs2.search import engine


class StubNodes:
    """3-node line: 0 -100u- 1 -100u- 2, plus padded table."""
    def __init__(self):
        geo = np.array([[0, 100, 200], [100, 0, 100], [200, 100, 0]], dtype=np.float32)
        self.geo_padded = np.full((256, 256), 1000.0, dtype=np.float32)
        self.geo_padded[:3, :3] = geo
        self.lower_max = None

    def is_lower(self, z):
        return False


def test_chamfer_exact_and_neighbor():
    nodes = StubNodes()
    tokens = np.array([[0, 2, 0xFF, 0xFF, 0xFF],       # players at nodes 0 and 2
                       [1, 0xFF, 0xFF, 0xFF, 0xFF]], dtype=np.uint8)
    c = engine._chamfer(nodes, [0], tokens)
    assert c[0] == 0.0            # exact node hit
    assert c[1] == 100.0          # one node away
    c2 = engine._chamfer(nodes, [0, 2], tokens)
    assert c2[0] == 0.0           # both query points exactly covered
    assert c2[1] == 100.0         # mean of (100, 100) against the lone node-1 player


def test_assignment_prefers_optimal_pairing():
    nodes = StubNodes()
    # two drawn points at nodes 0 and 2; players ordered adversarially
    q = [{"node": 0, "x": 0, "y": 0, "level": None},
         {"node": 2, "x": 200, "y": 0, "level": None}]
    players = [{"node_idx": 2, "x": 200, "y": 0, "z": 0},
               {"node_idx": 0, "x": 0, "y": 0, "z": 0}]
    cost = engine._assignment_cost(q, players, nodes)
    assert cost == 0.0            # optimal pairing crosses the order


def test_assignment_penalizes_missing_players():
    nodes = StubNodes()
    q = [{"node": 0, "x": 0, "y": 0, "level": None},
         {"node": 2, "x": 200, "y": 0, "level": None}]
    players = [{"node_idx": 0, "x": 0, "y": 0, "z": 0}]
    cost = engine._assignment_cost(q, players, nodes)
    assert cost >= 500.0          # unmatched drawn point costs the cap


def test_assignment_euclidean_refinement():
    nodes = StubNodes()
    q = [{"node": 0, "x": 0, "y": 0, "level": None}]
    near = [{"node_idx": 0, "x": 30, "y": 40, "z": 0}]    # same node, 50u away
    far = [{"node_idx": 0, "x": 300, "y": 400, "z": 0}]   # same node, 500u away
    assert engine._assignment_cost(q, near, nodes) == 50.0
    assert engine._assignment_cost(q, far, nodes) == 500.0
