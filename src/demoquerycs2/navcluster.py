"""Cluster CS2 nav-mesh areas into mid-sized nodes and build lookup artifacts.

Input: awpy nav JSON ({version, areas: {id: {area_id, corners, connections, ...}}}).
Output artifacts per map (saved as .npz by the build script):
  - quad_xy/quad_z/quad_node: every nav-area polygon and its node assignment (UI overlay + raster)
  - node_centroid: (K,3) node centroids
  - geo: (K,K) geodesic distance matrix over the node adjacency graph
  - raster_*: game-coord grid mapping (x,y) cells to up to 3 (z, node) candidates

Clustering is deterministic: greedy agglomerative merging on the area adjacency
graph, always merging the smallest cluster into its best-connected neighbor.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

GEO_CAP = 4000.0        # cap for unreachable / cross-island distances (game units)
RASTER_CELL = 16.0
RASTER_LAYERS = 3
EMPTY_NODE = 255


@dataclass
class NavData:
    area_ids: list[int]
    corners: dict[int, np.ndarray]       # id -> (n,3) float
    connections: dict[int, set[int]]     # symmetric adjacency
    poly_area: dict[int, float]
    centroid: dict[int, np.ndarray]      # id -> (3,)


def load_nav_json(path: Path) -> NavData:
    raw = json.loads(Path(path).read_text())
    areas = raw["areas"]
    ids, corners, conns, poly_area, centroid = [], {}, {}, {}, {}
    for key, a in areas.items():
        aid = int(a.get("area_id", key))
        pts = np.array([[c["x"], c["y"], c["z"]] for c in a["corners"]], dtype=np.float64)
        if len(pts) < 3:
            continue
        ids.append(aid)
        corners[aid] = pts
        conns[aid] = set(int(c) for c in a.get("connections", []))
        x, y = pts[:, 0], pts[:, 1]
        poly_area[aid] = max(1.0, 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
        centroid[aid] = pts.mean(axis=0)
    valid = set(ids)
    for aid in ids:
        conns[aid] = {c for c in conns[aid] if c in valid}
    # make adjacency symmetric (nav connections are directed)
    for aid in ids:
        for c in conns[aid]:
            conns[c].add(aid)
    return NavData(ids, corners, conns, poly_area, centroid)


def default_k(n_areas: int) -> int:
    return int(np.clip(n_areas // 20, 60, 180))


def cluster_areas(nav: NavData, k: int) -> dict[int, int]:
    """Return area_id -> node_idx (0..K-1). Deterministic."""
    # cluster state, keyed by representative area id (min area id in cluster)
    members: dict[int, list[int]] = {aid: [aid] for aid in nav.area_ids}
    size: dict[int, float] = {aid: nav.poly_area[aid] for aid in nav.area_ids}
    cent: dict[int, np.ndarray] = {aid: nav.centroid[aid].copy() for aid in nav.area_ids}
    # adjacency between clusters with connection multiplicity
    adj: dict[int, dict[int, int]] = {aid: {} for aid in nav.area_ids}
    for aid in nav.area_ids:
        for b in nav.connections[aid]:
            if b != aid:
                adj[aid][b] = adj[aid].get(b, 0) + 1

    unmergeable: set[int] = set()
    while len(members) - len(unmergeable) > 0 and len(members) > k:
        # smallest mergeable cluster (tiebreak: lowest id)
        candidates = [c for c in members if c not in unmergeable]
        if not candidates:
            break
        c = min(candidates, key=lambda i: (size[i], i))
        neighbors = [n for n in adj[c] if n in members and n != c]
        if not neighbors:
            unmergeable.add(c)
            continue

        def score(n: int) -> tuple:
            strength = adj[c][n]
            dist = float(np.linalg.norm(cent[c][:2] - cent[n][:2]))
            return (strength / (1.0 + dist / 100.0), -size[n], -n)  # prefer strong, small, low id

        best = max(neighbors, key=score)
        keep, gone = (c, best) if c < best else (best, c)
        # merge `gone` into `keep`
        w_keep, w_gone = size[keep], size[gone]
        cent[keep] = (cent[keep] * w_keep + cent[gone] * w_gone) / (w_keep + w_gone)
        size[keep] = w_keep + w_gone
        members[keep].extend(members[gone])
        for n, cnt in adj[gone].items():
            if n == keep or n not in members:
                continue
            adj[keep][n] = adj[keep].get(n, 0) + cnt
            adj[n][keep] = adj[keep][n]
            adj[n].pop(gone, None)
        adj[keep].pop(gone, None)
        del members[gone], size[gone], cent[gone], adj[gone]
        unmergeable.discard(keep)

    # stable node ordering: by centroid (y desc, x asc) so ids are spatially coherent
    reps = sorted(members.keys(), key=lambda i: (-round(cent[i][1] / 256), round(cent[i][0] / 256), i))
    area_to_node: dict[int, int] = {}
    for node_idx, rep in enumerate(reps):
        for aid in members[rep]:
            area_to_node[aid] = node_idx
    return area_to_node


def _fill_polygon(poly_xy: np.ndarray, x0: float, y0: float, cell: float,
                  w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Grid cell indices (ix, iy) whose centers fall inside the polygon (even-odd rule)."""
    xmin, ymin = poly_xy.min(axis=0)
    xmax, ymax = poly_xy.max(axis=0)
    ix0 = max(0, int((xmin - x0) / cell))
    ix1 = min(w - 1, int((xmax - x0) / cell))
    iy0 = max(0, int((ymin - y0) / cell))
    iy1 = min(h - 1, int((ymax - y0) / cell))
    if ix1 < ix0 or iy1 < iy0:
        return np.array([], dtype=int), np.array([], dtype=int)
    gx = x0 + (np.arange(ix0, ix1 + 1) + 0.5) * cell
    gy = y0 + (np.arange(iy0, iy1 + 1) + 0.5) * cell
    cx, cy = np.meshgrid(gx, gy)
    inside = np.zeros(cx.shape, dtype=bool)
    n = len(poly_xy)
    j = n - 1
    for i in range(n):
        xi, yi = poly_xy[i]
        xj, yj = poly_xy[j]
        crosses = ((yi > cy) != (yj > cy)) & (cx < (xj - xi) * (cy - yi) / (yj - yi + 1e-12) + xi)
        inside ^= crosses
        j = i
    iy, ix = np.nonzero(inside)
    return ix + ix0, iy + iy0


