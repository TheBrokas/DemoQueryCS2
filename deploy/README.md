# Hosted web demo (Railway)

The public demo at cs2analysis.com/demoquery is this app in read-only demo mode,
deployed as a Railway service from this repo.

## Railway service settings

- **Source**: this GitHub repo, branch `main`. Railpack detects `requirements.txt`
  (explicit deps — NOT `-e .`, railpack installs requirements before copying the
  source tree) + `Procfile` (`python serve.py ...` — a root shim that puts src/
  on sys.path itself; Procfile env-var prefixes are not reliably honored) +
  `runtime.txt`.
- **Environment variables**:
  - `CS2SF_DEMO=1` — read-only demo mode (ingest endpoints 403, rate limits on,
    UI shows the download CTA instead of scan controls)
  - `CS2SF_DB_PATH=/app/deploy/demo.sqlite3` — where the library lands at boot
  - `CS2SF_DATA_DIR=/tmp/demoquery` — scratch dirs on the ephemeral disk
  - `CS2SF_DB_URL` (optional) — where serve.py fetches the library from; defaults
    to the `demo-db` GitHub Release asset
  - `CS2SF_DOWNLOAD_URL=https://cs2analysis.com/demoquery/download` (optional; default)

## Demo library hosting

The ~600 MB `demo.sqlite3` is **not** committed (purged from git history to keep the
public repo lean). It lives as a GitHub Release asset under the `demo-db` tag;
`serve.py` downloads it at boot when `CS2SF_DB_PATH` is missing, **or when the file
on disk is a different size than the published asset**. That second condition
matters: a container's filesystem can outlive an upload (a restart that keeps the
writable layer, or a persistent volume), and a plain "file exists" check silently
pins the demo to an old library — that bit us on 2026-08-12, when two redeploys
after a `--clobber` kept serving the previous build.

## Updating the demo library

Re-ingest locally, checkpoint-copy the DB, and replace the release asset:

```powershell
python -c "import sqlite3; s=sqlite3.connect('cs2sf.sqlite3'); d=sqlite3.connect('deploy/demo.sqlite3'); s.backup(d)"
gh release upload demo-db deploy/demo.sqlite3 --clobber
```

Then boot a container: redeploy in the Railway dashboard, or push a commit. Verify
with `/api/maps` on the service — the summed `n_demos` must match the new library,
because the boot log is the only other place the refetch is visible.

## Embedding

The site page (cs2analysis repo, `demoquery/index.html`) iframes this service's
public URL. FastAPI sends no X-Frame-Options, so embedding works by default.
