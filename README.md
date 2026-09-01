# DemoQueryCS2

Sketch a Counter-Strike 2 scenario on the radar and instantly find every moment in **your own demos** that matches it — a recreation of the [ggViz paper](https://arxiv.org/abs/2107.06495) upgraded for CS2.

Draw hypothetical CT/T positions on a map, set a similarity tolerance (in game units), optionally filter by bomb state / buy type / alive counts, and get back ranked, replayable moments from your demo library. Everything runs locally; demos never leave your machine.

> **[Try the live demo](https://cs2analysis.com/demoquery/)** — in your browser, preloaded with 680+ pro matches from the 2026 season (no install needed).
>
> **[Download the app](https://github.com/TheBrokas/DemoQueryCS2/releases)** — free for players, analysts and teams; index your own demo library (Windows).

## How it works

1. **Nav clustering (build time)** — each map's navigation mesh (~2,000–4,000 walkable areas) is clustered into ~100–180 mid-sized *nodes* via deterministic agglomerative graph merging. A geodesic (walk-path) distance matrix between all nodes is precomputed, plus a raster grid for O(1) point→node lookup. See `scripts/build_map_assets.py` (needs `awpy get maps` + `awpy get navs` once, dev only — the shipped app is fully offline).
2. **Ingestion** — [demoparser2](https://github.com/LaihoE/demoparser) extracts one state per second per live round: positions, view angles, health, armor, money, inventory, flash state, sides and bomb state, plus per-round buy classification from carried equipment value, including saved weapons (≤$5,000 eco, ≤$16,250 semi, else full), bomb-site plants, kills/assists, grenade detonations with travel paths, and how each round was won. States are stored in SQLite (`cs2sf.sqlite3`) as compact tokens (sorted per-side node ids) + 18-byte-per-player position blobs (coordinates quantized to int16, which is finer than a radar pixel). Node labels ("Mid", "BombsiteA"…) are learned from the demos' own `last_place_name` data.
3. **Search** — stage 1: vectorized geodesic Chamfer scan over all states of the map (walls and floors respected — two points close through a wall are *far*); stage 2: exact optimal-assignment re-rank between your drawn markers and actual player positions, `cost = max(node geodesic, Euclidean)`. Consecutive matching seconds merge into *moments*, ranked by average per-player offset in units.
4. **Results** — snapshot cards + click-to-play playback of the matched round: match header with team names and running score, per-player panels (K/D/A, health, armor, money, inventory icons), kill feed with timestamps, muzzle flashes, and utility with true-scale smokes/mollies and travel paths. A collapsible **Scenario stats** strip aggregates the *full* match population (not just the listed cards): CT/T win rate for exact and close matches, win methods per side, a round-clock timing histogram, next-kill percentages, and buy-type mix.

## Download

Latest Windows installer: **[Releases](https://github.com/TheBrokas/DemoQueryCS2/releases)** —
free for personal and team use (see [LICENSE.txt](LICENSE.txt)).

| Version | SHA-256 |
|---|---|
| 0.3.0 (`DemoQueryCS2_0.3.0_x64-setup.exe`) | `40f3b9722291dcff19e5f260d334df86b68626b0b91d7960e7a60584ccb3e1c1` |
| 0.2.2 (`DemoQueryCS2_0.2.2_x64-setup.exe`) | `8000baf5ca9d9becdcb2b98171c3aa54af4315cdef3c6df66548eddf3666c828` |
| 0.2.1 (`DemoQueryCS2_0.2.1_x64-setup.exe`) | `1143b7f311293ddd991314862097587d9ce631cf57fe474fbd5653bd3d5a8158` |
| 0.2.0 (`DemoQueryCS2_0.2.0_x64-setup.exe`) | `cfd4e6276dd2aa6a41d25c5885e119c051e7a47b25b54251260713d71c5ba490` |
| 0.1.0 (`DemoQueryCS2_0.1.0_x64-setup.exe`) | `c09748daf45df526c9ade87a9ebd0ae44934d7dd4bff64b27caa8e810fef478a` |

Verify after downloading: `Get-FileHash <file> -Algorithm SHA256`. The installer is
unsigned — SmartScreen will warn; choose "More info" → "Run anyway".

**Updates:** the app offers new versions at launch (one-click install-and-restart,
signed update manifest; your demo library and settings are kept). It asks
github.com once per launch whether a newer release exists — the app's only
network call; parsing and search stay fully offline. Disable in Settings or with
`CS2SF_NO_UPDATE_CHECK=1`.

Prefer not to run a downloaded unsigned exe? You can run or build the app from
source instead — see [Quick start](#quick-start-development) (run directly, no
exe involved) or [Desktop app](#desktop-app-what-teams-get) (build your own
installer). Self-built binaries carry no Mark-of-the-Web, so SmartScreen does
not warn. Personal/team builds are expressly permitted by
[LICENSE.txt](LICENSE.txt) § 3a; redistributing the code or your builds is not.

Using the app: pick (or open) your demos folder, drop `.dem` files in, click
**Scan folder for new demos**, then draw and search. Compressed demos
(`.rar`/`.zip`/`.zst`) need extracting first — the folder takes plain `.dem` files.

## Desktop app (what teams get)

A Tauri-based native Windows app: `DemoQueryCS2_x64-setup.exe` → install wizard → Start Menu/desktop icon → the app opens in its own window (WebView2, no browser). The Python engine runs as a bundled sidecar process, auto-killed on window close. User data (demos folder, database, caches) lives in `%LOCALAPPDATA%\DemoQueryCS2\` (override with `CS2SF_DATA_DIR`; deliberately not Documents — Controlled Folder Access flags unsigned apps writing there). The demos folder is user-selectable in Settings. Dev args: `cs2sf-server.exe --headless --port N`.

Build it:

```powershell
.venv\Scripts\python -m PyInstaller packaging\cs2sf-server.spec --noconfirm --distpath dist-server
cargo tauri build        # needs rustup (MSVC) + VS Build Tools; NSIS fetched automatically
# -> src-tauri\target\release\bundle\nsis\DemoQueryCS2_<ver>_x64-setup.exe
```

Per release: compute the installer's SHA-256 (`Get-FileHash`), update the Download
table above, and upload the installer as a GitHub Release asset. Code signing
(Azure Trusted Signing ~$10/mo) can remove the SmartScreen warning later.

## Quick start (development)

```powershell
# once
python -m venv .venv
.venv\Scripts\python -m pip install -e .

# run (opens a browser tab; the packaged app uses a native window instead)
.venv\Scripts\python -m demoquerycs2
```

Drop `.dem` files into the `demos/` folder, click **Scan folder for new demos**,
then draw and search.

## Verified behavior (real BLAST/IEM/StarLadder demos)

- Self-retrieval: sketching a stored state's exact positions returns its round at rank 1 with score 0 (15/15).
- Boundary robustness: ±40-unit perturbed sketches still retrieve the source round in the top 3 (15/15) — the failure mode of place-token approaches this design eliminates.
- Full 5v5 query over a 14k-state map: ~70–140 ms; stage-1 scan scales to 1M states in ~150 ms.
- Parsing: 44 demos (~14 GB) in 30 s on 8 workers (~0.7 s/demo effective; ~2.5 s/demo single-threaded). Parsing runs in a process pool — set `CS2SF_WORKERS` to override the worker count (default: cores−3, max 8).

## Maps

Bundled: Mirage, Dust2, Inferno, Nuke (2 levels), Ancient, Anubis, Overpass, Train (2 levels), Vertigo (2 levels), Italy, Office. Cache has no public nav-mesh data yet, so Cache demos are skipped with a clear error. Custom radar images can be dropped in `maps_override/`.

## Development

```powershell
.venv\Scripts\python -m pytest tests -q        # unit tests
python scripts\build_map_assets.py             # rebuild map assets (dev; needs awpy data)
python scripts\build_map_assets.py --k 150     # override node count
```

Layout: `src/demoquerycs2/` — `navcluster.py` (clustering + geodesics + raster), `ingest/` (scanner, demo parser, tokenizer, buy types), `search/engine.py` (two-stage retrieval), `web/` (FastAPI + vanilla JS canvas UI), `assets/maps/` (bundled radars, calibration, node artifacts).

## License

**© 2026 Rokas Dargis / CS2Analysis.** The app is **free to download and use** for
personal and team use — see [LICENSE.txt](LICENSE.txt) for the full terms (provided
"as is", no warranty). The source code is source-available, not open source: you may
clone and build it for your own personal/team use (LICENSE § 3a), but no license to
modify or redistribute the code or your builds is granted.

Third-party open-source components are used under their own licenses — see
[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt) (bundled with every build). Radar
imagery and weapon/HUD iconography derive from Valve's Counter-Strike 2 game assets;
Valve Corporation retains all rights to those assets, and this project is not affiliated
with or endorsed by Valve.