def build_artifacts(nav: NavData, area_to_node: dict[int, int]) -> dict[str, np.ndarray]:
    k = max(area_to_node.values()) + 1
    if k > EMPTY_NODE - 1:
        raise ValueError(f"K={k} exceeds uint8 capacity")

    # --- node centroids (walkable-area weighted) ---
    cent_acc = np.zeros((k, 3)), np.zeros(k)
    for aid, node in area_to_node.items():
        w = nav.poly_area[aid]
        cent_acc[0][node] += nav.centroid[aid] * w
        cent_acc[1][node] += w
    node_centroid = (cent_acc[0] / cent_acc[1][:, None]).astype(np.float32)

    # --- node adjacency -> geodesic matrix (Floyd-Warshall) ---
    geo = np.full((k, k), np.inf, dtype=np.float64)
    np.fill_diagonal(geo, 0.0)
    for aid, node in area_to_node.items():
        for b in nav.connections[aid]:
            nb = area_to_node.get(b)
            if nb is not None and nb != node:
                d = float(np.linalg.norm(node_centroid[node] - node_centroid[nb]))
                if d < geo[node, nb]:
                    geo[node, nb] = geo[nb, node] = d
    for m in range(k):
        geo = np.minimum(geo, geo[:, m:m + 1] + geo[m:m + 1, :])
    geo = np.minimum(geo, GEO_CAP).astype(np.float32)

    # --- per-area quads for overlay + rasterization ---
    n_areas = len(nav.area_ids)
    quad_xy = np.zeros((n_areas, 4, 2), dtype=np.float32)
    quad_z = np.zeros(n_areas, dtype=np.float32)
    quad_node = np.zeros(n_areas, dtype=np.uint8)
    for i, aid in enumerate(sorted(nav.area_ids)):
        pts = nav.corners[aid]
        idx = np.linspace(0, len(pts) - 1, 4).round().astype(int) if len(pts) != 4 else np.arange(4)
        quad_xy[i] = pts[idx, :2]
        quad_z[i] = pts[:, 2].mean()
        quad_node[i] = area_to_node[aid]

    # --- raster ---
    all_pts = np.concatenate([nav.corners[a][:, :2] for a in nav.area_ids])
    x0, y0 = all_pts.min(axis=0) - 2 * RASTER_CELL
    x1, y1 = all_pts.max(axis=0) + 2 * RASTER_CELL
    w = int(math.ceil((x1 - x0) / RASTER_CELL))
    h = int(math.ceil((y1 - y0) / RASTER_CELL))
    r_node = np.full((h, w, RASTER_LAYERS), EMPTY_NODE, dtype=np.uint8)
    r_z = np.full((h, w, RASTER_LAYERS), np.nan, dtype=np.float32)

    for i in range(n_areas):
        ixs, iys = _fill_polygon(quad_xy[i], x0, y0, RASTER_CELL, w, h)
        if len(ixs) == 0:  # tiny area: at least mark its centroid cell
            cxy = quad_xy[i].mean(axis=0)
            ixs = np.array([int((cxy[0] - x0) / RASTER_CELL)])
            iys = np.array([int((cxy[1] - y0) / RASTER_CELL)])
            if not (0 <= ixs[0] < w and 0 <= iys[0] < h):
                continue
        for ix, iy in zip(ixs, iys):
            for layer in range(RASTER_LAYERS):
                zc = r_z[iy, ix, layer]
                if np.isnan(zc):
                    r_node[iy, ix, layer] = quad_node[i]
                    r_z[iy, ix, layer] = quad_z[i]
                    break
                if abs(zc - quad_z[i]) < 64.0:      # same storey: keep first
                    break

    # --- dilation: give empty cells the candidates of their nearest filled neighbor ---
    filled = r_node[:, :, 0] != EMPTY_NODE
    for _ in range(80):
        empty = ~filled
        if not empty.any():
            break
        grew = False
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            src = np.roll(np.roll(filled, dy, axis=0), dx, axis=1)
            if dy > 0: src[:dy, :] = False
            if dy < 0: src[dy:, :] = False
            if dx > 0: src[:, :dx] = False
            if dx < 0: src[:, dx:] = False
            take = empty & src
            if take.any():
                sy, sx = np.nonzero(take)
                r_node[sy, sx] = r_node[sy - dy, sx - dx]
                r_z[sy, sx] = r_z[sy - dy, sx - dx]
                filled[sy, sx] = True
                empty[sy, sx] = False
                grew = True
        if not grew:
            break

    return {
        "k": np.array(k),
        "node_centroid": node_centroid,
        "geo": geo,
        "quad_xy": quad_xy,
        "quad_z": quad_z,
        "quad_node": quad_node,
        "raster_meta": np.array([x0, y0, RASTER_CELL], dtype=np.float64),
        "raster_node": r_node,
        "raster_z": r_z,
    }


def build_map(nav_json_path: Path, k: int | None = None) -> dict[str, np.ndarray]:
    nav = load_nav_json(nav_json_path)
    kk = k or default_k(len(nav.area_ids))
    area_to_node = cluster_areas(nav, kk)
    return build_artifacts(nav, area_to_node)
