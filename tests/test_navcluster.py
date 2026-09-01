"""Nav clustering unit tests on a synthetic 4x4 grid mesh."""
import json

import numpy as np
import pytest

from demoquerycs2 import navcluster


@pytest.fixture()
def grid_nav(tmp_path):
    """4x4 grid of 100x100 areas, 4-connected."""
    areas = {}
    def aid(r, c):
        return r * 4 + c + 1
    for r in range(4):
        for c in range(4):
            x0, y0 = c * 100.0, r * 100.0
            conns = []
            if r > 0: conns.append(aid(r - 1, c))
            if r < 3: conns.append(aid(r + 1, c))
            if c > 0: conns.append(aid(r, c - 1))
            if c < 3: conns.append(aid(r, c + 1))
            areas[str(aid(r, c))] = {
                "area_id": aid(r, c),
                "corners": [{"x": x0, "y": y0, "z": 0.0}, {"x": x0 + 100, "y": y0, "z": 0.0},
                            {"x": x0 + 100, "y": y0 + 100, "z": 0.0}, {"x": x0, "y": y0 + 100, "z": 0.0}],
                "connections": conns,
            }
    p = tmp_path / "nav.json"
    p.write_text(json.dumps({"version": 35, "areas": areas}))
    return navcluster.load_nav_json(p)


def test_load(grid_nav):
    assert len(grid_nav.area_ids) == 16
    assert grid_nav.poly_area[1] == pytest.approx(10000.0)


def test_cluster_deterministic(grid_nav):
    a = navcluster.cluster_areas(grid_nav, 4)
    b = navcluster.cluster_areas(grid_nav, 4)
    assert a == b
    assert set(a.values()) == {0, 1, 2, 3}
    assert set(a.keys()) == set(grid_nav.area_ids)


def test_clusters_connected(grid_nav):
    mapping = navcluster.cluster_areas(grid_nav, 4)
    for node in set(mapping.values()):
        members = {a for a, n in mapping.items() if n == node}
        seen = {next(iter(members))}
        frontier = list(seen)
        while frontier:
            cur = frontier.pop()
            for nb in grid_nav.connections[cur]:
                if nb in members and nb not in seen:
                    seen.add(nb)
                    frontier.append(nb)
        assert seen == members, f"node {node} not internally connected"


def test_artifacts_and_raster_roundtrip(grid_nav):
    mapping = navcluster.cluster_areas(grid_nav, 4)
    art = navcluster.build_artifacts(grid_nav, mapping)
    k = int(art["k"])
    assert k == 4
    assert art["geo"].shape == (k, k)
    assert np.all(np.diag(art["geo"]) == 0)
    assert np.all(art["geo"] >= 0)
    # symmetric geodesics
    assert np.allclose(art["geo"], art["geo"].T)

    # every area centroid must raster-resolve to its own node
    x0, y0, cell = art["raster_meta"]
    rn = art["raster_node"]
    for aidx, aid in enumerate(sorted(grid_nav.area_ids)):
        cx, cy = grid_nav.centroid[aid][:2]
        ix, iy = int((cx - x0) / cell), int((cy - y0) / cell)
        assert rn[iy, ix, 0] == mapping[aid]


def test_default_k_bounds():
    assert navcluster.default_k(100) == 60
    assert navcluster.default_k(2500) == 125
    assert navcluster.default_k(100000) == 180
