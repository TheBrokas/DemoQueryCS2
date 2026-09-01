"""Hosted-demo entry point (Railway): run the server straight from the source tree.

The app package lives in src/ and is not pip-installed in the container, so this
shim puts src/ on sys.path before importing. See deploy/README.md.

The demo library (demo.sqlite3, ~67 MB) is NOT committed to the repo - it is a
GitHub Release asset, fetched here at boot if the DB file is missing. Override
the source with CS2SF_DB_URL; on fetch failure the app still starts (empty).
"""
import os
import sys
import time
import urllib.request
from pathlib import Path

# numpy's BLAS never runs in this workload; keep its thread pools (one arena +
# stack each) from spawning. Must be set before numpy is first imported.
# MALLOC_ARENA_MAX=2 lives in the Procfile - glibc reads it before Python starts.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

sys.path.insert(0, str(Path(__file__).parent / "src"))

DEFAULT_DB_URL = ("https://github.com/TheBrokas/DemoQueryCS2/releases/download/"
                  "demo-db/demo.sqlite3")


def _remote_size(url: str) -> int | None:
    """Byte size of the published library, or None if the check itself failed.

    Asks for a single byte rather than issuing a HEAD: GitHub's release-asset CDN
    closes HEAD connections without responding, but honours Range and reports the
    full size in Content-Range. Servers that ignore Range answer 200 with a plain
    Content-Length instead; either way the body is never read.
    """
    for attempt in range(3):                        # the CDN drops a connection now and then
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                crange = r.headers.get("Content-Range", "")   # "bytes 0-0/633503744"
                total = crange.rsplit("/", 1)[-1].strip() if "/" in crange else ""
                if total.isdigit():
                    return int(total)
                length = r.headers.get("Content-Length")
                return int(length) if length and r.status == 200 else None
        except Exception as e:  # noqa: BLE001 - fall back to whatever is on disk
            print(f"WARNING: db size check failed ({attempt + 1}/3): {e}", flush=True)
            time.sleep(2 * attempt + 1)
    return None


def _fetch_demo_db() -> None:
    db = os.environ.get("CS2SF_DB_PATH")
    url = os.environ.get("CS2SF_DB_URL", DEFAULT_DB_URL)
    if not db or not url:
        return
    local = Path(db)
    if local.exists():
        # A file on disk is not necessarily the current library: the container
        # filesystem can outlive an upload (persistent volume, or a restart that
        # keeps the writable layer), which silently pins the demo to an old DB.
        # Re-fetch whenever the published asset is a different size.
        have, want = local.stat().st_size, _remote_size(url)
        if want is None or want == have:
            return
        print(f"demo db stale ({have} bytes on disk, {want} published) - refetching", flush=True)
    else:
        print(f"demo db missing - fetching {url}", flush=True)
    tmp = local.with_suffix(".download")
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as out:
            while chunk := r.read(1 << 20):
                out.write(chunk)
        tmp.replace(local)                     # replaces the stale copy atomically
        print(f"demo db ready ({local.stat().st_size / 1e6:.0f} MB)", flush=True)
    except Exception as e:  # noqa: BLE001 - demo still boots on whatever is there
        print(f"WARNING: demo db fetch failed: {e}", flush=True)
        tmp.unlink(missing_ok=True)


from demoquerycs2.__main__ import main  # noqa: E402

if __name__ == "__main__":
    _fetch_demo_db()
    main()
