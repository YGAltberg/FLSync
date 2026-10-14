#!/usr/bin/env python3
"""FLSync setup: puts everything FLSync needs on this computer (docs/phase3.md, "Setup").

FLSync.exe runs it in FLSync's own window (app/installer.py) when started
from the download, and the Start menu's Reparera/Avinstallera FLSync too. For
developers, and as a fallback, it also runs in a console:

    Installera FLSync.cmd [--dry-run] [--uninstall] [--yes] [--skip-loopmidi] [--skip-fl-config]

With FL Studio closed (FL writes its settings back when it exits), it:
  1. finds every FL Studio and its Python;
  2. makes sure loopMIDI is installed (winget, else its download page), has
     the port FLSync and runs with it before FL starts (also after a restart
     of Windows: loopMIDI's autostart);
  3. copies the program to %LOCALAPPDATA%\\FLSync\\app, the FL script to FL's
     Settings\\Hardware\\FLSync (a junction there, a developer's checkout, is
     left alone) and the piano roll scripts to FL's "Piano roll scripts";
  4. sets FLSync up in the MIDI settings of every FL version (registry);
  5. makes the shortcuts "FLSync" on the desktop and in the Start menu. They
     run the installed FLSync.exe, which looks for a Python each time (the
     newest FL's own first), so updating or removing an FL version is fine.
Running it again repairs and updates. --uninstall undoes 3-5 and removes the
port; loopMIDI itself stays.

It runs on FL's own Python (3.12, embedded: it doesn't put the script's folder
on sys.path) and on any Python 3.10+, with the standard library only.
Everything it does to the computer goes through System, so the tests can use
a fake one (tests/test_setup.py).
"""

import argparse
import collections
import ctypes
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the download, or the installed copy
BRIDGE_DIR = os.path.join(ROOT, "bridge")
sys.dont_write_bytecode = True  # leave no __pycache__ in the downloaded folder
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)  # FL's Python doesn't put script folders on the path itself

import bridge  # noqa: E402  where FL and FLSync keep their files

PORT = "FLSync"                 # the loopMIDI port, FL's MIDI device and the script folder
FIRST_PORT = 10                 # FL port numbers below this are left to the user's controllers
APP_ITEMS = ["app", "bridge", "relay", "tools", "fl", "docs", "setup", "FLSync.exe",
             "FLSync.cmd", "Installera FLSync.cmd", "README.md", "LICENSE", "CHANGELOG.md"]  # setup too: repair, uninstall
NEEDED = [r"app\flsync.py", r"bridge\bridge.py", r"relay\relay.py", r"fl\FLSync", ("FLSync.exe", "FLSync.cmd")]
PROGRAM = "FLSync.exe"          # what the shortcuts start (FLSync.cmd in a checkout without it)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # console programs run without a window of their own
SKIP = ("__pycache__", "*.pyc", "*.pyo", ".*", "*.tmp", "*.part", "*.bak")  # never copied, never removed
PIANO_ROLL = "Piano roll scripts"
SHORTCUT = "FLSync.lnk"
REPAIR_LINK = "Reparera FLSync.lnk"  # in the Start menu: setup again, from the installed copy
UNINSTALL_LINK = "Avinstallera FLSync.lnk"  # ... with --uninstall
DESCRIPTION = "Jobba i samma FL Studio-projekt som andra, live"

LOOPMIDI_ID = "TobiasErichsen.loopMIDI"  # checked with `winget show --id TobiasErichsen.loopMIDI -e`
LOOPMIDI_PAGE = "https://www.tobias-erichsen.de/software/loopmidi.html"
LOOPMIDI_EXE = "loopMIDI.exe"
LOOPMIDI_PORTS = r"Software\Tobias Erichsen\loopMIDI\Ports"  # a DWORD 1 per port, read when loopMIDI starts

IMAGE_LINE = r"Software\Image-Line"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
FL_KEY = re.compile(r"FL Studio (\d+)\Z")
FL_PROCESS = re.compile(r"FL(64)?( \(scaled\))?\.exe\Z", re.IGNORECASE)  # FL64.exe, "FL64 (scaled).exe"

# FL's MIDI settings for FLSync, as FL itself writes them (all REG_SZ); "Port" is added.
MIDI_INPUT = {"Type": "-3", "ScriptFolder": PORT, "IDString": "-", "Enabled": "1"}
MIDI_OUTPUT = {"Sync": "0", "SendAllNotesOff": "1", "IDString": "-"}

REG_SZ, REG_DWORD = 1, 4        # winreg's numbers, also without winreg
DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"   # KNOWNFOLDERIDs
PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"
SW_SHOWMINNOACTIVE = 7

Install = collections.namedtuple("Install", "name folder version major python")
LoopMidi = collections.namedtuple("LoopMidi", "listed exe")


# --- small helpers ---------------------------------------------------------------------

def describe(error):
    if isinstance(error, OSError):
        text = error.strerror or str(error)
        return "%s: %s" % (text, error.filename) if error.filename else text
    return str(error) or error.__class__.__name__


def skipped(name):
    return any(fnmatch.fnmatch(name.lower(), pattern) for pattern in SKIP)


