# PyInstaller spec: DemoQueryCS2 server sidecar (onedir).
# Build: .venv\Scripts\python -m PyInstaller packaging\cs2sf-server.spec --noconfirm --distpath dist-server
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all

block_cipher = None

# demoparser2's Rust core loads these lazily at parse time (polars for the
# dataframe engine, pyarrow for the Rust->pandas handoff, tqdm for progress) -
# PyInstaller's static analysis never sees them, so collect each in full or the
# frozen exe panics with "No module named 'polars'" the moment a demo is parsed.
_lazy_datas, _lazy_bins, _lazy_hidden = [], [], []
for _pkg in ("polars", "pyarrow", "tqdm"):
    _d, _b, _h = collect_all(_pkg)
    _lazy_datas += _d
    _lazy_bins += _b
    _lazy_hidden += _h

a = Analysis(
    ["server_entry.py"],
    pathex=["../src"],
    binaries=collect_dynamic_libs("demoparser2") + _lazy_bins,
    datas=[
        ("../src/demoquerycs2/web/static", "demoquerycs2/web/static"),
        ("../src/demoquerycs2/assets", "demoquerycs2/assets"),
        ("../THIRD_PARTY_LICENSES.txt", "."),
    ] + _lazy_datas,
    hiddenimports=[
        "demoquerycs2.web.app",          # imported by string via uvicorn.run
        "demoquerycs2.folderpick",       # imported lazily inside the pick-folder endpoint
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ] + _lazy_hidden,
    excludes=["matplotlib", "tkinter", "IPython", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cs2sf-server",
    console=True,          # spawned with CREATE_NO_WINDOW by the Tauri shell
    disable_windowed_traceback=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="cs2sf-server",
)
