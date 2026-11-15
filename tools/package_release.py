#!/usr/bin/env python3
"""Build the installable Paper2Project release ZIP from tracked-style sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from paper2project import __version__  # noqa: E402

FILES = [
    "pyproject.toml",
    "LICENSE",
    "NOTICE.md",
    "licenses/Paper2Agent-MIT.txt",
    "docs/product/README.md",
    "docs/product/PRODUCT.md",
    f"docs/RELEASE-NOTES-{__version__}.md",
]
TREES = ["src/paper2project", "examples/kvset-demo"]
EXCLUDE = {"__pycache__", ".pyc"}


def collect():
    found = [ROOT / name for name in FILES]
    for tree in TREES:
        found += [p for p in sorted((ROOT / tree).rglob("*")) if p.is_file() and not (EXCLUDE & set(p.parts)) and p.suffix != ".pyc"]
    return found


def main():
    destination = ROOT / "dist" / f"paper2project-{__version__}.zip"
    destination.parent.mkdir(exist_ok=True)
    prefix = f"paper2project-{__version__}/"
    inventory = {}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in collect():
            if path.is_symlink():
                raise SystemExit("Refusing symlink: " + str(path))
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(prefix + relative, (2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
            inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive.writestr(zipfile.ZipInfo(prefix + "MANIFEST.json", (2026, 1, 1, 0, 0, 0)), json.dumps(inventory, indent=2, sort_keys=True))
    print(json.dumps({"path": str(destination), "files": len(inventory), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                      "bytes": destination.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