def lookup(values, name):
    """(value, type) of a registry value, by name in any case (as Windows does), or None."""
    for key, value in (values or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def inside(path, folder):
    path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # another drive
        return False


def command_path(text):
    """The program at the start of a command line or icon location:
    '"C:\\x y\\a.exe" /u' and 'C:\\x y\\a.exe,0' both give C:\\x y\\a.exe."""
    text = (text or "").strip()
    if text.startswith('"'):
        return text[1:].split('"', 1)[0]
    match = re.match(r"(.+?\.exe)\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return re.sub(r",\s*-?\d+\Z", "", text)


def major_version(version, folder_name=""):
    """FL's major version, which names its registry key: "25.2.4.5242" or a folder
    "FL Studio 2025" give 25, "FL Studio 21" gives 21."""
    match = re.match(r"\s*(\d+)", version or "") or re.search(r"FL Studio (\d+)", folder_name or "", re.IGNORECASE)
    if not match:
        return None
    number = int(match.group(1))
    return number - 2000 if number >= 2000 else number


def fl_devices(system, path):
    """{device: values} of FL's MIDI inputs or outputs under HKCU\\path."""
    return {name: system.reg_values("HKCU", path + "\\" + name) or {} for name in system.reg_keys("HKCU", path)}


def fl_settings_changes(system, key):
    """What FLSync's MIDI input and output in FL's settings (`key`, "FL Studio
    26") still need: (port, note about the port, [(path, name, value), ...]),
    no changes when that FL is set up already. Setup and the launcher both
    use it (the launcher for an FL installed after FLSync)."""
    devices = IMAGE_LINE + "\\" + key + r"\Devices"
    port, note = choose_port(fl_devices(system, devices + r"\MIDI input"),
                             fl_devices(system, devices + r"\MIDI output"))
    wanted = [(devices + r"\MIDI input" + "\\" + PORT, dict(MIDI_INPUT, Port=str(port))),
              (devices + r"\MIDI output" + "\\" + PORT, dict(MIDI_OUTPUT, Port=str(port)))]
    changes = []
    for path, values in wanted:
        have = system.reg_values("HKCU", path)
        for name, value in values.items():
            if lookup(have, name) != (value, REG_SZ):
                changes.append((path, name, value))
    return port, note, changes


def fl_label(major):
    """The name FL Studio goes by: 25 is "FL Studio 2025", 21 is "FL Studio 21"."""
    if major is None:
        return "FL Studio"
    return "FL Studio 20%02d" % major if major >= 24 else "FL Studio %d" % major


def version_key(install):
    parts = tuple(int(p) for p in re.findall(r"\d+", install.version or ""))
    return (install.major or 0,) + parts


def port_number(value):
    """An FL MIDI port number from the registry ("10"), or None (unset is "-1")."""
    if isinstance(value, tuple):
        value = value[0]
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if 0 <= number <= 255 else None


def choose_port(inputs, outputs):
    """The port number for FLSync: its own if it has one that no other device
    uses, else the lowest from FIRST_PORT on that no other device uses.
    `inputs`/`outputs`: {device: registry values}. Returns (port, note or None)."""
    used, own = set(), []
    for devices in (inputs, outputs):
        for name, values in devices.items():
            port = port_number(lookup(values, "Port"))
            if port is None:
                continue
            if name.lower() == PORT.lower():
                own.append(port)
            else:
                used.add(port)
    for port in own:
        if port not in used:
            return port, None
    port = FIRST_PORT
    while port in used:
        port += 1
    if own:
        return port, "Port %d används av en annan MIDI-enhet, så FLSync får port %d." % (own[0], port)
    return port, None


def is_link(path):
    """A symbolic link or a junction (what install.ps1 made for developers)."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def link_target(path):
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        return os.path.realpath(path)
    if target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + target[8:]
    return target[4:] if target.startswith("\\\\?\\") else target


def known_folder(guid):
    """A Windows known folder (desktop, Start menu), wherever it is (OneDrive too), or None."""
    try:
        from ctypes import wintypes
        folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
        found = ctypes.c_wchar_p()
        get = ctypes.WinDLL("shell32").SHGetKnownFolderPath
        get.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        get.restype = ctypes.c_long
        if get(ctypes.addressof(folder_id), 0, None, ctypes.byref(found)) != 0:
            return None
        try:
            return found.value or None
        finally:
            ctypes.WinDLL("ole32").CoTaskMemFree(ctypes.cast(found, ctypes.c_void_p))
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def allowed_key(path):
    """Setup never goes near license data: no key named Registrations."""
    return "registrations" not in path.lower().split("\\")


def _processes():
    """(pid, exe name) of every process, from a Toolhelp snapshot."""
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel32, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        getattr(kernel32, name).restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _retry_writable(func, path, _info):
    os.chmod(path, stat.S_IWRITE)  # a read-only file: make it writable and try once more
    func(path)


# One line without double quotes, so it passes through the command line as it is;
# the paths come in environment variables, so no quoting of them is needed.
SHORTCUT_SCRIPT = " ".join("""
try {
    $shell = New-Object -ComObject WScript.Shell;
    foreach ($path in $env:FLSYNC_LINKS.Split('|')) {
        $link = $shell.CreateShortcut($path);
        $link.TargetPath = $env:FLSYNC_TARGET;
        $link.WorkingDirectory = $env:FLSYNC_WORKDIR;
        $link.Description = $env:FLSYNC_DESCRIPTION;
        if ($env:FLSYNC_ARGS) { $link.Arguments = $env:FLSYNC_ARGS };
        if ($env:FLSYNC_ICON) { $link.IconLocation = $env:FLSYNC_ICON };
        $link.WindowStyle = 1;
        $link.Save()
    }
} catch {
    [Console]::Out.WriteLine($_.Exception.Message);
    exit 1
}
""".split())


# --- the computer ----------------------------------------------------------------------

class System:
    """What setup does to the computer: registry, files, processes and other
    programs. This one is the real Windows; the tests use a fake.

    Registry paths are relative to a root, "HKCU" or "HKLM"; `view` 32 or 64
    picks that half of HKLM (uninstall entries of 32-bit programs, like FL's,
    are in the 32-bit one)."""

    # --- folders -----------------------------------------------------------------------

    def local_data_dir(self):
        return bridge.local_data_dir()  # %LOCALAPPDATA%\FLSync

    def fl_user_dir(self):
        return bridge.fl_folders()["user_dir"]  # FL's user data folder: <Documents>\Image-Line unless moved

    def desktop_dir(self):
        return known_folder(DESKTOP) or os.path.join(os.path.expanduser("~"), "Desktop")

    def programs_dir(self):
        roaming = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return known_folder(PROGRAMS) or os.path.join(roaming, "Microsoft", "Windows", "Start Menu", "Programs")

    def program_files_dirs(self):
        found = []
        for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            path = os.environ.get(name)
            if path and os.path.normcase(path) not in [os.path.normcase(p) for p in found]:
                found.append(path)
        return found

    # --- registry ----------------------------------------------------------------------

    def _open(self, root, path, view, access):
        import winreg
        if not allowed_key(path):
            raise ValueError("FLSync rör inte %s" % path)
        if view == 64:
            access |= winreg.KEY_WOW64_64KEY
        elif view == 32:
            access |= winreg.KEY_WOW64_32KEY
        top = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[root]
        return winreg.OpenKey(top, path, 0, access)

    def reg_keys(self, root, path, view=None):
        """Names of the subkeys; [] if the key isn't there."""
        import winreg
        names = []
        try:
            with self._open(root, path, view, winreg.KEY_READ) as key:
                while True:
                    try:
                        names.append(winreg.EnumKey(key, len(names)))
                    except OSError:
                        return names
        except OSError:
            return []

    def reg_values(self, root, path, view=None):
        """{name: (value, type)} of a key, or None if the key isn't there."""
        import winreg
        try:
            with self._open(root, path, view, winreg.KEY_READ) as key:
                values = {}
                while True:
                    try:
                        name, value, kind = winreg.EnumValue(key, len(values))
                    except OSError:
                        return values
                    values[name] = (value, kind)
        except OSError:
            return None

    def reg_set(self, root, path, name, value, kind):
        import winreg
        if root != "HKCU" or not allowed_key(path):
            raise ValueError("FLSync ändrar inte %s\\%s" % (root, path))
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)

    def reg_delete_value(self, root, path, name):
        import winreg
        with self._open(root, path, None, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)

    def reg_delete_key(self, root, path):
        """Remove a key with everything in it."""
        import winreg
        for name in self.reg_keys(root, path):
            self.reg_delete_key(root, path + "\\" + name)
        parent, _, name = path.rpartition("\\")
        with self._open(root, parent, None, winreg.KEY_WRITE) as key:
            winreg.DeleteKey(key, name)

    # --- processes ---------------------------------------------------------------------

    def processes(self):
        """(pid, exe name) of every running process."""
        try:
            return _processes()
        except OSError:
            pass
        code, text = self.run(["tasklist", "/FO", "CSV", "/NH"], timeout=30)
        found = []
        for line in text.splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) > 1 and parts[1].isdigit():
                found.append((int(parts[1]), parts[0]))
        return found

    def process_path(self, pid):
        """The program file of a running process, or None."""
        try:
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                            ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return buffer.value
                return None
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return None

    def start(self, exe, minimized=False):
        """Start a program on its own (it outlives setup and its window)."""
        info = subprocess.STARTUPINFO()
        if minimized:
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = SW_SHOWMINNOACTIVE
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe), startupinfo=info, close_fds=True,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            os.startfile(exe)  # e.g. one that asks for administrator rights

    def kill(self, name):
        """End every process with this exe name at once, without asking it (loopMIDI
        could save its ports while closing). True when they are all gone."""
        windows = os.environ.get("SystemRoot") or r"C:\Windows"
        self.run([os.path.join(windows, "System32", "taskkill.exe"), "/F", "/IM", name], timeout=30)
        for _ in range(50):
            if not any(n.lower() == name.lower() for _, n in self.processes()):
                return True
            time.sleep(0.1)
        return False

    def sleep(self, seconds):
        time.sleep(seconds)

    # --- other programs ----------------------------------------------------------------

    def which(self, name):
        return shutil.which(name)

    def run(self, args, live=False, timeout=None, env=None):
        """Run a program and wait: (exit code, its output). The code is None if it
        couldn't run. `live`: its output goes straight to the console; else it
        is kept, and a console program gets no window of its own (FLSync's
        window has no console for it)."""
        try:
            if live:
                return subprocess.call(args, timeout=timeout, env=env), ""
            done = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=env,
                                  creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return None, "det tog för lång tid"
        except OSError as e:
            return None, describe(e)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")

    def open_url(self, url):
        os.startfile(url)  # the default browser

    def make_shortcuts(self, links, target, workdir, description, icon=None, arguments=""):
        """Windows shortcuts (.lnk) at `links`, all to `target` (with `arguments`)."""
        for folder in {os.path.dirname(link) for link in links}:
            os.makedirs(folder, exist_ok=True)
        env = dict(os.environ, FLSYNC_LINKS="|".join(links), FLSYNC_TARGET=target, FLSYNC_WORKDIR=workdir,
                   FLSYNC_DESCRIPTION=description, FLSYNC_ICON=icon or "", FLSYNC_ARGS=arguments)
        windows = os.environ.get("SystemRoot") or r"C:\Windows"
        powershell = os.path.join(windows, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(powershell):
            powershell = "powershell"
        code, output = self.run([powershell, "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
                                timeout=120, env=env)
        if code != 0:
            raise RuntimeError(output.strip() or "PowerShell kunde inte skapa genvägarna")

    def midi_ports(self):
        """Names of the MIDI ports Windows has right now, or None if it can't tell."""
        try:
            import winmidi
        except ImportError:
            return None
        try:
            return winmidi.port_names(True) + winmidi.port_names(False)
        except (winmidi.MidiError, OSError, AttributeError, ValueError):
            return None

    # --- files ---------------------------------------------------------------------------

    def check(self, path):
        """Every file path setup uses passes here (the fake keeps them in its temp folder)."""
        return path

    def exists(self, path):
        return os.path.lexists(self.check(path))

    def isdir(self, path):
        return os.path.isdir(self.check(path))

    def isfile(self, path):
        return os.path.isfile(self.check(path))

    def listdir(self, path):
        try:
            return sorted(os.listdir(self.check(path)))
        except OSError:
            return []

    def read(self, path):
        with open(self.check(path), "rb") as f:
            return f.read()

    def is_link(self, path):
        return is_link(self.check(path))

    def link_target(self, path):
        return link_target(self.check(path))

    def makedirs(self, path):
        os.makedirs(self.check(path), exist_ok=True)

    def copy_file(self, source, target):
        self.check(source)
        if os.path.isfile(self.check(target)) and not os.access(target, os.W_OK):
            os.chmod(target, stat.S_IWRITE)
        shutil.copy2(source, target)
        try:
            # Python 3.12+ copies the "downloaded from the internet" mark too; without
            # it Windows doesn't warn each time the installed FLSync.cmd starts.
            os.remove(target + ":Zone.Identifier")
        except OSError:
            pass

    def remove_file(self, path):
        try:
            os.remove(self.check(path))
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)

    def remove_link(self, path):
        """Remove a link (junction) itself; what it points to stays."""
        try:
            os.rmdir(self.check(path))
        except OSError:
            os.remove(path)

    def remove_tree(self, path):
        if is_link(self.check(path)):
            self.remove_link(path)  # never through a link
        elif sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry_writable)
        else:
            shutil.rmtree(path, onerror=_retry_writable)

    def leave(self, folder):
        """Step out of `folder` if this process is in it, so that it can be removed."""
        if inside(os.getcwd(), self.check(folder)):
            os.chdir(os.path.dirname(os.path.normpath(folder)))


