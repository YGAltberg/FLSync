#!/usr/bin/env python3
"""Builds FLSync.exe from launcher/FLSync.cs with the C# compiler that comes
with Windows (.NET Framework 4: csc.exe under %WINDIR%\\Microsoft.NET). The
icon is app/flsync.ico, the version app/version.py's. make_zip.py calls this
when the exe is older than its sources.

    python scripts/build_exe.py            -> FLSync.exe next to the README
"""

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "launcher", "FLSync.cs")
ICON = os.path.join(ROOT, "app", "flsync.ico")
VERSION_FILE = os.path.join(ROOT, "app", "version.py")
EXE = os.path.join(ROOT, "FLSync.exe")


def version():
    """app/version.py's VERSION ("0.9.0"), read without importing it."""
    with open(VERSION_FILE, encoding="utf-8") as f:
        found = re.search(r'^VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', f.read(), re.MULTILINE)
    if not found:
        raise SystemExit("Ingen VERSION = \"X.Y.Z\" i %s" % VERSION_FILE)
    return ".".join(found.groups())


def compiler():
    windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    for framework in ("Framework64", "Framework"):
        path = os.path.join(windows, "Microsoft.NET", framework, "v4.0.30319", "csc.exe")
        if os.path.isfile(path):
            return path
    return None


def outdated():
    """Whether FLSync.exe is missing or older than its sources."""
    if not os.path.isfile(EXE):
        return True
    built = os.path.getmtime(EXE)
    return any(os.path.getmtime(path) > built for path in (SOURCE, ICON, VERSION_FILE, __file__))


def build():
    csc = compiler()
    if csc is None:
        raise SystemExit("Hittar ingen C#-kompilator (.NET Framework 4, csc.exe) på den här datorn.")
    number = version() + ".0"
    folder = tempfile.mkdtemp(prefix="flsync-build-")
    info = os.path.join(folder, "Version.cs")
    with open(info, "w", encoding="utf-8") as f:
        f.write('[assembly: System.Reflection.AssemblyVersion("%s")]\n'
                '[assembly: System.Reflection.AssemblyFileVersion("%s")]\n'
                '[assembly: System.Reflection.AssemblyInformationalVersion("%s")]\n' % (number, number, version()))
    try:
        args = [csc, "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu", "/win32icon:" + ICON,
                "/reference:System.Windows.Forms.dll", "/out:" + EXE, SOURCE, info]
        done = subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        os.remove(info)
        os.rmdir(folder)
    output = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
    if done.returncode != 0:
        raise SystemExit("FLSync.exe gick inte att bygga:\n" + output)
    return EXE


def main():
    print("Byggde %s (version %s)" % (build(), version()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
