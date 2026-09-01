"""Launch-time update check against GitHub releases (the app's only network call).

Runs once, in a background thread, with a short timeout; any failure (offline,
rate limit, bad JSON) is silent - the app must never depend on the network.
Disabled in demo mode, by the settings toggle, or by CS2SF_NO_UPDATE_CHECK=1.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request

from . import __version__, config

_TIMEOUT_S = 4.0
_API_URL = f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest"

_lock = threading.Lock()
_result: dict = {"checked": False, "available": False,
                 "current": __version__, "latest": None, "url": config.RELEASES_URL}


def _ver_tuple(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    lt, ct = _ver_tuple(latest), _ver_tuple(current)
    return lt is not None and ct is not None and lt > ct


def _run() -> None:
    tag = None
    try:
        req = urllib.request.Request(_API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"DemoQueryCS2/{__version__}",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data.get("tag_name"), str):
            tag = data["tag_name"]
    except Exception:  # noqa: BLE001 - stay silent on any failure
        pass
    with _lock:
        _result["checked"] = True
        if tag:
            _result["latest"] = tag.lstrip("v")
            _result["available"] = is_newer(tag, __version__)


def enabled() -> bool:
    if config.DEMO_MODE or config.UPDATE_CHECK_DISABLED:
        return False
    return bool(config.get_ui_settings().get("check_updates", True))


def start() -> None:
    """Fire the one-shot background check at boot; no-op when disabled."""
    if not enabled():
        with _lock:
            _result["checked"] = True
        return
    threading.Thread(target=_run, daemon=True, name="update-check").start()


def status() -> dict:
    with _lock:
        return dict(_result)
