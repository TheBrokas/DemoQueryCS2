"""Runtime access to bundled map assets: radar, calibration, node artifacts, point->node lookup."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config
from .navcluster import EMPTY_NODE


@lru_cache(maxsize=1)
def calibrations() -> dict:
    out: dict = {}
    for base in (config.ASSETS_DIR, config.MAPS_OVERRIDE_DIR):
        f = base / "map_data.json"
        if f.exists():
            out.update(json.loads(f.read_text()))
    # per-map override jsons: maps_override/de_x.json
    if config.MAPS_OVERRIDE_DIR.exists():
        for f in config.MAPS_OVERRIDE_DIR.glob("de_*.json"):
            try:
                out[f.stem] = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return out


def radar_path(map_name: str, level: str = "upper") -> Path | None:
    suffix = "" if level == "upper" else "_lower"
    for base in (config.MAPS_OVERRIDE_DIR, config.ASSETS_DIR):
        p = base / f"{map_name}{suffix}.png"
        if p.exists():
            return p
    return None


class MapNodes:
    """Node artifacts for one map, with point->node lookup."""

    def __init__(self, npz_path: Path, calibration: dict | None):
        data = np.load(npz_path)
        self.k = int(data["k"])
        self.node_centroid = data["node_centroid"]
        self.geo = data["geo"]                      # (K,K) float32
        self.quad_xy = data["quad_xy"]
        self.quad_z = data["quad_z"]
        self.quad_node = data["quad_node"]
        self.x0, self.y0, self.cell = data["raster_meta"]
        self.raster_node = data["raster_node"]      # (H,W,3) uint8
        self.raster_z = data["raster_z"]            # (H,W,3) float32
        self.h, self.w = self.raster_node.shape[:2]
        cal = calibration or {}
        lm = cal.get("lower_level_max_units")
        self.lower_max = lm if lm is not None and lm > -999999 else None
        # geo table padded so node id 255 (empty token slot) gathers as +cap
        self.geo_padded = np.full((256, 256), float(self.geo.max()), dtype=np.float32)
        self.geo_padded[: self.k, : self.k] = self.geo

    def _cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        ix = int((x - self.x0) / self.cell)
        iy = int((y - self.y0) / self.cell)
        if 0 <= ix < self.w and 0 <= iy < self.h:
            return ix, iy
        return None

    def node_at(self, x: float, y: float, z: float | None = None, level: str | None = None) -> int | None:
        cell = self._cell_of(x, y)
        if cell is None:
            return None
        ix, iy = cell
        nodes = self.raster_node[iy, ix]
        zs = self.raster_z[iy, ix]
        valid = nodes != EMPTY_NODE
        if not valid.any():
            return None
        nodes, zs = nodes[valid], zs[valid]
        if z is not None and len(nodes) > 1:
            return int(nodes[np.argmin(np.abs(zs - z))])
        if level is not None and self.lower_max is not None and len(nodes) > 1:
            want_lower = level == "lower"
            mask = (zs < self.lower_max) == want_lower
            if mask.any():
                nodes, zs = nodes[mask], zs[mask]
        return int(nodes[0])

    def nodes_at_bulk(self, xyz: np.ndarray) -> np.ndarray:
        """Vectorized (n,3) -> (n,) node ids (EMPTY_NODE when off-grid)."""
        ix = ((xyz[:, 0] - self.x0) / self.cell).astype(np.int64)
        iy = ((xyz[:, 1] - self.y0) / self.cell).astype(np.int64)
        ok = (ix >= 0) & (ix < self.w) & (iy >= 0) & (iy < self.h)
        out = np.full(len(xyz), EMPTY_NODE, dtype=np.uint8)
        if not ok.any():
            return out
        cand_nodes = self.raster_node[iy[ok], ix[ok]]        # (m,3)
        cand_z = self.raster_z[iy[ok], ix[ok]]               # (m,3)
        dz = np.abs(np.nan_to_num(cand_z, nan=1e9) - xyz[ok, 2:3])
        dz[cand_nodes == EMPTY_NODE] = 1e9
        pick = np.argmin(dz, axis=1)
        out[ok] = cand_nodes[np.arange(len(pick)), pick]
        return out

    def is_lower(self, z: float) -> bool:
        return self.lower_max is not None and z < self.lower_max


@lru_cache(maxsize=16)
def get_nodes(map_name: str) -> MapNodes | None:
    p = config.ASSETS_DIR / f"nodes_{map_name}.npz"
    if not p.exists():
        return None
    return MapNodes(p, calibrations().get(map_name))


def available_maps() -> list[str]:
    return sorted(p.stem.replace("nodes_", "") for p in config.ASSETS_DIR.glob("nodes_*.npz"))
