"""Entry point: start the server; open a browser in dev, stay headless as a sidecar."""
from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn

from . import config


def _pick_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _open_browser(url: str) -> None:
    import urllib.request
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    webbrowser.open(url)


def main() -> None:
    import multiprocessing
    multiprocessing.freeze_support()   # required for parallel scans in a frozen exe

    ap = argparse.ArgumentParser(prog="demoquerycs2")
    ap.add_argument("--port", type=int, default=0,
                    help="bind to this exact port (default: first free port from 8642)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (0.0.0.0 for hosted demo mode)")
    ap.add_argument("--headless", action="store_true",
                    help="do not open a browser window (sidecar/server mode)")
    args = ap.parse_args()

    config.ensure_dirs()
    import os
    env_port = 0
    try:
        env_port = int(os.environ.get("PORT", "0"))   # hosted platforms (Railway) set PORT
    except ValueError:
        pass
    port = args.port or env_port or _pick_port(config.DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}"
    mode = " [demo mode]" if config.DEMO_MODE else ""
    print(f"DemoQueryCS2{mode} -> {url}   (data folder: {config.APP_DIR})", flush=True)
    if not args.headless:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    uvicorn.run("demoquerycs2.web.app:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
