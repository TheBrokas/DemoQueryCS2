"""Path resolution for dev vs frozen (PyInstaller/installed) runs."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def _resolve_data_dir() -> Path:
    """First writable candidate wins.

    Deliberately avoids Documents: it can be redirected to a missing drive, and
    Windows Controlled Folder Access flags unsigned apps writing there
    ("unauthorized changes blocked" on every launch). LocalAppData is never
    a protected folder; the UI exposes an "open demos folder" button instead.
    """
    candidates: list[Path] = []
    env = os.environ.get("CS2SF_DATA_DIR")
    if env:
        candidates.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "DemoQueryCS2")
    candidates.append(Path.home() / "DemoQueryCS2")
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except OSError:
            continue
    return candidates[-1]


if FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PKG_DIR = BUNDLE_DIR / "demoquerycs2"
    APP_DIR = _resolve_data_dir()
else:
    PKG_DIR = Path(__file__).parent
    APP_DIR = PKG_DIR.parent.parent                # project root (src/demoquerycs2 -> root)

ASSETS_DIR = PKG_DIR / "assets" / "maps"
STATIC_DIR = PKG_DIR / "web" / "static"

# User settings (currently just a demos-folder override) live next to the DB in
# the writable data dir. The DB, cache and extracted-archive scratch always stay
# in APP_DIR; only the demos *read* location is redirectable.
SETTINGS_PATH = APP_DIR / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


_settings = _load_settings()

DEFAULT_DEMOS_DIR = APP_DIR / "demos"
_demos_override = _settings.get("demos_dir")
DEMOS_DIR = Path(_demos_override) if _demos_override else DEFAULT_DEMOS_DIR
MAPS_OVERRIDE_DIR = APP_DIR / "maps_override"
CACHE_DIR = APP_DIR / "cache"
_db_env = os.environ.get("CS2SF_DB_PATH")
DB_PATH = Path(_db_env) if _db_env else APP_DIR / "cs2sf.sqlite3"

DEFAULT_PORT = 8642

# demo mode: read-only public instance (web demo on cs2analysis.com)
DEMO_MODE = os.environ.get("CS2SF_DEMO") == "1"
DOWNLOAD_URL = os.environ.get("CS2SF_DOWNLOAD_URL", "https://cs2analysis.com/demoquery/download")
# public page share links point at (the site page embedding the web demo)
SHARE_BASE = os.environ.get("CS2SF_SHARE_BASE", "https://cs2analysis.com/demoquery/")

# launch-time update check (the app's only network call); off via settings or env
GITHUB_REPO = "TheBrokas/DemoQueryCS2"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK_DISABLED = os.environ.get("CS2SF_NO_UPDATE_CHECK") == "1"
TOKENIZER_VERSION = 10     # v6: grenades; v7: coach-proof roster; v8: economy/inventory/flash
                           # per state (int16 xyz, same 18B slot), shots, kill assists;
                           # v9: stable per-round sides for terminal kill events;
                           # v10: buy types use carried equipment (saved guns included)

ROUND_CLOCK_S = 115        # competitive round clock (1:55); the bomb timer replaces it after plant

MAX_PLAYERS_PER_SIDE = 5

# Active pool appears first in selectors; supported inactive maps remain available below it.
ACTIVE_DUTY_MAPS = {
    "de_ancient", "de_anubis", "de_cache", "de_dust2",
    "de_inferno", "de_mirage", "de_nuke",
}
LEGACY_MAPS = {"de_overpass"}
SEARCHABLE_MAPS = ACTIVE_DUTY_MAPS | LEGACY_MAPS


def ensure_dirs() -> None:
    for d in (DEMOS_DIR, MAPS_OVERRIDE_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_ui_settings() -> dict:
    ui = _settings.get("ui")
    return dict(ui) if isinstance(ui, dict) else {}


def update_ui_settings(values: dict) -> dict:
    ui = get_ui_settings()
    ui.update(values)
    _settings["ui"] = ui
    _save_settings(_settings)
    return ui


def is_default_demos_dir() -> bool:
    return DEMOS_DIR == DEFAULT_DEMOS_DIR


def set_demos_dir(path: str | os.PathLike | None) -> Path:
    """Point the app at a different demos folder (empty/None resets to default).

    Reassigns the live globals so a running server picks it up without a restart
    (every reader accesses ``config.DEMOS_DIR`` at call time) and persists the
    choice to settings.json. Raises OSError if the folder can't be created/used.
    """
    global DEMOS_DIR
    text = str(path).strip() if path is not None else ""
    if text:
        new = Path(text).expanduser()
        new.mkdir(parents=True, exist_ok=True)   # validates it exists / is writable
        _settings["demos_dir"] = str(new)
    else:
        new = DEFAULT_DEMOS_DIR
        _settings.pop("demos_dir", None)
    DEMOS_DIR = new
    _save_settings(_settings)
    ensure_dirs()
    return DEMOS_DIR
