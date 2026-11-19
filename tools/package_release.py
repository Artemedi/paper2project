#!/usr/bin/env python3
"""Build the installable Paper2Project release ZIP from tracked-style sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
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
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", *FILES, *TREES], cwd=ROOT)
    names = {p.decode("utf-8", "surrogateescape") for p in tracked.split(b"\0") if p}
    if any(name not in names for name in FILES):
        raise ValueError("Release manifest contains an untracked mandatory file")
    found = [ROOT / name for name in FILES]
    for name in sorted(names - set(FILES)):
        path = ROOT / name
        if (path.suffix in {".py", ".md", ".json", ".jsonl"} and not any(
                part.startswith(".") or part in EXCLUDE for part in Path(name).parts)):
            found.append(path)
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
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            archive.writestr(info, data)
            inventory[relative] = hashlib.sha256(data).hexdigest()
        manifest_info = zipfile.ZipInfo(prefix + "MANIFEST.json", (2026, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o644 << 16
        archive.writestr(manifest_info, json.dumps(inventory, indent=2, sort_keys=True))
    print(json.dumps({"path": str(destination), "files": len(inventory), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                      "bytes": destination.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
