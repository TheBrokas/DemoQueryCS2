"""Dev-time build step: produce bundled map assets from awpy data + user radar images.

Prereq (once, with internet): pip install awpy && awpy get maps && awpy get navs
Then: python scripts/build_map_assets.py [--k K] [--maps de_mirage,de_nuke]

Outputs into src/demoquerycs2/assets/maps/:
  de_<map>.png (+ _lower), map_data.json, nodes_de_<map>.npz
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from demoquerycs2 import navcluster  # noqa: E402

AWPY_DIR = Path.home() / ".awpy"
RADAR_USER_DIR = ROOT / "radar_images"
OUT_DIR = ROOT / "src" / "demoquerycs2" / "assets" / "maps"

KEEP_CAL_KEYS = ("pos_x", "pos_y", "scale", "lower_level_max_units")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=None, help="override node count for all maps")
    ap.add_argument("--maps", type=str, default=None, help="comma-separated subset")
    args = ap.parse_args()

    map_data_file = AWPY_DIR / "maps" / "map-data.json"
    if not map_data_file.exists():
        sys.exit("awpy data missing - run: pip install awpy && awpy get maps && awpy get navs")
    all_cal = json.loads(map_data_file.read_text())

    nav_files = {p.stem: p for p in (AWPY_DIR / "navs").glob("*.json")}
    subset = set(args.maps.split(",")) if args.maps else None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # a --maps subset run must not wipe other maps' calibration from the
    # bundled map_data.json - start from what is already shipped
    out_file = OUT_DIR / "map_data.json"
    out_cal: dict = json.loads(out_file.read_text()) if out_file.exists() else {}
    summary = []

    for map_name, nav_path in sorted(nav_files.items()):
        if subset and map_name not in subset:
            continue
        if not map_name.startswith(("de_", "cs_")):
            continue
        cal = all_cal.get(map_name)
        if cal is None:
            summary.append((map_name, "SKIP: no calibration"))
            continue

        # radar: user-supplied preferred, then awpy
        radar_found = False
        for level_suffix in ("", "_lower"):
            user = RADAR_USER_DIR / f"{map_name}{level_suffix}_radar.png"
            awpy_png = AWPY_DIR / "maps" / f"{map_name}{level_suffix}.png"
            src = user if user.exists() else (awpy_png if awpy_png.exists() else None)
            if src is not None:
                shutil.copyfile(src, OUT_DIR / f"{map_name}{level_suffix}.png")
                if level_suffix == "":
                    radar_found = True
        if not radar_found:
            summary.append((map_name, "SKIP: no radar image"))
            continue

        t0 = time.time()
        try:
            nav = navcluster.load_nav_json(nav_path)
            k = args.k or navcluster.default_k(len(nav.area_ids))
            area_to_node = navcluster.cluster_areas(nav, k)
            art = navcluster.build_artifacts(nav, area_to_node)
        except Exception as e:  # noqa: BLE001
            summary.append((map_name, f"FAIL: {e}"))
            continue
        import numpy as np
        np.savez_compressed(OUT_DIR / f"nodes_{map_name}.npz", **art)
        out_cal[map_name] = {kk: cal[kk] for kk in KEEP_CAL_KEYS if kk in cal and cal[kk] is not None}
        summary.append((map_name, f"ok: {len(nav.area_ids)} areas -> {int(art['k'])} nodes, "
                                  f"raster {art['raster_node'].shape[1]}x{art['raster_node'].shape[0]}, "
                                  f"{time.time()-t0:.1f}s"))

    out_file.write_text(json.dumps(out_cal, indent=1))
    print(f"\nassets -> {OUT_DIR}\n")
    for name, status in summary:
        print(f"  {name:20s} {status}")


if __name__ == "__main__":
    main()
