"""Release helper: stamp the version everywhere, then build the update manifest.

Usage:
  python scripts/release.py bump 0.2.2
      Writes the version into pyproject.toml, src-tauri/Cargo.toml,
      src-tauri/tauri.conf.json and src/demoquerycs2/__init__.py (UTF-8, no
      BOM). Run `cargo check` in src-tauri afterwards to refresh Cargo.lock.

  python scripts/release.py manifest 0.2.2 --notes "Bug fixes"
      After `cargo tauri build` (with TAURI_SIGNING_PRIVATE_KEY set to the key content):
      reads the NSIS installer + .sig from src-tauri/target/release/bundle/nsis,
      writes latest.json next to them and prints the installer SHA-256 for the
      README table. Upload BOTH installers (versioned + stable-named copy) AND
      latest.json to the GitHub release - latest.json at the stable name is what
      installed apps poll via releases/latest/download/latest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NSIS_DIR = ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"
REPO = "TheBrokas/DemoQueryCS2"

# file -> (pattern, replacement template); every pattern must match exactly once
VERSION_FILES = {
    ROOT / "pyproject.toml": (r'(?m)^version = "[^"]+"', 'version = "{v}"'),
    ROOT / "src-tauri" / "Cargo.toml": (r'(?m)^version = "[^"]+"', 'version = "{v}"'),
    ROOT / "src-tauri" / "tauri.conf.json": (r'"version": "[^"]+"', '"version": "{v}"'),
    ROOT / "src" / "demoquerycs2" / "__init__.py": (r'__version__ = "[^"]+"', '__version__ = "{v}"'),
}


def bump(version: str) -> None:
    for path, (pat, repl) in VERSION_FILES.items():
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pat, repl.format(v=version), text)
        if n != 1:
            sys.exit(f"expected exactly 1 version match in {path}, found {n}")
        path.write_text(new, encoding="utf-8", newline="\n")   # no BOM, ever
        print(f"stamped {version} in {path.relative_to(ROOT)}")
    print("now run: cargo check (in src-tauri) to refresh Cargo.lock")


def manifest(version: str, notes: str) -> None:
    installer = NSIS_DIR / f"DemoQueryCS2_{version}_x64-setup.exe"
    sig = installer.with_name(installer.name + ".sig")
    if not installer.exists() or not sig.exists():
        sys.exit(f"missing {installer.name} or its .sig in {NSIS_DIR} - run `cargo tauri build` "
                 "with TAURI_SIGNING_PRIVATE_KEY set to the KEY CONTENT (the _PATH variant "
                 "is not picked up by tauri-cli 2.11): "
                 'TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/demoquerycs2.key)"')
    data = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "windows-x86_64": {
                "signature": sig.read_text(encoding="utf-8").strip(),
                "url": f"https://github.com/{REPO}/releases/download/v{version}/{installer.name}",
            }
        },
    }
    out = NSIS_DIR / "latest.json"
    out.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")
    sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    print(f"wrote {out}")
    print(f"SHA-256 (for README + release notes): {sha}")
    print(f"upload: gh release upload v{version} \"{installer}\" \"{out}\" "
          f"and a copy named DemoQueryCS2-setup.exe")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bump")
    b.add_argument("version")
    m = sub.add_parser("manifest")
    m.add_argument("version")
    m.add_argument("--notes", default="")
    args = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        sys.exit("version must look like 0.2.2")
    if args.cmd == "bump":
        bump(args.version)
    else:
        manifest(args.version, args.notes)


if __name__ == "__main__":
    main()
