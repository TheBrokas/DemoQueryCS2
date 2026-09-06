# DemoQueryCS2

Sketch a Counter-Strike 2 scenario on the radar and instantly find every moment in **your own demos** that matches it — a recreation of the [ggViz paper](https://arxiv.org/abs/2107.06495) upgraded for CS2.

Draw hypothetical CT/T positions on a map, set a similarity tolerance (in game units), optionally filter by bomb state / buy type / alive counts, and get back ranked, replayable moments from your demo library. Everything runs locally; demos never leave your machine.

> **[Try the live demo](https://cs2analysis.com/demoquery/)** — in your browser, preloaded with 680+ pro matches from the 2026 season (no install needed).
>
> **[Download the app](https://github.com/TheBrokas/DemoQueryCS2/releases)** — free for players, analysts and teams; index your own demo library (Windows).

## Team cores (v1.1.0)

In local mode, open **Settings → Team cores**, name a team and select 3–5
players from your indexed demos. Players are identified by Steam ID; the picker
shows the username from their latest indexed recording (using its file date).
Every selected player must appear on the same side of a round. The remaining
teammates can vary, and the custom name overrides any recorded team name.

Saving applies to existing indexed rounds without reparsing the original demos;
future scans update membership automatically. Names feed the team filter,
result cards, scenario statistics and playback. If multiple cores match the
same side, Settings reports the overlap and that round keeps its recorded name
until the conflict is resolved. Definitions survive clearing the demo index.
These controls and mutation endpoints are unavailable in the hosted demo.

## How it works

1. **Nav clustering (build time)** — each map's navigation mesh (~2,000–4,000 walkable areas) is clustered into ~100–180 mid-sized *nodes* via deterministic agglomerative graph merging. A geodesic (walk-path) distance matrix between all nodes is precomputed, plus a raster grid for O(1) point→node lookup. See `scripts/build_map_assets.py` (needs `awpy get maps` + `awpy get navs` once, dev only — the shipped app is fully offline).
2. **Ingestion** — [demoparser2](https://github.com/LaihoE/demoparser) extracts one state per second per live round: positions, view angles, health, armor, money, inventory, flash state, sides and bomb state, plus per-round buy classification from carried equipment value, including saved weapons (≤$5,000 eco, ≤$16,250 semi, else full), bomb-site plants, kills/assists, grenade detonations with travel paths, and how each round was won. States are stored in SQLite (`cs2sf.sqlite3`) as compact tokens (sorted per-side node ids) + 18-byte-per-player position blobs (coordinates quantized to int16, which is finer than a radar pixel). Node labels ("Mid", "BombsiteA"…) are learned from the demos' own `last_place_name` data.
3. **Search** — stage 1: vectorized geodesic Chamfer scan over all states of the map (walls and floors respected — two points close through a wall are *far*); stage 2: exact optimal-assignment re-rank between your drawn markers and actual player positions, `cost = max(node geodesic, Euclidean)`. Consecutive matching seconds merge into *moments*, ranked by average per-player offset in units.
4. **Results** — snapshot cards + click-to-play playback of the matched round: match header with team names and running score, per-player panels (K/D/A, health, armor, money, inventory icons), kill feed with timestamps, muzzle flashes, and utility with true-scale smokes/mollies and travel paths. A collapsible **Scenario stats** strip aggregates the *full* match population (not just the listed cards): CT/T win rate for exact and close matches, win methods per side, a round-clock timing histogram, next-kill percentages, and buy-type mix.

## Download

**[Download DemoQueryCS2 v1.1.0](https://github.com/TheBrokas/DemoQueryCS2/releases/download/v1.1.0/DemoQueryCS2-setup.exe)** (Windows) —
free for personal and team use (see [LICENSE.txt](LICENSE.txt)).

The installer is unsigned — SmartScreen will warn; choose "More info" → "Run anyway".
Verify the download if you like with `Get-FileHash <file> -Algorithm SHA256`:

| Version | SHA-256 |
|---|---|
| 1.1.0 (`DemoQueryCS2-setup.exe`) | `130eabd6eb25c59df586c73eb40c5e443e2caf5509ea3841e1a0a77c86064e01` |

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
cargo tauri build --config packaging/unsigned-build.json  # personal build; no updater signing key required
# -> src-tauri\target\release\bundle\nsis\DemoQueryCS2_<ver>_x64-setup.exe
```

For official releases, build without the unsigned override and supply the existing
updater signing key through `TAURI_SIGNING_PRIVATE_KEY` (key content, not its
path). The key and its password must never be committed. Windows Authenticode
signing and the updater signature are separate: the updater verifies the release,
while SmartScreen still treats the executable as unsigned.

If an unattended build pauses at the key-password prompt, the finished installer
can be signed separately with `cargo tauri signer sign --private-key-path <key>`.
For a password-free key, pass an explicit empty `--password` argument using a
process launcher that preserves empty arguments (Python `subprocess.run` does).
Then run `python scripts/release.py manifest <version> --notes "..."` and verify
the signature against the public key in `src-tauri/tauri.conf.json`.

Upload the versioned installer, an identical `DemoQueryCS2-setup.exe` copy,
its `.sig`, and `latest.json` to a draft GitHub release. Check asset hashes and
manifest URLs before publishing it as the latest stable release. Update the
Download SHA-256 table above and the site's pinned download redirect afterward.
Installed apps keep their library and settings and offer the signed update.

Release verification for v1.1.0: 86 backend tests and 8 frontend regressions;
core-builder browser checks against source and the bundled Windows engine;
native WebView2 startup and Settings inspection. Tests used a separate real-library
copy: a three-player core matched 787 rounds, including query and playback labels.
Creation, editing, player limits, overlap handling, failed-save retry, deletion and
reload persistence passed. First-save backfill took about 19 seconds on that
library; later source saves took under a second.

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

Bundled: Mirage, Dust2, Inferno, Nuke (2 levels), Ancient, Anubis, Cache, Overpass, Train (2 levels), Vertigo (2 levels), Italy, Office. Custom radar images can be dropped in `maps_override/`.

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
