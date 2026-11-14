#!/usr/bin/env python3
"""Build the HicksField zip from this tree.

Exec bits carry the file-type bits (0o100755 scripts, 0o100644 plain
files), or GUI extractors drop the execute bit and the installer
refuses to run. The zip lands next to this tree, named with the app
version from the package.
"""

import hashlib
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOP = "HicksField"
EXEC_NAMES = {"install.sh", "run.sh", "installer.py", "build_zip.py"}
SKIP_DIRS = {"__pycache__", ".git", "venv", ".mypy_cache"}
SKIP_SUFFIXES = (".pyc", ".zip")


def version():
    text = (ROOT / "hicksfield" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def collect():
    files = []
    for path in sorted(ROOT.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def build():
    ver = version()
    out = ROOT.parent / f"hicksfield-v{ver}.zip"
    files = collect()
    dirs = set()
    for path in files:
        for parent in path.relative_to(ROOT).parents:
            if str(parent) != ".":
                dirs.add(str(parent))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in sorted(dirs):
            info = zipfile.ZipInfo(TOP + "/" + directory + "/")
            info.external_attr = (0o040755 & 0xFFFF) << 16
            archive.writestr(info, "")
        for path in files:
            relative = path.relative_to(ROOT)
            arcname = TOP + "/" + str(relative)
            info = zipfile.ZipInfo(arcname)
            if path.name in EXEC_NAMES:
                info.external_attr = (0o100755 & 0xFFFF) << 16
            else:
                info.external_attr = (0o100644 & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read())
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    size = out.stat().st_size
    print(f"zip: {out}")
    print(f"files: {len(files)}  dirs: {len(dirs)}  "
          f"entries: {len(files) + len(dirs)}")
    print(f"size: {size} bytes")
    print(f"sha256: {digest}")
    return out


if __name__ == "__main__":
    build()