# --- setup -----------------------------------------------------------------------------

class Setup:
    """Installs or uninstalls FLSync on `system`, telling the user what it finds
    and does through `out(text, end)`; asks through `ask(prompt)` when
    `interactive`. run() returns the exit code. `window`: it runs in FLSync's
    window (app/installer.py), not a console: no console for other programs,
    and `cancelled()` (the window's Avbryt) ends the wait for FL to close."""

    def __init__(self, system, args, root=ROOT, out=None, ask=None, interactive=False, window=False,
                 cancelled=None):
        self.system = system
        self.args = args
        self.root = root
        self.out = out or (lambda text="", end="\n": print(text, end=end, flush=True))
        self.ask = ask or input
        self.interactive = interactive
        self.window = window
        self.cancelled = cancelled or (lambda: False)
        self.dry = args.dry_run
        self.problems = []           # what went wrong
        self.todo = []               # what the user has to do
        self.steps = self.step_no = 0
        self.assume_fl_closed = False  # a dry run shows what happens once FL is closed
        self.removing_self = False   # uninstalling the copy setup runs from
        self.updated = False         # a new version over an older one
        self.entries = None          # uninstall entries, read once

    # --- talking -----------------------------------------------------------------------

    def write(self, text="", end="\n"):
        self.out(text, end)

    def say(self, text):
        self.write("  " + text)

    def step(self, title):
        self.step_no += 1
        self.write()
        self.write("Steg %d av %d: %s" % (self.step_no, self.steps, title))

    def act(self, text, action, live=False):
        """Make one change and say what it is; in a dry run only say it. True if done."""
        if self.dry:
            self.write("  -> " + text)
            return True
        self.write("  -> %s%s" % (text, "" if live else " ..."), end="\n" if live else "")
        try:
            action()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
            if not live:
                self.write(" gick inte")
            self.write("     " + describe(e))
            self.problems.append("%s: %s" % (text, describe(e)))
            return False
        if not live:
            self.write(" klart")
        return True

    def confirm(self, question, default=True):
        if self.args.yes:
            return True
        if not self.interactive:
            return default
        try:
            answer = self.ask("  %s [%s] " % (question, "J/n" if default else "j/N"))
        except EOFError:
            return default
        answer = answer.strip().lower()
        return default if not answer else answer[0] in "jy"

    def again(self):
        if self.window:
            return "Start-menyn > %s FLSync" % ("Avinstallera" if self.args.uninstall else "Reparera")
        return "Installera FLSync.cmd" + (" --uninstall" if self.args.uninstall else "")

    # --- finding things ----------------------------------------------------------------

    def app_dir(self):
        return os.path.join(self.system.local_data_dir(), "app")

    def fl_settings_dir(self):
        return os.path.join(self.system.fl_user_dir(), "FL Studio", "Settings")

    def uninstall_entries(self):
        """What Windows lists under installed programs: [{value: text}]."""
        if self.entries is None:
            self.entries = []
            for root, view in (("HKLM", 64), ("HKLM", 32), ("HKCU", None)):
                for name in self.system.reg_keys(root, UNINSTALL, view):
                    values = self.system.reg_values(root, UNINSTALL + "\\" + name, view) or {}
                    self.entries.append({k: v for k, (v, _) in values.items() if isinstance(v, str)})
        return self.entries

    def find_fl(self):
        """Every installed FL Studio (a folder with FL64.exe), newest first."""
        found = collections.OrderedDict()

        def add(folder, name=None, version=None, up=0):
            """`up`: how many folders FL's own folder may be above `folder`."""
            if not folder or not os.path.isabs(folder):
                return  # an entry without a usable path
            folder = os.path.normpath(folder)
            for _ in range(up + 1):
                if self.system.isfile(os.path.join(folder, "FL64.exe")):
                    break
                folder = os.path.dirname(folder)
            else:
                return
            key = os.path.normcase(folder)
            if key in found and (found[key].version or not version):
                return
            base = os.path.basename(folder)
            major = major_version(version, base)
            if not name:
                name = base if re.match(r"FL Studio \d", base, re.IGNORECASE) else fl_label(major)
            python = os.path.join(folder, "Shared", "Python", "python.exe")
            found[key] = Install(name, folder, version, major, python if self.system.isfile(python) else None)

        for entry in self.uninstall_entries():
            name = (entry.get("DisplayName") or "").strip()
            if not re.match(r"FL Studio \d", name, re.IGNORECASE):
                continue  # also "FL Studio ASIO"
            version = entry.get("DisplayVersion")
            if entry.get("InstallLocation"):
                add(entry["InstallLocation"].strip().strip('"'), name, version)
            if entry.get("UninstallString"):  # <FL>\uninstall.exe
                add(os.path.dirname(command_path(entry["UninstallString"])), name, version, up=1)
            if entry.get("DisplayIcon"):  # <FL>\Data\System\icon.ico,0
                add(os.path.dirname(command_path(entry["DisplayIcon"])), name, version, up=2)
        for base in self.system.program_files_dirs():
            image_line = os.path.join(base, "Image-Line")
            for name in self.system.listdir(image_line):
                if re.match(r"FL Studio \d", name, re.IGNORECASE):
                    add(os.path.join(image_line, name))
        paths = self.system.reg_values("HKCU", IMAGE_LINE + r"\Shared\Paths") or {}
        installed = lookup(paths, "Install path")  # the FL installed last
        if installed and isinstance(installed[0], str) and installed[0].strip():
            add(installed[0].strip())
        return sorted(found.values(), key=version_key, reverse=True)

    def fl_keys(self):
        """FL's settings keys under HKCU\\Software\\Image-Line ("FL Studio 25"), oldest first.
        Only their names are listed: nothing else under Image-Line is opened."""
        keys = [name for name in self.system.reg_keys("HKCU", IMAGE_LINE) if FL_KEY.match(name)]
        return sorted(keys, key=lambda name: int(FL_KEY.match(name).group(1)))

    def fl_running(self):
        if self.assume_fl_closed:
            return False
        return any(FL_PROCESS.match(name) for _, name in self.system.processes())

    def loopmidi_running(self):
        return any(name.lower() == LOOPMIDI_EXE.lower() for _, name in self.system.processes())

    def find_loopmidi(self):
        """Is loopMIDI installed (Windows lists it), and where is loopMIDI.exe."""
        listed, places = False, []
        for entry in self.uninstall_entries():
            if (entry.get("DisplayName") or "").strip().lower() != "loopmidi":
                continue  # also "loopMIDIBlockLegacy"
            listed = True
            if entry.get("InstallLocation"):
                places.append(os.path.join(entry["InstallLocation"].strip().strip('"'), LOOPMIDI_EXE))
            if entry.get("DisplayIcon"):
                places.append(command_path(entry["DisplayIcon"]))
        for value in self.autostart_values():
            places.append(command_path(value))
        for base in self.system.program_files_dirs():
            places.append(os.path.join(base, "Tobias Erichsen", "loopMIDI", LOOPMIDI_EXE))
        for pid, name in self.system.processes():
            if name.lower() == LOOPMIDI_EXE.lower():
                places.append(self.system.process_path(pid) or "")
        for place in places:
            if (place and os.path.isabs(place) and os.path.basename(place).lower() == LOOPMIDI_EXE.lower()
                    and self.system.isfile(place)):
                return LoopMidi(True, os.path.normpath(place))
        return LoopMidi(listed, None)

    def autostart_values(self):
        """The commands Windows runs at login that start loopMIDI."""
        found = []
        for root, view in (("HKCU", None), ("HKLM", 64), ("HKLM", 32)):
            for value, _ in (self.system.reg_values(root, RUN, view) or {}).values():
                if isinstance(value, str) and LOOPMIDI_EXE.lower() in value.lower():
                    found.append(value)
        return found

    def port_saved(self):
        return lookup(self.system.reg_values("HKCU", LOOPMIDI_PORTS), PORT) == (1, REG_DWORD)

    def port_live(self):
        """Does the port exist right now (loopMIDI made it)? None: can't tell."""
        names = self.system.midi_ports()
        if names is None:
            return None
        return any(name.lower() == PORT.lower() for name in names)

    def pianoroll_names(self, folder):
        return [n for n in self.system.listdir(folder) if n.lower().endswith(".pyscript")]

    def same_file(self, a, b):
        s = self.system
        return s.isfile(a) and s.isfile(b) and not s.is_link(b) and s.read(a) == s.read(b)

    def same_tree(self, source, target):
        """Is folder `target` already a copy of `source` (caches aside)?"""
        s = self.system
        if s.is_link(target) or not s.isdir(target):
            return False
        names = [n for n in s.listdir(source) if not skipped(n)]
        if sorted(n.lower() for n in names) != sorted(n.lower() for n in s.listdir(target) if not skipped(n)):
            return False
        for name in names:
            a, b = os.path.join(source, name), os.path.join(target, name)
            if not (self.same_tree(a, b) if s.isdir(a) else self.same_file(a, b)):
                return False
        return True

    # --- changing things ----------------------------------------------------------------

    def remove_any(self, path):
        s = self.system
        if s.is_link(path):
            s.remove_link(path)
        elif s.isdir(path):
            s.remove_tree(path)
        elif s.exists(path):
            s.remove_file(path)

    def copy_over(self, source, target):
        if self.system.is_link(target) or self.system.isdir(target):
            self.remove_any(target)
        self.system.copy_file(source, target)

    def mirror(self, source, target):
        """Make folder `target` a copy of `source`, leaving caches alone. Links on
        the way are replaced, never followed."""
        s = self.system
        if s.is_link(target) or (s.exists(target) and not s.isdir(target)):
            self.remove_any(target)
        s.makedirs(target)
        names = [n for n in s.listdir(source) if not skipped(n)]
        for name in names:
            a, b = os.path.join(source, name), os.path.join(target, name)
            if s.isdir(a):
                self.mirror(a, b)
            else:
                self.copy_over(a, b)
        keep = {n.lower() for n in names}
        for name in s.listdir(target):
            if name.lower() not in keep and not skipped(name):
                self.remove_any(os.path.join(target, name))

    def copy_program(self, app):
        s = self.system
        if s.is_link(app):
            s.remove_link(app)
        s.makedirs(app)
        wanted = set()
        for item in APP_ITEMS:
            source = os.path.join(self.root, item)
            if not s.exists(source):
                continue
            wanted.add(item.lower())
            if s.isdir(source):
                self.mirror(source, os.path.join(app, item))
            else:
                self.copy_over(source, os.path.join(app, item))
        for name in s.listdir(app):  # left from an older version
            if name.lower() not in wanted and not skipped(name):
                self.remove_any(os.path.join(app, name))

    def program_same(self, app):
        s = self.system
        if not s.isdir(app) or s.is_link(app):
            return False
        items = [item for item in APP_ITEMS if s.exists(os.path.join(self.root, item))]
        if sorted(i.lower() for i in items) != sorted(n.lower() for n in s.listdir(app) if not skipped(n)):
            return False
        for item in items:
            a, b = os.path.join(self.root, item), os.path.join(app, item)
            if not (self.same_tree(a, b) if s.isdir(a) else self.same_file(a, b)):
                return False
        return True

    # --- FL must be closed ---------------------------------------------------------------

    def fl_gate(self):
        """FL must be closed while its settings change: it writes them back when it
        exits. Waits for that if it can; True to go on."""
        if self.args.skip_fl_config or not self.fl_running():
            return True
        what = "avinstallationen" if self.args.uninstall else "installationen"
        if self.dry:
            self.say("FL Studio är igång. På riktigt ber %s dig först stänga FL Studio och fortsätter" % what)
            self.say("sedan (med --yes avslutas den i stället). Så här skulle det gå när FL är stängt:")
            self.assume_fl_closed = True
            return True
        if not self.interactive:
            self.say("FL Studio är igång. FL sparar sina inställningar när det stängs och skulle skriva")
            self.say("över FLSyncs. Stäng FL Studio och kör %s igen (eller kör med --skip-fl-config,"
                     % self.again())
            self.say("så rörs inte FL:s MIDI-inställningar).")
            self.write()
            self.write("Ingenting har ändrats.")
            return False
        self.say("FL Studio är igång. Spara ditt projekt och stäng FL Studio, så fortsätter %s." % what)
        self.say("(FL sparar sina inställningar när det stängs och skulle annars skriva över FLSyncs.)")
        self.say("Väntar på att FL Studio stängs ..." + ("" if self.window else " (Ctrl+C avbryter)"))
        while self.fl_running():
            if self.cancelled():
                self.write()
                self.write("Avbrutet. Ingenting har ändrats.")
                return False
            self.system.sleep(1)
        self.say("FL Studio är stängt.")
        return True

    # --- install ---------------------------------------------------------------------------

    def run(self):
        code = self.uninstall() if self.args.uninstall else self.install()
        if self.removing_self and not self.dry and self.interactive and not self.window:
            try:
                self.ask("Tryck Enter för att stänga fönstret.")  # the .cmd that would pause is gone
            except EOFError:
                pass
        return code

    def install(self):
        self.steps = 5
        self.write("FLSync - installation")
        self.write("Installationen ställer in allt som FLSync behöver på den här datorn. Kör den igen när")
        self.write("du vill, så repareras och uppdateras det som behövs.")
        if self.dry:
            self.write("PROVKÖRNING: ingenting ändras. Rader med -> visar vad som skulle göras.")
        installs = self.step_fl()
        if installs is None:
            return 1
        if not self.fl_gate():
            return 2
        self.step_loopmidi()
        self.step_program()
        self.step_fl_settings()
        self.step_shortcuts()
        return self.finish_install()

    def step_fl(self):
        self.step("FL Studio")
        installs = self.find_fl()
        keys = self.fl_keys()
        for fl in installs:
            self.say("Hittade %s%s i %s" % (fl.name, " (version %s)" % fl.version if fl.version else "", fl.folder))
        if not installs and not keys:
            self.say("Hittade inget FL Studio på den här datorn.")
            self.write()
            self.write("Installera FL Studio, starta det en gång och kör sedan %s igen." % self.again())
            return None
        with_python = [fl for fl in installs if fl.python]
        if with_python:
            self.say("FLSync kommer att köras med Python från %s:" % with_python[0].name)
            self.say("  " + with_python[0].python)
        else:
            self.say("Inget FL Studio här har en egen Python, så FLSync använder den Python som finns på datorn.")
        started = {int(FL_KEY.match(key).group(1)) for key in keys}
        for fl in installs:
            if fl.major is not None and fl.major not in started:
                self.say("%s har inte startats än, så det har inga MIDI-inställningar att ställa in." % fl.name)
                self.todo.append("Ska du använda %s med FLSync: starta det en gång, stäng det och kör %s igen."
                                 % (fl.name, self.again()))
        return installs

    def step_loopmidi(self):
        self.step("loopMIDI (den virtuella MIDI-kabeln mellan FL Studio och FLSync)")
        if self.args.skip_loopmidi:
            self.say("Hoppar över loopMIDI (--skip-loopmidi).")
            return
        found = self.find_loopmidi()
        if found.exe:
            self.say("loopMIDI är installerat: %s" % found.exe)
        elif found.listed:
            self.say("loopMIDI är installerat, men programfilen %s hittades inte." % LOOPMIDI_EXE)
        else:
            self.say("loopMIDI saknas. Det är ett gratis program som FLSync behöver för att prata med FL Studio.")
            found = self.install_loopmidi()
        if not found.exe:
            # loopMIDI reads its ports when it starts: then FLSync is there from the start.
            if not self.port_saved():
                self.act("Lägger till porten FLSync i loopMIDI:s inställningar", self.save_port)
            if found.listed:
                self.todo.append("Starta loopMIDI och kontrollera att porten FLSync finns, innan du startar FL Studio.")
            return
        self.ensure_port(found.exe)
        if self.autostart_values():
            self.say("loopMIDI startar när du loggar in i Windows.")
        else:
            self.act("Ställer in att loopMIDI startar när du loggar in i Windows (porten måste finnas innan FL"
                     " Studio startar)", lambda: self.system.reg_set("HKCU", RUN, "loopMIDI", '"%s"' % found.exe,
                                                                     REG_SZ))

    def save_port(self):
        self.system.reg_set("HKCU", LOOPMIDI_PORTS, PORT, 1, REG_DWORD)

    def install_loopmidi(self):
        """Install loopMIDI with winget, or send the user to its download page."""
        winget = self.system.which("winget")
        if winget and self.system.run([winget, "--version"], timeout=60)[0] != 0:
            winget = None
        command = [winget or "winget", "install", "--id", LOOPMIDI_ID, "-e", "--source", "winget",
                   "--accept-source-agreements", "--accept-package-agreements"]
        if self.dry:
            if winget:
                self.act("Installerar loopMIDI med winget (efter en fråga; Windows ber om lov): %s"
                         % " ".join(command[1:]), None)
            else:
                self.say("winget (Windows pakethanterare) finns inte, så loopMIDI:s hemsida skulle öppnas.")
            return LoopMidi(False, None)
        if winget and self.confirm("Installera loopMIDI nu? Windows frågar om lov att ändra datorn.", True):
            if self.act("Installerar loopMIDI med winget, det kan ta en minut", lambda: self.winget(command),
                        live=not self.window):
                self.entries = None  # Windows lists it now
                found = self.find_loopmidi()
                if found.exe:
                    self.say("loopMIDI är installerat: %s" % found.exe)
                    return found
                self.say("loopMIDI verkar inte ha installerats.")
        elif not winget:
            self.say("winget (Windows pakethanterare) finns inte på den här datorn.")
        self.say("Öppnar loopMIDI:s hemsida i webbläsaren: %s" % LOOPMIDI_PAGE)
        self.say("Ladda ner loopMIDI där, packa upp zip-filen och kör loopMIDISetup.exe.")
        try:
            self.system.open_url(LOOPMIDI_PAGE)
        except OSError as e:
            self.say("Det gick inte att öppna sidan (%s). Öppna den själv." % describe(e))
        self.todo.append("Installera loopMIDI från %s och kör sedan %s igen." % (LOOPMIDI_PAGE, self.again()))
        return LoopMidi(False, None)

    def winget(self, command):
        code, output = self.system.run(command, live=not self.window, timeout=900 if self.window else None)
        if code != 0:
            last = [line.strip() for line in output.splitlines() if line.strip()][-1:]
            raise RuntimeError("winget slutade med felkod %s%s" % (code, ": " + last[0] if last else ""))

    def ensure_port(self, exe):
        """loopMIDI running with the port FLSync. Its ports are made when it starts,
        so a new one means a restart; but not while FL runs: a port made then
        carries nothing to that FL."""
        saved, running, live = self.port_saved(), self.loopmidi_running(), self.port_live()
        if running and live is not False:
            self.say("loopMIDI är igång och porten FLSync finns." if live else
                     "loopMIDI är igång (om porten FLSync finns går inte att se härifrån).")
            if not saved:
                self.act("Sparar porten FLSync i loopMIDI:s inställningar, så att den finns nästa gång",
                         self.save_port)
            return
        if running and self.fl_running():
            if not saved:
                self.act("Lägger till porten FLSync i loopMIDI:s inställningar", self.save_port)
            self.say("loopMIDI är igång utan porten FLSync och kan inte startas om nu: en port som skapas")
            self.say("medan FL Studio är igång får ingen data.")
            self.todo.append("Stäng FL Studio och kör %s igen, så startas loopMIDI om med porten FLSync."
                             % self.again())
            return
        if running:
            self.say("loopMIDI är igång, men utan porten FLSync.")

            def restart():
                if not self.system.kill(LOOPMIDI_EXE):
                    raise RuntimeError("loopMIDI gick inte att stänga. Avsluta det själv (högerklicka på "
                                       "loopMIDI-ikonen vid klockan > Exit) och kör Installera FLSync igen")
                self.save_port()  # after it closed: it can't write its old list over this
                self.system.start(exe, minimized=True)
            if not self.act("Startar om loopMIDI med porten FLSync", restart):
                return
        else:
            self.say("loopMIDI är inte igång.")
            if not saved:
                self.act("Lägger till porten FLSync i loopMIDI:s inställningar", self.save_port)
            if not self.act("Startar loopMIDI (minimerat)", lambda: self.system.start(exe, minimized=True)):
                return
            if self.fl_running():
                self.todo.append("Starta om FL Studio, så att det ser porten FLSync.")
        if not self.dry:
            self.wait_for_port()

    def wait_for_port(self):
        for _ in range(20):
            live = self.port_live()
            if live is None:
                return
            if live:
                self.say("Porten FLSync finns nu.")
                return
            self.system.sleep(0.5)
        self.say("Porten FLSync syns inte än.")
        self.todo.append("Öppna loopMIDI och kontrollera att porten FLSync finns, innan du startar FL Studio.")

    def step_program(self):
        self.step("Programmet")
        s = self.system
        app = self.app_dir()
        old_scripts = self.pianoroll_names(os.path.join(app, "fl", PIANO_ROLL))
        if same_path(self.root, app):
            self.say("Programmet körs redan från %s, så det kopieras inte." % app)
        else:
            # (A tuple: any one of them will do.)
            missing = [" eller ".join(item) if isinstance(item, tuple) else item for item in NEEDED
                       if not any(s.exists(os.path.join(self.root, one))
                                  for one in (item if isinstance(item, tuple) else (item,)))]
            if missing:
                self.say("Det här saknas i %s: %s" % (self.root, ", ".join(missing)))
                self.problems.append("Delar av FLSync saknas (%s). Ladda ner FLSync igen." % ", ".join(missing))
            if self.program_same(app):
                self.say("Programmet i %s är redan den här versionen." % app)
            elif s.exists(app):
                if self.act("Uppdaterar programmet i %s" % app, lambda: self.copy_program(app)):
                    self.updated = True
            else:
                self.act("Kopierar programmet till %s" % app, lambda: self.copy_program(app))
        self.install_fl_script()
        self.install_pianoroll_scripts(old_scripts)

    def install_fl_script(self):
        s = self.system
        source = os.path.join(self.root, "fl", PORT)
        target = os.path.join(self.fl_settings_dir(), "Hardware", PORT)
        if s.is_link(target):
            self.say("FLSync-scriptet: %s är en länk till %s" % (target, s.link_target(target)))
            self.say("(en utvecklarinstallation), så den lämnas som den är.")
        elif not s.isdir(source):
            self.say("FLSync-scriptet saknas i %s." % source)
            self.problems.append("FLSync-scriptet saknas (%s). Ladda ner FLSync igen." % source)
        elif self.same_tree(source, target):
            self.say("FLSync-scriptet i %s är redan den här versionen." % target)
        else:
            verb = "Uppdaterar FLSync-scriptet i" if s.exists(target) else "Kopierar FLSync-scriptet till"
            self.act("%s %s" % (verb, target), lambda: self.mirror(source, target))

    def install_pianoroll_scripts(self, old):
        s = self.system
        source = os.path.join(self.root, "fl", PIANO_ROLL)
        target = os.path.join(self.fl_settings_dir(), PIANO_ROLL)
        names = self.pianoroll_names(source)
        changed = [n for n in names if not self.same_file(os.path.join(source, n), os.path.join(target, n))]

        def copy():
            s.makedirs(target)
            for name in changed:
                self.copy_over(os.path.join(source, name), os.path.join(target, name))
        if changed:
            self.act("Kopierar notscripten för pianorullen (%s) till %s"
                     % (", ".join(os.path.splitext(n)[0] for n in changed), target), copy)
        elif names:
            self.say("Notscripten för pianorullen i %s är redan den här versionen." % target)
        current = {n.lower() for n in names}
        for name in old:  # an older version's, no longer part of FLSync
            path = os.path.join(target, name)
            if name.lower() not in current and s.isfile(path):
                self.act("Tar bort det gamla notscriptet %s" % path, lambda p=path: s.remove_file(p))

    def step_fl_settings(self):
        self.step("FL Studios MIDI-inställningar")
        if self.args.skip_fl_config:
            self.say("Hoppar över FL Studios MIDI-inställningar (--skip-fl-config).")
            return
        if self.fl_running():
            self.say("FL Studio är igång, så dess MIDI-inställningar ändras inte nu.")
            self.todo.append("Stäng FL Studio och kör %s igen, så ställs FLSync in i FL:s MIDI-inställningar."
                             % self.again())
            return
        keys = self.fl_keys()
        if not keys:
            self.say("FL Studio har inga sparade inställningar än.")
            if not any("starta det en gång" in item for item in self.todo):  # said already for that FL
                self.todo.append("Starta FL Studio en gång, stäng det och kör %s igen." % self.again())
            return
        for key in keys:
            self.configure_fl(key)

    def devices(self, path):
        """{device: values} of FL's MIDI inputs or outputs."""
        return fl_devices(self.system, path)

    def configure_fl(self, key):
        label = fl_label(int(FL_KEY.match(key).group(1)))
        port, note, changes = fl_settings_changes(self.system, key)
        if not changes:
            self.say("%s: FLSync är redan inställt (port %d)." % (label, port))
            return
        if note:
            self.say(note)

        def apply():
            for path, name, value in changes:
                self.system.reg_set("HKCU", path, name, value, REG_SZ)
        self.act("%s: ställer in FLSync på port %d (ingången med scriptet FLSync, utgången på samma port)"
                 % (label, port), apply)

    def shortcut_paths(self):
        return [os.path.join(self.system.desktop_dir(), SHORTCUT), os.path.join(self.system.programs_dir(), SHORTCUT)]

    def tool_paths(self):
        """The Start menu's Reparera FLSync and Avinstallera FLSync."""
        programs = self.system.programs_dir()
        return [os.path.join(programs, REPAIR_LINK), os.path.join(programs, UNINSTALL_LINK)]

    def step_shortcuts(self):
        self.step("Genvägar")
        app = self.app_dir()
        # FLSync.exe opens FLSync's window (and the installer in it); a
        # developer's checkout without it has the console's .cmd files.
        exe = self.system.isfile(os.path.join(self.root, PROGRAM))
        target = os.path.join(app, PROGRAM if exe else "FLSync.cmd")
        links = self.shortcut_paths()
        icon = os.path.join(app, "app", "flsync.ico")
        icon = icon if self.system.isfile(os.path.join(self.root, "app", "flsync.ico")) else None
        verb = "Uppdaterar" if all(self.system.exists(link) for link in links) else "Skapar"
        if self.act("%s genvägen FLSync på skrivbordet och i Start-menyn" % verb,
                    lambda: self.system.make_shortcuts(links, target, app, DESCRIPTION, icon)):
            for link in links:
                self.say("   " + link)
            self.say("Den startar %s" % target)
        # Repairing and removing FLSync without the downloaded folder.
        setup = target if exe else os.path.join(app, "Installera FLSync.cmd")
        repair, uninstall = self.tool_paths()
        self.act("Lägger Reparera FLSync och Avinstallera FLSync i Start-menyn",
                 lambda: (self.system.make_shortcuts([repair], setup, app, "Installera om FLSync och rätta till "
                                                     "inställningarna", icon, arguments="--repair" if exe else ""),
                          self.system.make_shortcuts([uninstall], setup, app, "Ta bort FLSync", icon,
                                                     arguments="--uninstall")))

    def finish_install(self):
        self.write()
        if self.problems:
            self.write("Det här gick inte:")
            for problem in self.problems:
                self.write("  - " + problem)
            self.write("Rätta till det och kör %s igen." % self.again())
            self.write()
        if self.todo:
            self.write("Kvar att göra:" if not self.dry else "Kvar att göra efter installationen:")
            for item in self.todo:
                self.write("  - " + item)
            self.write()
        if self.dry:
            if self.window:
                self.write("Provkörningen är klar. Ingenting har ändrats.")
            else:
                self.write("Provkörningen är klar. Ingenting har ändrats. Kör Installera FLSync.cmd utan --dry-run")
                self.write("för att installera.")
            return 1 if self.problems else 0
        if self.problems:
            self.write("FLSync är installerat, men allt gick inte (se ovan).")
        elif self.todo:
            self.write("Nästan klart: gör först det som står under Kvar att göra.")
        else:
            self.write("Klart! FLSync är installerat.")
        self.write()
        self.write("Så här kommer du igång:")
        self.write("  1. Starta FL Studio.")
        self.write("  2. Dubbelklicka på FLSync på skrivbordet (eller välj FLSync i Start-menyn).")
        self.write("     FLSync öppnas i ett eget fönster.")
        self.write('  3. Välj "Starta session" och skicka inbjudan till de andra, eller "Gå med" och')
        self.write("     klistra in inbjudan du har fått.")
        if self.updated and not self.window:
            self.write("Är FLSync redan igång: stäng det och starta det igen, så används den nya versionen.")
        self.write()
        self.write("Reparera eller ta bort FLSync: Start-menyn > Reparera FLSync eller Avinstallera FLSync.")
        return 1 if self.problems else 0

    # --- uninstall --------------------------------------------------------------------------

    def uninstall(self):
        self.steps = 6
        self.write("FLSync - avinstallation")
        if self.dry:
            self.write("PROVKÖRNING: ingenting ändras. Rader med -> visar vad som skulle göras.")
        elif not self.confirm("Ta bort FLSync från den här datorn? (loopMIDI får vara kvar)", default=False):
            self.write("Avbrutet. Ingenting har ändrats.")
            return 1
        self.step("FL Studio")
        for fl in self.find_fl():
            self.say("Hittade %s i %s" % (fl.name, fl.folder))
        if not self.fl_gate():
            return 2
        self.remove_fl_settings()
        self.remove_scripts()
        self.step("Genvägar")
        links = [link for link in self.shortcut_paths() + self.tool_paths() if self.system.exists(link)]
        for link in links:
            self.act("Tar bort genvägen %s" % link, lambda p=link: self.system.remove_file(p))
        if not links:
            self.say("Det finns inga genvägar till FLSync.")
        self.remove_program()
        self.remove_port()
        return self.finish_uninstall()

    def remove_fl_settings(self):
        self.step("FL Studios MIDI-inställningar")
        if self.args.skip_fl_config:
            self.say("Hoppar över FL Studios MIDI-inställningar (--skip-fl-config).")
            return
        if self.fl_running():
            self.say("FL Studio är igång, så dess MIDI-inställningar ändras inte nu.")
            self.todo.append("Stäng FL Studio och kör %s igen, så tas FLSync bort ur FL:s MIDI-inställningar."
                             % self.again())
            return
        found = False
        for key in self.fl_keys():
            label = fl_label(int(FL_KEY.match(key).group(1)))
            for kind, what in (("MIDI input", "ingången"), ("MIDI output", "utgången")):
                path = "%s\\%s\\Devices\\%s\\%s" % (IMAGE_LINE, key, kind, PORT)
                if self.system.reg_values("HKCU", path) is not None:
                    found = True
                    self.act("%s: tar bort MIDI-%s FLSync" % (label, what),
                             lambda p=path: self.system.reg_delete_key("HKCU", p))
        if not found:
            self.say("FLSync finns inte i FL Studios MIDI-inställningar.")

    def remove_scripts(self):
        self.step("FLSync-scriptet och notscripten i FL Studio")
        s = self.system
        target = os.path.join(self.fl_settings_dir(), "Hardware", PORT)
        if s.is_link(target):
            self.act("Tar bort länken %s (filerna i %s är kvar)" % (target, s.link_target(target)),
                     lambda: s.remove_link(target))
        elif s.exists(target):
            self.act("Tar bort FLSync-scriptet %s" % target, lambda: s.remove_tree(target))
        else:
            self.say("FLSync-scriptet är inte installerat.")
        folder = os.path.join(self.fl_settings_dir(), PIANO_ROLL)
        names = {}
        for source in (os.path.join(self.root, "fl", PIANO_ROLL), os.path.join(self.app_dir(), "fl", PIANO_ROLL)):
            for name in self.pianoroll_names(source):
                names.setdefault(name.lower(), name)
        removed = False
        for name in sorted(names.values()):
            path = os.path.join(folder, name)
            if s.isfile(path):
                removed = True
                self.act("Tar bort notscriptet %s" % path, lambda p=path: s.remove_file(p))
        if not removed:
            self.say("Notscripten för pianorullen är inte installerade.")
        data = os.path.join(folder, "FLSync")  # the files the scripts and the bridge exchange notes through
        if s.isdir(data):
            self.act("Tar bort FLSyncs mapp för noterna %s" % data, lambda: s.remove_tree(data))

    def remove_program(self):
        self.step("Programmet")
        app = self.app_dir()
        if not self.system.exists(app):
            self.say("Programmet finns inte i %s." % app)
            return
        if inside(self.root, app):
            self.removing_self = True  # running from the installed copy: it goes too

        def remove():
            self.system.leave(app)
            self.system.remove_tree(app)
            data = self.system.local_data_dir()
            if self.system.isdir(data) and not self.system.listdir(data):
                self.system.remove_tree(data)  # nothing else was kept there
        if not self.act("Tar bort programmet %s" % app, remove):
            self.todo.append("Stäng FLSync om det är igång och kör %s igen." % self.again())

    def remove_port(self):
        self.step("loopMIDI-porten FLSync")
        if self.args.skip_loopmidi:
            self.say("Hoppar över loopMIDI (--skip-loopmidi).")
            return
        name = next((n for n in (self.system.reg_values("HKCU", LOOPMIDI_PORTS) or {})
                     if n.lower() == PORT.lower()), None)
        if name is None:
            self.say("loopMIDI har ingen port som heter FLSync.")
        else:
            running, exe = self.loopmidi_running(), self.find_loopmidi().exe
            if running and exe and not self.fl_running():
                def restart():
                    if not self.system.kill(LOOPMIDI_EXE):
                        raise RuntimeError("loopMIDI gick inte att stänga. Avsluta det själv (högerklicka på "
                                       "loopMIDI-ikonen vid klockan > Exit) och kör Installera FLSync igen")
                    self.system.reg_delete_value("HKCU", LOOPMIDI_PORTS, name)
                    self.system.start(exe, minimized=True)
                self.act("Tar bort porten FLSync ur loopMIDI (loopMIDI startas om, dina andra portar är kvar)",
                         restart)
            else:
                self.act("Tar bort porten FLSync ur loopMIDI:s inställningar",
                         lambda: self.system.reg_delete_value("HKCU", LOOPMIDI_PORTS, name))
                if running:
                    self.say("Porten finns kvar tills loopMIDI startas om.")
                    self.todo.append("Starta om loopMIDI%s, så försvinner porten FLSync."
                                     % (" när FL Studio är stängt" if self.fl_running() else ""))
        self.say("loopMIDI får vara kvar (andra program kan använda det). Vill du ta bort det: Inställningar >")
        self.say("Appar > Installerade appar > loopMIDI.")

    def finish_uninstall(self):
        self.write()
        if self.problems:
            self.write("Det här gick inte:")
            for problem in self.problems:
                self.write("  - " + problem)
            self.write()
        if self.todo:
            self.write("Kvar att göra:")
            for item in self.todo:
                self.write("  - " + item)
            self.write()
        if self.dry:
            self.write("Provkörningen är klar. Ingenting har ändrats.")
            return 1 if self.problems else 0
        self.write("FLSync är borttaget." if not self.problems else
                   "FLSync är borttaget, men allt gick inte (se ovan).")
        kept = [(path, what) for path, what in (
            (os.path.join(self.system.fl_user_dir(), "FL Studio", "Projects", "FLSync"), "projekt du har fått"),
            (self.system.local_data_dir(), "FLSyncs cache och inställningar")) if self.system.exists(path)]
        if kept:
            self.write("Det här finns kvar. Ta bort det själv om du inte vill ha det:")
            for path, what in kept:
                self.write("  %s (%s)" % (path, what))
        return 1 if self.problems else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="Installera FLSync.cmd",
        description="Installerar FLSync: loopMIDI och porten FLSync, programmet, scripten i FL Studio, "
                    "FL Studios MIDI-inställningar och genvägar. Kör den igen för att reparera eller uppdatera.")
    parser.add_argument("--dry-run", action="store_true", help="visa vad som skulle göras, men ändra ingenting")
    parser.add_argument("--uninstall", action="store_true", help="ta bort FLSync (loopMIDI får vara kvar)")
    parser.add_argument("--yes", "-y", action="store_true", help="fråga ingenting och vänta inte på FL Studio")
    parser.add_argument("--skip-loopmidi", action="store_true", help="rör inte loopMIDI")
    parser.add_argument("--skip-fl-config", action="store_true",
                        help="rör inte FL Studios MIDI-inställningar (då får FL vara igång)")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        # Paths can have any characters: print them (and --help) even into a pipe or a file.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = parse_args(argv)
    if sys.platform != "win32":
        print("FLSync fungerar bara på Windows.")
        return 1
    interactive = not args.yes and sys.stdin is not None and sys.stdin.isatty()
    try:
        return Setup(System(), args, interactive=interactive).run()
    except KeyboardInterrupt:
        print("\nAvbrutet.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
