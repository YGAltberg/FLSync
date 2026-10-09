#!/usr/bin/env python3
"""Packs FLSync into one zip to send to the others: unpack it, double-click
FLSync.exe, click Installera, done. FLSync.exe is built first if its source
is newer (scripts/build_exe.py).

    python scripts/make_zip.py            -> dist/FLSync.zip
    python scripts/make_zip.py --out X.zip
"""

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ("app", "bridge", "relay", "tools", "fl", "setup", "docs")
FILES = ("FLSync.exe", "README.md", "LICENSE", "CHANGELOG.md")  # no .cmd: FLSync.exe opens no console
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".part")
TOP = "FLSync"  # the folder the zip unpacks to


def entries(root=ROOT):
    """(path on disk, name in the zip) for everything that goes in."""
    for name in FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            raise SystemExit("Saknas: %s" % path)
        yield path, "%s/%s" % (TOP, name)
    for folder in FOLDERS:
        base = os.path.join(root, folder)
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                if name.endswith(SKIP_SUFFIXES) or name.startswith("."):
                    continue
                path = os.path.join(current, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                yield path, "%s/%s" % (TOP, rel)


def make(out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in entries():
            z.write(path, name)
            count += 1
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="Packa FLSync i en zip att skicka till de andra")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "FLSync.zip"))
    args = parser.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_exe
    if build_exe.outdated():
        print("Byggde %s" % build_exe.build())
    count = make(args.out)
    print("Klart: %s (%d filer, %.1f MB)" % (args.out, count, os.path.getsize(args.out) / 1e6))
    print("Skicka den till de andra: de packar upp den och dubbelklickar på FLSync.exe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
