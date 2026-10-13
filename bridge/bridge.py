#!/usr/bin/env python3
"""FLSync bridge: connects the FLSync script in FL Studio to the relay.

FL Studio doesn't let scripts open network connections, so the script sends
and receives MIDI over a loopMIDI port and this program carries the messages
to and from the relay:

    FL Studio <--MIDI "FLSync"--> bridge <--TCP--> relay

FL and the bridge share the one port and talk on different MIDI channels
(see wire.py). The bridge also sends FL a heartbeat many times per second,
which drives the script through OnMidiIn even when FL doesn't call OnIdle.

The MIDI link carries about 4 kB/s, so the bridge also does what is too big
for it (docs/protocol_v2.md) and tells FL only in short:
  * project snapshots: when FL saves the project, the bridge uploads the .flp
    and its samples over a second ("files") connection to the relay; when
    someone else shares a version, it downloads it into a project folder FL
    can open. A save made while someone else shared a newer version is merged
    with it first (flp_merge.py, docs/phase4.md), and FL opens the result;
  * piano-roll notes: FL's piano roll scripts can't reach the network, so they
    exchange notes with the bridge through files in %LOCALAPPDATA%\\FLSync\\notes.

    python bridge/bridge.py [--relay 127.0.0.1:31415] [--port FLSync] [--name Anna]
"""

import argparse
import asyncio
import base64
import binascii
import collections
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata

import flp
import flp_merge
import wire

PROTO = 2                   # of the files connection; FL's session says its own in its hello
PUMP_INTERVAL = 0.01        # seconds between rounds of MIDI in and out
EVENTS_PER_SECOND = 4000    # one event per byte: what FL takes in (about 4 kB/s, docs/protocol_v2.md)...
EVENT_BURST = 120           # ...with a short burst. Paced by the clock, not per round: Windows wakes the
                            # loop at 15.6 ms, or at once when anything else happens
HEARTBEAT_INTERVAL = 0.03   # also clocks the FL script
FL_TIMEOUT = 5.0            # FL silent this long is busy (loading, saving, a dialog) while its window is open...
FL_GONE = 120.0             # ...and counts as gone after this long, or as soon as its window is closed
FL_CHECK_INTERVAL = 1.0     # how often a silent FL's window is looked for
CONFIRM_TIME = 15.0         # saving a project that isn't the session's again within this makes it everyone's
AUTOSAVE_CHECK = 15.0       # FL asked to save by itself (`autosave`) and no save seen within this: it can't
AUTOSAVE_RETRY = 60.0       # ...now (a dialog was open): it is asked again after this,
AUTOSAVE_TRIES = 3          # ...until it didn't save this many times in a row
BACKUP_EVERY = 600.0        # of the copies FL's Backup folder gets for FL's saves by itself, one per project per this stays
BACKUP_SLACK = 2.0          # a copy this much older than the `autosave` that asked for the save is still that save's
BACKUP_COPY = re.compile(r" \(overwritten (?:on \d{4}-\d{2}-\d{2} )?at \d{1,2}h\d{2}\)(?:_\d+)?\.flp", re.I)
SEARCH_DEPTH = 3            # how deep FL's projects folder is searched for the file FL's title names...
SEARCH_KEPT = 30.0          # ...and how long what was found counts
LIVE_FOLDER = "Kanaler"      # in a version's folder: the channels brought into FL live, as plugin presets
LIVE_ADD_GAP = 0.5          # between asking FL to open two of them
LIVE_CONFIRM = 15.0         # ...and how long FL may take to have them all (a big VST loads for seconds)
CONNECT_TIMEOUT = 5.0
MIDI_RETRY = 2.0            # seconds between attempts to (re)open the MIDI ports
MAX_LINE = 64 << 20         # the welcome holds the session's whole state and its stored notes
WATCH_INTERVAL = 0.5        # how often the notes outbox is looked for
PROJECT_INTERVAL = 1.0      # how often the project file is looked at
STABLE_TIME = 1.0           # a saved project must stay unchanged this long before it is shared
SAVE_AS_WINDOW = 10.0       # a project file that FL starts using this soon after it was written was just saved
SAVED_WINDOW = 5.0          # a .flp written this long before FL said it saved (`st`) is FL's save...
SAVED_LATE = 1.5            # ... or this long after (FL writes first; `st` comes on its script's next tick)
WRITES_KEPT = 120.0         # .flp writes older than this don't tell where FL saves any more
SHARE_RETRY = 5.0           # a share that failed on the way is tried again after this
CONTEXT_INTERVAL = 4.0      # context.json is rewritten at least this often (the scripts check its age)
OUTBOX_GIVE_UP = 10.0       # an outbox.json that stays unreadable this long is removed
CHUNK = 512 << 10           # blob chunk; the protocol's largest
ANSWER_TIMEOUT = 60.0       # the relay's answer on the files connection must come within this
MAX_SNAP_PUT = 1 << 20      # a snap_put must stay well within the relay's longest line
MAX_LIST = 4096             # hashes per blob_has, samples and plugins per snapshot
MAX_TEXT = 4096
MAX_NAME = 256
MAX_NOTES = 20000           # notes in one notes_put
NOTE_FIELDS = 14
MAX_MISSING = 10            # missing plugins named in FL's notice
MAX_CHANNELS = 16           # channel numbers per missing plugin in FL's notice
LOADED_INTERVAL = 2.0       # how often FL's recent files and window title are looked at (see _check_loaded)
LOAD_LAG = 5.0              # FL's report of a loaded project may come this long before its recent files or title show it
LOAD_TIME = 60.0            # ... or this long after (a big project takes a while to load)
PING_INTERVAL = 5.0         # the bridge pings the relay this often ...
RELAY_TIMEOUT = 20.0        # ... and counts it as gone after this long without a byte from it
SNAP_TIMEOUT = 60.0         # the relay's answer to a snap_put must come within this
SHARE_TRIES = 5             # merges in a row, when others keep sharing newer versions meanwhile
IDENTIFY_TIME = 10.0        # after FL loaded a project, how long to look for which file it was
IDENTIFY_INTERVAL = 0.25
KNOWN_KEPT = 50             # versions remembered in bridge-state.json
VERSIONS_KEPT = 20          # older copies kept in a project folder's Versioner folder
STATE_FILE = "bridge-state.json"
FL_WINDOW = "TFruityLoopsMainForm"  # the class of FL's main window
VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){1,5}\Z")  # an FL version: "25.2.4.4960"
HASH = re.compile(r"[0-9a-f]{64}\Z")
RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            | {kind + n for kind in ("COM", "LPT") for n in "123456789¹²³"})
RUNNABLE = {".exe", ".com", ".bat", ".cmd", ".scr", ".pif", ".msi", ".msp", ".dll", ".cpl", ".sys", ".ps1",
            ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".url", ".reg", ".jar",
            ".inf", ".application", ".appref-ms", ".scf", ".msc"}


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def encode(msg):
    return json.dumps(msg, separators=(",", ":")).encode("ascii") + b"\n"


def is_int(value, low=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


def is_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def valid_hash(h):
    return isinstance(h, str) and HASH.match(h) is not None


def fingerprint(fp):
    return fp if isinstance(fp, str) and fp else None


def megabytes(size):
    return "%.1f MB" % (size / 1e6)


def capital(text):
    return text[:1].upper() + text[1:]


def sample_count(n):
    return "%d %s" % (n, "sample" if n == 1 else "samples")


def fl_version(text):
    """An FL version text ("25.2.4.4960") as it came, or None if it isn't one."""
    return text if isinstance(text, str) and len(text) <= 40 and VERSION.match(text) else None


def newer(theirs, ours):
    """Whether FL version `theirs` is a newer release than `ours` (major, minor,
    release; builds of one release open each other's projects). False if either
    can't be read."""
    if not fl_version(theirs) or not fl_version(ours):
        return False
    key = lambda text: tuple(int(part) for part in text.split(".")[:3])
    return key(theirs) > key(ours)


def missing_text(entry):
    """'Serum (kanal 3, 7)', 'Pro-Q 3 (mixern)' or just the name, as FL's script says it too."""
    if entry.get("channels"):
        return "%s (kanal %s)" % (entry["plugin"], ", ".join(str(c) for c in entry["channels"]))
    if entry.get("mixer"):
        return "%s (mixern)" % entry["plugin"]
    return entry["plugin"]


# --- where things are --------------------------------------------------------------

def local_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "FLSync")


def documents_dir():
    """The user's Documents folder, wherever Windows keeps it (it may be in OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            documents = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                             (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            found = ctypes.c_wchar_p()
            get = ctypes.windll.shell32.SHGetKnownFolderPath
            get.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                            ctypes.POINTER(ctypes.c_wchar_p)]
            get.restype = ctypes.c_long
            if get(ctypes.byref(documents), 0, None, ctypes.byref(found)) == 0:
                try:
                    if found.value:
                        return found.value
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(found, ctypes.c_void_p))
        except (OSError, AttributeError, ValueError, TypeError):
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def registry_text(path, name):
    """A text value under HKEY_CURRENT_USER, or None (not there, not Windows)."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def fl_folders():
    """What the placeholders in a project's sample paths stand for, as flp.resolve()
    takes them: FL's install folder (%FLStudioFactoryData%), its user data folder
    (%FLStudioUserData%, set in FL's File settings) and its downloads folder."""
    factory = registry_text(r"Software\Image-Line\Shared\Paths", "Install path")
    user = registry_text(r"Software\Image-Line\Shared\Paths", "Shared data")  # normally <Documents>\Image-Line
    if not user or not os.path.isdir(user):
        user = os.path.join(documents_dir(), "Image-Line")
    user = os.path.normpath(user)
    return {"factory_dir": factory, "user_dir": user, "downloads_dir": os.path.join(user, "Downloads")}


def read_mru_all():
    """Every file in FL's recent-files lists, the highest FL version's first,
    newest first. FL writes the list when it closes: it says what was open
    then, not what FL has now."""
    try:
        import winreg
    except ImportError:
        return []
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Image-Line") as root:
            for i in itertools.count():
                try:
                    names.append(winreg.EnumKey(root, i))
                except OSError:
                    break
    except OSError:
        return []
    versions = sorted(((int(m.group(1)), name) for name in names for m in [re.fullmatch(r"FL Studio (\d+)", name)]
                       if m), reverse=True)
    found = []
    for _, name in versions:
        for i in range(30):
            path = registry_text(r"Software\Image-Line\%s\MRU" % name, str(i))
            if not path:
                break
            found.append(path)
    return found


def title_file(title):
    """The project file's name, without .flp, that FL's window title shows
    ("Beat.flp - FL Studio 2025" -> "Beat"), or None: no title, or no file
    (a new, unsaved project, or a title that names no .flp)."""
    if not isinstance(title, str):
        return None
    head = re.split(r"\s+[-\u2013\u2014|]\s+FL Studio", title.strip(), maxsplit=1)[0].strip().strip("*").strip()
    if len(head) > 4 and head.lower().endswith(".flp"):
        return head[:-4].strip() or None
    return None


def read_mru():
    """The newest file in FL's recent-files list (the highest FL version's), or None."""
    try:
        import winreg
    except ImportError:
        return None
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Image-Line") as root:
            for i in itertools.count():
                try:
                    names.append(winreg.EnumKey(root, i))
                except OSError:
                    break
    except OSError:
        return None
    versions = []
    for name in names:
        match = re.fullmatch(r"FL Studio (\d+)", name)
        if match:
            versions.append((int(match.group(1)), name))
    for _, name in sorted(versions, reverse=True):
        newest = registry_text(r"Software\Image-Line\%s\MRU" % name, "0")
        if newest:
            return newest
    return None


def open_file(path):
    """Open a project in FL: with the FL Studio that runs, which hands it to
    its open window. Windows opens .flp files with the newest FL installed,
    which may be another version than the one in use: that would start a
    second FL. Without a running FL, the way Windows opens .flp files. A
    plugin preset (.fst) only ever goes to the FL that runs (it adds a channel)."""
    kind = os.path.splitext(path)[1].lower()
    exe = running_fl() if kind in (".flp", ".fst") else None
    if exe:
        subprocess.Popen([exe, path], close_fds=True)
    elif kind == ".fst":
        # A preset is for the FL in the session (it adds a channel there);
        # Windows may open .fst files with another FL version.
        raise OSError("FL Studio körs inte (eller flera FL-versioner gör det)")
    else:
        os.startfile(path)


_windows = None  # (user32, the type of EnumWindows' callback), once they are needed


def fl_window_title():
    """The title of FL Studio's main window (FL64.exe's top-level window of class
    TFruityLoopsMainForm), or None: not Windows, FL isn't running, or more than
    one FL is (then it can't be told which one is ours). Reading another
    program's window title never waits for that program."""
    titles = _fl_titles()
    return titles[0] if titles and len(titles) == 1 else None


def fl_window_open():
    """Whether an FL Studio main window is open (FL may be busy, but it runs),
    or None if that can't be told (not Windows)."""
    titles = _fl_titles()
    return None if titles is None else bool(titles)


def _fl_titles():
    """The titles of FL Studio's main windows, or None if they can't be read."""
    global _windows
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        if _windows is None:
            user32 = ctypes.WinDLL("user32")
            each_window = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [each_window, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            for function in (user32.GetClassNameW, user32.GetWindowTextW):
                function.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                function.restype = ctypes.c_int
            _windows = user32, each_window
        user32, each_window = _windows
        titles = []
        text = ctypes.create_unicode_buffer(512)

        def each(hwnd, _):
            if user32.GetClassNameW(hwnd, text, len(text)) and text.value == FL_WINDOW:
                user32.GetWindowTextW(hwnd, text, len(text))
                titles.append(text.value)
            return True

        user32.EnumWindows(each_window(each), 0)
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    return titles


def running_fl():
    """The program file of the FL Studio whose main window is open, or None:
    not Windows, no FL, or FLs of different versions (then it can't be told
    which one is in the session)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        each_window = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [each_window, wintypes.LPARAM]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        pids, text = set(), ctypes.create_unicode_buffer(256)

        def each(hwnd, _):
            if user32.GetClassNameW(hwnd, text, len(text)) and text.value == FL_WINDOW:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pids.add(pid.value)
            return True

        user32.EnumWindows(each_window(each), 0)
        exes = set()
        for pid in pids:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    exes.add(path.value)
            finally:
                kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    if len({os.path.normcase(e) for e in exes}) != 1:
        return None
    exe = exes.pop()
    return exe if os.path.isfile(exe) else None


def title_shows(title, name):
    """Whether a window title starts or ends with the project `name`, cut off by
    " - " (as in "Beat - FL Studio 2025"), maybe with ".flp" or a "*" for unsaved
    changes. A name inside a longer one ("Beat" in "Beat 2") doesn't count."""
    if not isinstance(title, str) or not name:
        return False
    shown = r"\*?\s*%s(?:\.flp)?\s*\*?" % re.escape(name.casefold())
    cut = r"\s+[-–—|]\s+"
    title = title.strip().casefold()
    return bool(re.match(r"%s(?:%s|\Z)" % (shown, cut), title) or re.search(r"(?:\A|%s)%s\Z" % (cut, shown), title))


# --- names from the network ----------------------------------------------------------

def safe_name(text, fallback, limit=64):
    """A harmless file or folder name made from a name that came over the network:
    no path separators, no characters Windows refuses, no device names."""
    text = text if isinstance(text, str) else ""
    text = "".join("_" if ch in '<>:"/\\|?*' or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Cn") else ch
                   for ch in text)
    text = text.strip(" .")[:limit].strip(" .")  # Windows drops trailing dots and spaces itself
    if not text:
        return fallback
    if text.split(".")[0].strip(" ").upper() in RESERVED:
        text = "_" + text
    return text


def sample_name(path):
    """A harmless file name for a sample, from its path in someone else's .flp."""
    base = re.split(r"[\\/]", path)[-1] if isinstance(path, str) else ""
    stem, dot, ext = base.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,10}", ext):
        stem, ext = base, ""
    name = safe_name(stem, "sample", 80) + ("." + ext if ext else "")
    if ("." + ext).lower() in RUNNABLE:
        name += ".bin"  # nobody should be able to double-click a "sample" into running
    return name


def plugin_sample_name(path):
    """The name a sample that a plugin uses gets next to the project: its own
    (FL looks for a missing file by its name), made harmless; None if it isn't
    an audio file's."""
    base = re.split(r"[\\/]", path)[-1] if isinstance(path, str) else ""
    stem, dot, ext = base.rpartition(".")
    if not dot or ext.lower() not in flp.AUDIO:
        return None
    stem = safe_name(stem, "", 200)
    return stem + "." + ext if stem else None


def inside(root, name):
    """root/name, refusing anything that would end up anywhere but right inside
    root (also through a link on the way)."""
    path = os.path.abspath(os.path.join(root, name))
    base, real = os.path.realpath(root), os.path.realpath(path)
    try:
        if os.path.dirname(real) != base or os.path.commonpath([base, real]) != base:
            raise ValueError(name)
    except ValueError:
        raise TransferError("ogiltigt namn: %r" % (name,)) from None
    return path


def temp_name(path):
    return "%s.%d-%s.part" % (path, os.getpid(), os.urandom(4).hex())


def discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --- what comes from the network ------------------------------------------------------

def clean_snapshot(msg):
    """The fields of a snapshot from the relay, checked, or None."""
    if not isinstance(msg, dict):
        return None
    samples, plugins = msg.get("samples"), msg.get("plugins")
    if not (is_int(msg.get("v"), 1) and isinstance(msg.get("name"), str) and fingerprint(msg.get("fp"))
            and valid_hash(msg.get("flp")) and is_int(msg.get("size")) and isinstance(msg.get("from"), str)
            and isinstance(samples, list) and isinstance(plugins, list)):
        return None
    clean = []
    for sample in samples:
        if not (isinstance(sample, dict) and valid_hash(sample.get("h")) and is_int(sample.get("size"))
                and isinstance(sample.get("path"), str) and sample["path"]):
            return None
        clean.append({"h": sample["h"], "size": sample["size"], "path": sample["path"]})
        if sample.get("plugin") is True:
            clean[-1]["plugin"] = True  # a plugin's: it goes next to the .flp and its path stays
    by = msg.get("by")
    snapshot = {"v": msg["v"], "name": msg["name"], "fp": msg["fp"], "flp": msg["flp"], "size": msg["size"],
                "samples": clean, "plugins": [p for p in plugins if isinstance(p, str)],
                "by": by if isinstance(by, str) and by else "någon", "from": msg["from"]}
    if fl_version(msg.get("flv")):
        snapshot["flv"] = msg["flv"]  # the FL version that saved it
    merged = msg.get("merged")
    if isinstance(merged, list) and merged and all(isinstance(n, str) and n for n in merged):
        snapshot["merged"] = [n[:40] for n in merged[:10]]  # whose versions it includes besides its sharer's
    if msg.get("switch") is True:
        snapshot["switch"] = True  # it replaced the session's project: not one to merge a save of the old with
    return snapshot


def short(snapshot, how=None):
    """What FL hears of a snapshot: its MIDI link is much too slow for the lists.
    `how` tells FL how to get it: "save" (Ctrl+S merges it in) or "open"."""
    notice = {"v": snapshot["v"], "name": snapshot["name"][:64], "fp": snapshot["fp"], "by": snapshot["by"][:40],
              "from": snapshot["from"]}
    if snapshot.get("flv"):
        notice["flv"] = snapshot["flv"]
    if snapshot.get("merged"):
        notice["merged"] = [n[:20] for n in snapshot["merged"][:3]]
    if snapshot.get("switch"):
        notice["switch"] = True
    if how:
        notice["how"] = how
    return notice


def names_text(names):
    """'Kim', 'Kim och Bo', 'Kim, Bo och Cy'."""
    names = [str(n) for n in names if n]
    if len(names) <= 1:
        return "".join(names) or "någon"
    return ", ".join(names[:-1]) + " och " + names[-1]


def clean_notes(msg):
    """A `notes` entry from the relay, checked, or None."""
    if not isinstance(msg, dict):
        return None
    notes = msg.get("notes")
    if not (is_int(msg.get("p"), 1) and is_int(msg.get("c")) and is_int(msg.get("ppq"), 1)
            and isinstance(notes, list) and all(isinstance(n, list) for n in notes)):
        return None
    by, sender = msg.get("by"), msg.get("from")
    return {"p": msg["p"], "c": msg["c"], "v": msg.get("v") if is_int(msg.get("v")) else 0,
            "by": by if isinstance(by, str) else "någon", "from": sender if isinstance(sender, str) else None,
            "ppq": msg["ppq"], "notes": notes}


def clean_outbox(data):
    """(pattern, channel, ppq, notes) from a piano roll script's outbox.json, or None."""
    if not isinstance(data, dict):
        return None
    pattern, channel, ppq, notes = data.get("pattern"), data.get("channel"), data.get("ppq"), data.get("notes")
    if not (is_int(pattern, 1) and is_int(channel) and is_int(ppq, 1) and isinstance(notes, list)
            and len(notes) <= MAX_NOTES):
        return None
    for note in notes:
        if not (isinstance(note, list) and len(note) == NOTE_FIELDS and all(is_number(x) for x in note)):
            return None
    return pattern, channel, ppq, notes


# --- files ---------------------------------------------------------------------------

def file_stat(path):
    """What tells one version of a file from the next, or None if it isn't there."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def file_stem(path):
    """A file's name without its extension, from a Windows path."""
    return re.split(r"[\\/]", path)[-1].rpartition(".")[0] or re.split(r"[\\/]", path)[-1]


def same_file(a, b):
    """Whether two paths name the same file (any case, links followed)."""
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def file_key(path):
    """A project file's path as bridge-state.json keeps it: any case is the same file."""
    return os.path.normcase(os.path.abspath(path))


def flp_files_named(root, name, depth):
    """The files called `name` (casefolded) in `root` and its folders down to
    `depth` levels, leaving out FL's Backup folders (autosaves of FL's own)."""
    found, folders = [], [(root, 0)]
    while folders:
        folder, level = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if level < depth and entry.name.casefold() != "backup":
                        folders.append((entry.path, level + 1))
                elif entry.name.casefold() == name:
                    found.append(os.path.abspath(entry.path))
            except OSError:
                continue
    return found


def parts_differ(a, b, area):
    """Whether structures `a` and `b` differ where keys of `area` point ("c"
    the channels, "m" the mixer: flsync_client.parts_differ)."""
    if a is None or b is None or a == b:
        return False
    if len(a) != 12 or len(b) != 12:
        return True
    return a[:6] != b[:6] if area == "c" else a[6:] != b[6:]


def inside_folder(path, folder):
    """Whether `path` is somewhere inside `folder` (any case, links followed)."""
    try:
        folder = os.path.normcase(os.path.realpath(folder))
        return os.path.commonpath([folder, os.path.normcase(os.path.realpath(path))]) == folder
    except (OSError, ValueError):
        return False


async def replace(source, target, tries=5):
    """os.replace, a few times: on Windows a virus scanner may hold a new file for a moment."""
    for attempt in range(tries):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(0.2)


class Changed(Exception):
    """The file changed while it was about to be written over."""


_ANY = object()


async def write_file(path, data, unchanged=_ANY):
    """Write `path` whole or not at all: nobody ever sees half of it. With
    `unchanged` (a file_stat, None for "not there"), only if the file still
    is as it was then, right up to the moment it is replaced (else Changed)."""
    temp = temp_name(path)
    try:
        with open(temp, "wb") as f:
            f.write(data)
        if unchanged is not _ANY and file_stat(path) != unchanged:
            raise Changed(path)
        await replace(temp, path)
    finally:
        discard(temp)


async def copy_file(source, target):
    """Copy a (maybe big) file a block at a time, so MIDI keeps flowing meanwhile."""
    temp = temp_name(target)
    try:
        with open(source, "rb") as src, open(temp, "wb") as dst:
            while True:
                block = src.read(1 << 20)
                if not block:
                    break
                dst.write(block)
                await asyncio.sleep(0)
        await replace(temp, target)
    finally:
        discard(temp)


def write_json(path, data):
    temp = temp_name(path)
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        discard(temp)


# --- the files connection --------------------------------------------------------------

class TransferError(Exception):
    """The relay refused a transfer, or the files connection broke."""


class FilesLink:
    """The bridge's second connection to the relay, for blobs: one request at a time,
    so big transfers never hold up the session's messages."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.max_blob = 0

    @classmethod
    async def open(cls, host, port, name, password=None):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=MAX_LINE),
                                                CONNECT_TIMEOUT)
        link = cls(reader, writer)
        try:
            hello = {"t": "hello", "app": "files", "proto": PROTO, "name": name}
            if password:
                hello["pw"] = password
            link._send(hello)
            welcome = await link._next()
            if welcome.get("t") != "welcome" or not is_int(welcome.get("max_blob")):
                raise TransferError(problem(welcome))
            link.max_blob = welcome["max_blob"]
        except BaseException:
            writer.close()
            raise
        return link

    @property
    def closed(self):
        return self.writer.is_closing()

    def close(self):
        self.writer.close()

    def _send(self, msg):
        self.writer.write(encode(msg))

    async def _next(self):
        line = await asyncio.wait_for(self.reader.readline(), ANSWER_TIMEOUT)
        if not line:
            raise TransferError("relayn stängde filanslutningen")
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            msg = None
        if not isinstance(msg, dict):
            raise TransferError("obegripligt svar från relayn")
        return msg

    async def missing(self, hashes):
        """Which of these blobs the relay doesn't have."""
        self._send({"t": "blob_has", "h": list(hashes)})
        answer = await self._next()
        if answer.get("t") != "blob_missing" or not isinstance(answer.get("h"), list):
            raise TransferError(problem(answer))
        return [h for h in answer["h"] if isinstance(h, str)]

    async def upload_bytes(self, h, data):
        count = max(1, -(-len(data) // CHUNK))
        for i in range(count):
            await self._chunk(h, i, count, data[i * CHUNK:(i + 1) * CHUNK])
        await self._uploaded(h)

    async def upload_file(self, h, path, size):
        count = max(1, -(-size // CHUNK))
        with open(path, "rb") as f:
            for i in range(count):
                await self._chunk(h, i, count, f.read(CHUNK))
        await self._uploaded(h)

    async def _chunk(self, h, i, count, block):
        self._send({"t": "blob", "h": h, "i": i, "n": count, "d": base64.b64encode(block).decode("ascii")})
        await self.writer.drain()

    async def _uploaded(self, h):
        answer = await self._next()
        if answer.get("t") != "blob_ok" or answer.get("h") != h:
            raise TransferError(problem(answer))

    async def fetch(self, h, size, target):
        """Download blob `h`, which should be `size` bytes, to `target` - only if
        it is exactly what its name (the sha256) says."""
        self._send({"t": "blob_get", "h": h})
        digest, got, index = hashlib.sha256(), 0, 0
        temp = temp_name(target)
        try:
            with open(temp, "wb") as out:
                while True:
                    answer = await self._next()
                    if answer.get("t") != "blob":
                        raise TransferError(problem(answer))
                    count = answer.get("n")
                    if answer.get("h") != h or answer.get("i") != index or not is_int(count, 1):
                        self.close()  # out of step with the relay: this connection is done
                        raise TransferError("relayn skickade fel del av filen")
                    try:
                        block = base64.b64decode(answer.get("d"), validate=True)
                    except (binascii.Error, ValueError, TypeError):
                        self.close()
                        raise TransferError("trasig del av filen från relayn") from None
                    got += len(block)
                    if got > size:
                        self.close()
                        raise TransferError("filen är större än versionen säger")
                    out.write(block)
                    digest.update(block)
                    index += 1
                    if index == count:
                        break
            if got != size or digest.hexdigest() != h:
                raise TransferError("kontrollsumman stämmer inte, filen är trasig")
            await replace(temp, target)
        finally:
            discard(temp)


def problem(answer):
    return str(answer.get("msg") or "oväntat svar från relayn: %s" % answer.get("t"))


# --- the bridge ------------------------------------------------------------------------

class Bridge:
    """Joins FL to the relay. Everything runs on one asyncio event loop.

    `incoming` takes packed MIDI events from the port (any thread may append);
    `send_to_fl` is called with packed MIDI events for FL, paced by pump().
    start() begins watching the project file and the notes exchange folder.

    Every place on disk can be given (the tests use temporary folders):
    `project` the .flp to share (else FL's newest recent file, from `mru()`),
    `cache_dir` downloaded blobs, `projects_dir` where others' versions are
    written, `notes_dir` the exchange folder of the piano roll scripts,
    `plugin_db` FL's plugin database, `fl_dirs` FL's folders as fl_folders()
    gives them. `fl_title()` gives FL's main window title (fl_window_title()),
    which with `mru()` tells whether FL opened a version written here.
    `state_path` is bridge-state.json (next to the cache folder unless given):
    the versions FL may have open, and which one it has (docs/phase4.md).

    `auto_open` opens every version someone else shares in FL as it arrives;
    `open_merged` (on by default) opens the result of merging a save, once FL
    has nothing unsaved and doesn't play.
    """

    def __init__(self, relay_host, relay_port, send_to_fl, logger=log, name=None, project=None, mru=None,
                 cache_dir=None, projects_dir=None, notes_dir=None, plugin_db=None, fl_dirs=None,
                 auto_open=False, opener=None, password=None, fl_title=None, state_path=None, open_merged=True,
                 flp_writes=None, mru_all=None, fl_open=None, auto_sync=True):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.send_to_fl = send_to_fl
        self.log = logger
        self.name = name  # shown to the others instead of FL's Windows user name
        self.password = password or None  # the session's, if the relay has one
        self.incoming = collections.deque()
        self.outgoing = collections.deque()  # (kind, data1) parts, numbered when sent
        self.tokens, self.paced_at = float(EVENT_BURST), None  # what may go out now (pump_once)
        self.queued = self.sent = 0  # parts put in outgoing / sent, ever
        self.welcome_end = 0     # `queued` after the welcome: it is on its way to FL until `sent` gets there
        self.encoder = wire.Encoder(wire.TO_FL)
        self.decoder = wire.Decoder(wire.FROM_FL)
        self.session = None  # task running the current relay connection
        self.writer = None
        self.fl_name = None
        self.hello_name = None    # the name in FL's last hello, before --name
        self.app_version = None   # the launcher's FLSync version, told to the others in the hello
        self.fl_last_heard = None
        self.fl_open = fl_window_open if fl_open is None else fl_open  # whether FL's window is open (fl_window_open)
        self.fl_busy = False      # FL is silent but its window is open: it stays in the session for now
        self.fl_seen_open = True  # what fl_open() said last (asked once per FL_CHECK_INTERVAL)
        self.next_fl_check = 0.0
        self.left_silent = False  # the relay was left because FL went silent: tell FL once it's heard again
        self.next_heartbeat = 0.0

        fl_dirs = fl_folders() if fl_dirs is None else fl_dirs
        fl_studio = os.path.join(fl_dirs.get("user_dir") or os.path.join(documents_dir(), "Image-Line"), "FL Studio")
        if plugin_db is None:
            plugin_db = os.path.join(fl_studio, "Presets", "Plugin database", "Installed")
        self.project = os.path.abspath(project) if project else None
        self.mru = read_mru if mru is None else mru
        self.cache_dir = cache_dir or os.path.join(local_data_dir(), "cache")
        self.projects_dir = projects_dir or os.path.join(fl_studio, "Projects", "FLSync")
        self.notes_dir = notes_dir or os.path.join(local_data_dir(), "notes")
        self.plugin_db = plugin_db
        self.fl_dirs = fl_dirs
        self.auto_open = auto_open
        self.opener = opener or open_file
        self.fl_title = fl_window_title if fl_title is None else fl_title
        # The .flp files written lately, [(time.monotonic(), path)] (flpwatch.FlpWatcher.recent), and every file
        # in FL's recent-files lists: with the title, where FL's project is (_watched_path). None: not watched.
        self.flp_writes = flp_writes
        self.mru_all = read_mru_all if mru_all is None else mru_all
        self.watch_interval = WATCH_INTERVAL
        self.project_interval = PROJECT_INTERVAL
        self.stable_time = STABLE_TIME
        self.share_retry = SHARE_RETRY
        self.context_interval = CONTEXT_INTERVAL
        self.loaded_interval = LOADED_INTERVAL

        # What the bridge knows about FL and the session.
        self.fl_fp = None        # FL's structure fingerprint, from its hello and fp messages
        self.fl_version = None   # FL's version text, from its hello
        self.reports = 0         # how many times FL has said its structure (hello, fp)
        self.report = None       # (number, when, fp, "hello" or "fp") of the newest time
        self.my_id = None        # our id in the session, from the welcome
        self.session_fp = None   # the session's structure: welcome, snapshots, reshare
        self.newest = None       # the newest snapshot heard of in this session
        self.generation = 0      # counts welcomes: a new one may be a new relay, with new numbers

        # The project file and the snapshots.
        self.watched = None      # the project file being watched
        self.watched_stat = None
        self.changed_at = None   # when it was last seen changing; None: nothing waits to be shared
        self.retry_at = 0.0
        self.next_project = 0.0
        self.sharing = False
        self.last_sha = None     # the .flp content last shared or received
        self.unshareable = None  # content that can't be shared (unreadable, too big): not tried again
        self.shared = set()      # .flp hashes this bridge shared
        self.received = {}       # .flp hash -> the project file written for it
        self.downloaded = set()  # .flp hashes of versions whose files are all in the cache
        self.to_open = None      # the version written last, to tell FL `loaded` when it opens it (_check_loaded)
        self.next_loaded = 0.0
        self.title_names = False  # FL's window title has been seen naming its project
        self.hashes = {}         # (path, size, mtime, inode) -> sha256 of a local file
        self.files = None        # the files connection, opened when needed
        self.lock = asyncio.Lock()  # one transfer at a time
        self.tasks = set()
        self.watcher = None
        self.warned_no_fp = False

        # Piano-roll notes.
        self.pr = None           # (pattern, channel, name) FL's piano roll shows
        self.notes = {}          # (pattern, channel) -> the session's newest notes for it
        self.next_context = 0.0
        self.outbox_broken = None  # since when outbox.json has been there without being readable

        # Merging saves (docs/phase4.md).
        self.state_path = state_path or os.path.join(os.path.dirname(os.path.abspath(self.cache_dir)), STATE_FILE)
        self.open_merged = open_merged
        self.peers = {}          # id -> info of the others in the session
        self.known = collections.OrderedDict()  # version sha256 -> {v, name, tokens}: versions FL may have open
        self.aliases = {}        # sha256 of a file written here -> the version it holds
        self.base = None         # the version FL's open project comes from (its sha256), None: unknown
        self.base_project = None  # the project file that was
        self.identifying = None  # the task finding out which project FL just loaded
        self.wrote = {}          # file_key -> file_stat of the version files written here (not FL's saves)
        self.fl_dirty = None     # FL's `st`: 0 nothing unsaved, else unsaved changes; None: never said
        self.fl_playing = None
        self.fl_saved_at = None  # when FL last said its unsaved changes are gone (it saved, or loaded)
        self.fl_loaded_at = None  # when FL last said it loaded a project (`opened`)
        self.launched = None     # (path, when) of the last file the bridge asked FL to open (until FL loads another)
        self.to_launch = None    # a version written for FL, to open once FL is clean and stopped
        self.share_answer = None  # (sha256, future) of a snap_put waiting for the relay's answer
        self.force_share = False  # "Dela mitt projekt": the next share replaces the session's project
        self.unrelated = False   # FL's project isn't the session's: its saves aren't shared
        self.unrelated_at = None  # when such a save was refused: saving again within CONFIRM_TIME shares it
        self.last_saved = None   # the outcome of the last save, for the launcher's page

        # Content without Ctrl+S (docs/phase5.md): FL saves by itself, a save with
        # nothing new isn't shared, and others' versions come in through a save.
        self.auto_sync = auto_sync
        self.autosave_sent = None  # what FL last heard: may it save by itself
        self.autosave = None     # {at, content, pull, seen}: FL said it saves by itself now
        self.autosave_failures = 0  # FL didn't save when it said it would, this many times in a row...
        self.autosave_retry = 0.0  # ...so it isn't asked to before this (AUTOSAVE_TRIES: never again)
        self.backups_kept = {}   # project name (casefolded): (time, path) of the last copy FL's Backup folder kept for a save by itself
        self.backups_told = False  # the log said why those copies go
        self.pull_sent = None    # the version FL was asked to get in (`pull`)
        self.search = None       # (name, when, path or None): the last search for the file FL's title names
        self._load_state()

    # --- from FL -----------------------------------------------------------

    def _from_fl(self, message):
        status, data1, data2 = wire.unpack(message)
        if not self.decoder.accepts(status):
            return  # our own events echoed by a shared port, or other MIDI
        now = time.monotonic()
        if self.fl_last_heard is None:
            self.log("FL hörs på MIDI")
            if self.left_silent and self.session is None:
                # FL may not have noticed that we left (it was busy all along):
                # this makes its script join again.
                self.to_fl({"t": "link", "up": False, "msg": "bryggan lämnade sessionen när FL inte svarade"})
            self.left_silent = False
        elif self.fl_busy:
            self.log("FL svarar igen efter %.0f s" % (now - self.fl_last_heard))
        self.fl_busy = False
        self.fl_last_heard = now
        msg = self.decoder.feed(status, data1, data2)
        if msg is None or msg is wire.HEARTBEAT:
            return
        kind = msg.get("t")
        if kind == "log":  # FL can't write files, so the script logs through us
            self.log("FL: %s" % json.dumps(msg.get("data"), ensure_ascii=False))
        elif kind == "hello":
            self.fl_fp = fingerprint(msg.get("fp"))
            self.fl_version = fl_version(msg.get("flv"))
            self.autosave_sent = self.pull_sent = None  # a new run of the script hears them again
            self.autosave_failures, self.autosave_retry = 0, 0.0  # (and whatever dialog stopped it is gone)
            self._start_session(msg)
            self._reported("hello")
        elif kind == "autosave":
            # FL saves by itself right after this: that save is told apart (_share_now).
            content = msg.get("content")
            self.autosave = {"at": now, "content": content if is_int(content) else 1, "pull": msg.get("pull") is True,
                             "check": msg.get("check") is True, "seen": False}
        elif kind == "pr":
            self._on_pr(msg)  # for the piano roll scripts; the relay never hears it
        elif kind == "st":
            self._on_status(msg)  # FL's unsaved changes and playback: when a version may be opened
        elif kind == "opened":
            self._on_opened(msg)  # FL loaded a project: which one is found out here
        else:
            if kind == "fp":
                self.fl_fp = fingerprint(msg.get("fp"))
            self._to_relay(msg)
            if kind == "fp":
                self._reported("fp")

    def _to_relay(self, msg):
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(encode(msg))
            return True
        return False

    # --- to FL -------------------------------------------------------------

    def to_fl(self, msg):
        parts = wire.parts(wire.to_ascii_payload(msg))
        self.outgoing.extend(parts)
        self.queued += len(parts)
        if msg.get("t") == "welcome":
            self.welcome_end = self.queued

    def _from_relay(self, line):
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("t")
        if kind == "pong":
            return  # the keepalive's answer: hearing it was the point
        if self._answers_share(msg):
            return  # how our snap_put went: _share_now tells FL
        if kind == "welcome":
            self._on_welcome(msg)
        elif kind == "snapshot":
            msg = self._on_snapshot(msg)
        elif kind == "notes":
            msg = self._on_notes(msg)
        elif kind == "reshare":
            # Our FL's structure is the session's now, and the relay dropped
            # everything it had for the old one, notes too.
            self.session_fp = self.fl_fp
            self._set_notes([])
        elif kind == "peer_join":
            if isinstance(msg.get("id"), str):
                self.peers[msg["id"]] = msg
        elif kind == "peer_leave":
            self.peers.pop(msg.get("id"), None)
        elif kind == "note":
            self._on_note(msg)
        elif kind == "error":
            self.log("relayn: %s" % msg.get("msg"))
        if msg is not None:
            self.to_fl(msg)

    def _on_note(self, msg):
        """Someone else's FL did something worth knowing (relay `note`)."""
        if msg.get("kind") == "unrelated":
            self.log("%s sparade '%s', som inte är sessionens projekt, så det delades inte. Sparar hen igen inom "
                     "%d sekunder blir det allas projekt" % (msg.get("by"), msg.get("name"), CONFIRM_TIME))

    def _answers_share(self, msg):
        """Whether `msg` answers the snap_put we wait for: our snapshot coming
        back (taken), or the relay's refusal. The snapshot still goes on as usual."""
        if self.share_answer is None:
            return False
        sha, future = self.share_answer
        if future.done():
            return False
        kind = msg.get("t")
        if kind == "snapshot" and msg.get("flp") == sha and msg.get("from") == self.my_id and self.my_id:
            future.set_result(("ok", msg.get("v")))
            return False
        if kind == "error" and msg.get("code") == "outdated":
            if msg.get("v") == 0:
                # The relay has no version (it dropped one of another structure
                # when the session took a new one): the next try shares as the first.
                self.newest = None
            future.set_result(("outdated", msg.get("v")))
            return True
        if kind == "error" and str(msg.get("msg") or "").startswith("versionen togs inte emot"):
            future.set_result(("error", str(msg.get("msg"))))
            return True
        return False

    # --- the relay connection ------------------------------------------------

    def _start_session(self, hello):
        """FL said hello: (re)join the relay on its behalf."""
        if self.session is not None and not self.session.done():
            if self.writer is None:
                return  # still connecting for the previous hello; its welcome will answer this one too
            if self.my_id is not None and self.sent < self.welcome_end and hello.get("name") == self.hello_name:
                # FL asked again while its welcome is still on the way (a big
                # session takes seconds over MIDI): that welcome answers it.
                return
        self.hello_name = hello.get("name")
        if self.name:
            hello = dict(hello, name=self.name)
        if self.app_version:
            hello = dict(hello, ver=self.app_version)  # so the others can tell when FLSyncs differ
        if self.password:
            hello = dict(hello, pw=self.password)
        self.close_session()
        # What was on its way to FL belongs to the session it just left (a
        # welcome, values): the new one sends what FL needs.
        self.outgoing.clear()
        self.welcome_end = self.queued = self.sent = 0
        self.fl_name = hello.get("name")
        self.session = asyncio.ensure_future(self._run_session(hello))

    async def _run_session(self, hello):
        address = "%s:%s" % (self.relay_host, self.relay_port)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.relay_host, self.relay_port, limit=MAX_LINE),
                CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            reason = "når inte relayn på %s (%s)" % (address, str(e) or e.__class__.__name__)
            self.log(reason)
            self.to_fl({"t": "link", "up": False, "msg": reason})
            return
        self.writer = writer
        writer.write(encode(hello))
        self.log("%s ansluter till relayn på %s" % (self.fl_name, address))
        pinger = asyncio.ensure_future(self._ping(writer))
        reason = "relayn stängde anslutningen"
        try:
            async for line in self._lines(reader):
                try:
                    self._from_relay(line)
                except Exception:  # a bug here must not cut FL off from the session
                    self.log("fel:\n" + traceback.format_exc())
        except asyncio.TimeoutError:
            # A connection that just goes quiet (Wi-Fi gone, a sleeping laptop)
            # never ends by itself: without this FL would wait for minutes.
            reason = "relayn har inte hörts av på %d sekunder" % RELAY_TIMEOUT
        except (ConnectionError, ValueError):
            pass
        finally:
            pinger.cancel()
            writer.close()
            if self.writer is writer:
                self.writer = None
                self.my_id = None
                self.peers = {}
            if self.share_answer is not None and not self.share_answer[1].done():
                self.share_answer[1].set_result(("error", reason))
        self.log(reason)
        self.to_fl({"t": "link", "up": False, "msg": reason})

    @staticmethod
    async def _lines(reader):
        """The relay's lines. Any byte from it counts as a sign of life (a long
        welcome can take a while on a slow line); RELAY_TIMEOUT without one
        raises TimeoutError."""
        parts, size = [], 0  # the line so far, kept in pieces: a big welcome is joined once
        while True:
            block = await asyncio.wait_for(reader.read(1 << 16), RELAY_TIMEOUT)
            if not block:
                return
            pieces = block.split(b"\n")
            if len(pieces) > 1:
                yield b"".join(parts) + pieces[0] + b"\n"
                for piece in pieces[1:-1]:
                    yield piece + b"\n"
                parts, size = [], 0
            if pieces[-1]:
                parts.append(pieces[-1])
                size += len(pieces[-1])
                if size > MAX_LINE:
                    raise ValueError("för lång rad från relayn")

    async def _ping(self, writer):
        """Keep the relay talking: it answers `pong`, which _lines counts."""
        while not writer.is_closing():
            await asyncio.sleep(PING_INTERVAL)
            if not writer.is_closing():
                writer.write(encode({"t": "ping"}))

    def close_session(self):
        if self.session is not None and not self.session.done():
            self.session.cancel()  # a cancelled session doesn't report "link down" to FL
        self.session = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        self.my_id = None
        self.peers = {}
        if self.share_answer is not None and not self.share_answer[1].done():
            self.share_answer[1].set_result(("error", "sessionen avslutades"))

    def in_session(self):
        return self.my_id is not None and self.writer is not None and not self.writer.is_closing()

    # --- the MIDI side ----------------------------------------------------------

    async def pump(self):
        while True:
            self.pump_once(time.monotonic())
            await asyncio.sleep(PUMP_INTERVAL)

    def pump_once(self, now):
        while self.incoming:
            try:
                self._from_fl(self.incoming.popleft())
            except Exception:  # a bug in handling one message must not stop MIDI for good
                self.log("fel:\n" + traceback.format_exc())
        if now >= self.next_heartbeat:
            self.send_to_fl(self.encoder.heartbeat())
            self.next_heartbeat = now + HEARTBEAT_INTERVAL
        clock = time.perf_counter()  # (time.monotonic() moves in 15.6 ms steps on Windows)
        if self.paced_at is not None:
            self.tokens = min(float(EVENT_BURST), self.tokens + (clock - self.paced_at) * EVENTS_PER_SECOND)
        self.paced_at = clock
        if not self.fl_busy:  # a busy FL reads nothing: messages wait here rather than in the MIDI driver
            for _ in range(min(int(self.tokens), len(self.outgoing))):
                self.send_to_fl(self.encoder.event(*self.outgoing.popleft()))
                self.tokens -= 1
                self.sent += 1
        heard = self.fl_last_heard
        if heard is None or now - heard <= FL_TIMEOUT:
            return
        if self.session is not None and now - heard < FL_GONE and self._fl_still_open(now):
            # Loading a plugin or a project, saving, a dialog: FL's script is
            # silent until it's done. Leaving would drop FL from the session.
            if not self.fl_busy:
                self.fl_busy = True
                self.log("FL svarar inte just nu (laddar eller sparar kanske något), stannar kvar i sessionen")
            return
        self.fl_last_heard = None
        self.fl_busy = False
        self.decoder = wire.Decoder(wire.FROM_FL)
        if self.session is not None:
            self.log("FL svarar inte längre, lämnar relayn")
            self.close_session()
            self.left_silent = True

    def _fl_still_open(self, now):
        """Whether FL's window is still there (asked at most once per FL_CHECK_INTERVAL)."""
        if now >= self.next_fl_check:
            self.next_fl_check = now + FL_CHECK_INTERVAL
            try:
                self.fl_seen_open = self.fl_open() is not False  # can't tell: as if it were
            except Exception:
                self.fl_seen_open = True
        return self.fl_seen_open

    # --- running ---------------------------------------------------------------

    def start(self):
        """Start watching the project file and the notes exchange folder."""
        if self.watcher is None:
            self.watcher = asyncio.ensure_future(self.watch())

    def close(self):
        """Stop everything: the program ends."""
        if self.watcher is not None:
            self.watcher.cancel()
            self.watcher = None
        for task in list(self.tasks):
            task.cancel()
        self.close_session()
        self._drop_files()
        try:
            os.remove(os.path.join(self.notes_dir, "context.json"))  # the scripts know at once we're gone
        except OSError:
            pass

    async def watch(self):
        while True:
            try:
                self.watch_once(time.monotonic())
            except Exception:  # a surprise must not end the watching
                self.log("fel:\n" + traceback.format_exc())
            await asyncio.sleep(self.watch_interval)

    def watch_once(self, now):
        if now >= self.next_project:
            self.next_project = now + self.project_interval
            self._watch_project(now)
            self._offer_autosave(now)
            self._check_pull()
        self._watch_outbox(now)
        if self.pr is not None and now >= self.next_context:
            self._write_context()
        if self.to_open is not None and now >= self.next_loaded:
            self.next_loaded = now + self.loaded_interval
            self._check_loaded(now)
        if self.to_launch is not None:
            self._try_launch()

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            e = task.exception()
            self.log("fel:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))

    async def _files(self):
        if self.files is None or self.files.closed:
            self.files = await FilesLink.open(self.relay_host, self.relay_port,
                                              self.name or self.fl_name or "brygga", password=self.password)
        return self.files

    def _drop_files(self):
        if self.files is not None:
            self.files.close()
            self.files = None

    async def _hash(self, path):
        """(sha256, size) of a local file, read a block at a time; remembered
        until the file changes."""
        stat = file_stat(path)
        key = (os.path.abspath(path),) + (stat or ())
        if stat is not None and key in self.hashes:
            return self.hashes[key], stat[1]
        digest, size = hashlib.sha256(), 0
        with open(path, "rb") as f:
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                await asyncio.sleep(0)
        if stat is not None and stat == file_stat(path):
            if len(self.hashes) > 10000:
                self.hashes.clear()
            self.hashes[key] = digest.hexdigest()
        return digest.hexdigest(), size

    # --- the project file: sharing ------------------------------------------------

    def _watched_path(self):
        """The project file FL has open. FL keeps its recent-files list in memory
        (the registry has it as of FL's last exit), so its window title says
        which file, by name, and the place comes from: a .flp of that name
        written when FL said it saved (flp_writes), the file the bridge asked FL
        to open, the file watched now, the one FL had when the bridge last ran
        (bridge-state.json: the launcher may restart while FL keeps it open), a
        version written here, a project file whose version is known, the recent
        files. A title naming a file found nowhere: None (FL's next save shows
        where). Without a title, the newest recent file."""
        if self.project:
            return self.project
        try:
            recent = self.mru()
        except Exception:
            recent = None
        recent = os.path.abspath(recent) if isinstance(recent, str) and recent.lower().endswith(".flp") else None
        try:
            name = title_file(self.fl_title())
        except Exception:
            name = None
        if name is None:
            return recent
        key = name.casefold()

        def named(path):
            return isinstance(path, str) and file_stem(path).casefold() == key

        for path in self._saved_by_fl():
            if named(path):
                return path
        launched = self.launched
        if launched is not None and self.fl_dirty is not None and (self.fl_loaded_at or 0) < launched[1]:
            launched = None  # FL tells its loads (`opened` comes with `st`) and hasn't loaded one since
        candidates = [launched[0] if launched else None, self.watched, self.base_project]
        candidates += list(reversed(list(self.received.values()))) + list(self.wrote)
        candidates += list(reversed(list(self.lineage)))
        try:
            candidates += list(self.mru_all() or ())
        except Exception:
            pass
        candidates.append(recent)
        for path in candidates:
            if named(path) and os.path.isfile(path):
                return os.path.abspath(path)
        return self._find_by_name(name, candidates)

    def _find_by_name(self, name, recent):
        """A project FL opened from somewhere none of the above saw (its browser,
        File > Open): the only file of that name in FL's projects folder or a
        folder of a recent file. Two of that name: None, as no file at all."""
        key, now = name.casefold(), time.monotonic()
        if self.search is not None and self.search[0] == key and now - self.search[1] < SEARCH_KEPT:
            return self.search[2]
        roots = []
        user = self.fl_dirs.get("user_dir")
        if user:
            roots.append((os.path.join(user, "FL Studio", "Projects"), SEARCH_DEPTH))
        roots += [(os.path.dirname(path), 0) for path in recent if isinstance(path, str) and path]
        found = {}
        for root, depth in roots:
            for path in flp_files_named(root, key + ".flp", depth):
                found[file_key(path)] = path
        result = next(iter(found.values())) if len(found) == 1 else None
        self.search = (key, now, result)
        return result

    def _saved_by_fl(self):
        """The .flp files written lately that FL wrote, likeliest first: not by
        this bridge, and just before FL said its changes were saved (FL writes
        the file, then clears its changed flag), the closest to that first.
        None once FL has loaded a project since."""
        if self.flp_writes is None:
            return []
        try:
            writes = self.flp_writes()
        except Exception:
            return []
        now, saved = time.monotonic(), self.fl_saved_at
        if saved is not None and self.fl_loaded_at is not None and self.fl_loaded_at >= saved - SAVED_LATE:
            return []  # FL loaded a project since (its changes going to 0 then was the load): it's elsewhere
        found = []
        for when, path in reversed(writes):
            if now - when > WRITES_KEPT:
                continue
            if saved is None and self.fl_dirty is not None:
                continue  # FL says when it saves (`st`), and it hasn't: someone else's .flp
            if saved is not None and not saved - SAVED_WINDOW <= when <= saved + SAVED_LATE:
                continue  # someone else's .flp (FL said it saved at another time)
            if self.wrote.get(file_key(path)) == file_stat(path):
                continue  # our own: a version written here
            found.append((0.0 if saved is None else abs(when - saved), os.path.abspath(path)))
        found.sort(key=lambda item: item[0])  # stable: the newest first otherwise
        return [path for _, path in found]

    def _watch_project(self, now):
        path = self._watched_path()
        if path != self.watched:
            # Another project is open now. That by itself isn't a save: it is
            # shared once FL writes it. A file FL wrote as it saved (or one
            # written moments ago), though, was just saved under this name
            # (Save as), so it counts as a change (a version we wrote ourselves
            # is recognised by its hash), and it is still the project FL had
            # (its base goes with it).
            stat = file_stat(path)
            self.watched, self.watched_stat, self.changed_at = path, stat, None
            if path:
                self.log("Bevakar projektet %s" % path)
                ours = stat is not None and self.wrote.get(file_key(path)) == stat  # a version written here
                saved = stat is not None and (path in self._saved_by_fl()
                                              or time.time() - stat[0] / 1e9 < SAVE_AS_WINDOW)
                if saved and not ours:
                    self.changed_at = now
                    if self.autosave is not None:
                        self.autosave["seen"] = True
                    if self.base is not None:
                        self._set_base(self.base, path)
                elif ours or self.base_project is None or not same_file(path, self.base_project):
                    self._identify_soon()  # FL opened it: which version is it?
            return
        if path is None:
            return
        stat = file_stat(path)
        if stat is None:
            return
        if stat != self.watched_stat:
            if self.wrote.get(file_key(path)) == stat:
                self.watched_stat = stat  # a version written here (FL had it open): no save to tell FL about
                return
            self.watched_stat, self.changed_at = stat, now  # FL is saving it: wait until it's done
            if self.autosave is not None:
                self.autosave["seen"] = True  # FL did save when it said it would
            return
        if self.changed_at is None or now - self.changed_at < self.stable_time or now < self.retry_at:
            return
        if self.sharing or not self.in_session():
            return  # shared once FL is in the session
        if self.fl_fp is None:
            if not self.warned_no_fp:
                self.warned_no_fp = True
                self.log("FL har inte sagt vilken struktur projektet har, så det kan inte delas")
            return
        self.changed_at = None
        self.sharing = True
        self._spawn(self._share(path))

    async def _share(self, path):
        done = False
        try:
            async with self.lock:
                done = await self._share_now(path)
        except (OSError, TransferError, asyncio.TimeoutError, ValueError) as e:
            self.log("Kunde inte dela projektet: %s" % (str(e) or e.__class__.__name__))
            self._drop_files()
        finally:
            self.sharing = False
        if not done:  # something on the way failed: try again a little later
            self.changed_at = time.monotonic()
            self.retry_at = self.changed_at + self.share_retry

    async def _share_now(self, path):
        """FL saved the project: share it with the session (docs/phase4.md,
        Saving). As it is when the session's newest version is the one FL's
        project comes from (its base); merged with the newest first when
        someone else shared since. False if it should be tried again later."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.log("Kunde inte läsa %s: %s" % (path, e))
            return False
        sha = hashlib.sha256(data).hexdigest()
        auto, self.autosave = self.autosave, None  # FL saved by itself (docs/phase5.md)
        if auto is not None:
            self.autosave_failures = 0
            self._thin_backups(path, auto["at"])
        if sha == self.last_sha or sha == self.unshareable:
            self._tell_saved("unchanged")
            return True  # nothing new: saved without changes, or the version we wrote ourselves
        name = os.path.splitext(os.path.basename(path))[0]
        if not self.force_share and self._is_version(sha):
            # FL stamps every save with its work time, so no save of FL's is a
            # version that exists already: something copied one over the file
            # FL has open (a synced folder, someone's FLSync on this PC). FL's
            # project and its base are as they were; nothing new to share.
            self.log("'%s' ändrades utanför FL till en version som redan finns, så det delas inte" % name)
            return True
        try:
            project = await asyncio.to_thread(flp.parse, data)  # a big project mustn't hold up MIDI
        except flp.FlpError as e:
            self.unshareable = sha
            self.log("'%s' går inte att läsa som ett FL-projekt (%s), så det delas inte" % (name, e))
            self._tell_saved("failed", why="projektfilen gick inte att läsa")
            return True
        if not self.in_session():
            return False
        files = await self._files()
        if len(data) > files.max_blob:
            self.unshareable = sha
            self.log("'%s' är större än relayns gräns på %s, så det delas inte"
                     % (name, megabytes(files.max_blob)))
            self._tell_saved("failed", why="projektet är större än sessionens gräns")
            return True
        samples = await self._local_samples(project, files.max_blob)
        if len({s["h"] for s in samples} - {sha}) >= MAX_LIST or len(samples) > MAX_LIST:
            self.unshareable = sha
            self.log("'%s' använder för många samples (%d) för att delas" % (name, len(samples)))
            self._tell_saved("failed", why="för många samples")
            return True
        mine = {"path": path, "name": name, "data": data, "sha": sha, "project": project, "samples": samples,
                "tokens": {s["path"]: s["h"] for s in samples if not s.get("plugin")}}
        if auto is not None and not self.force_share and await self._nothing_new(mine, auto):
            return await self._settle(mine, self.newest)
        force, self.force_share = self.force_share, False
        for _ in range(SHARE_TRIES):
            if not self.in_session() or self.fl_fp is None:
                return False
            newest = self.newest
            if newest is not None and newest["flp"] == sha:
                self._set_base(sha, path)  # FL saved exactly the session's newest version
                self._tell_saved("unchanged")
                return True
            if force:
                done = await self._share_mine(mine, newest, "shared", force=True)  # "Dela mitt projekt"
            elif newest is None or newest["flp"] == self.base:
                done = await self._share_mine(mine, newest, "shared")  # nobody else shared since: fast forward
            elif newest.get("switch") and newest["flp"] not in self.shared:
                done = await self._not_ours(mine, newest, quiet=auto is not None)  # a save of the project the session left: FL opens the new
            elif self.base is None and self._other_fls():
                done = await self._not_ours(mine, newest, quiet=auto is not None)
            elif self.base is None:
                done = await self._share_mine(mine, newest, "shared")  # alone: the project becomes the session's
            else:
                done = await self._merge_and_share(mine, newest)
            if done != "outdated":
                return done
            self.log("Någon delade en nyare version under tiden, slår ihop med den")
        self.log("De andra delar nya versioner hela tiden, försöker igen om en stund")
        return False

    async def _not_ours(self, mine, newest, quiet=False):
        """A save of a project that isn't the session's (FL didn't open it from
        a version, and it was never shared): others work on the session's
        project, so it isn't shared, and they hear of it. Saving it again
        within CONFIRM_TIME shares it as everyone's project, as "Dela mitt
        projekt" (share_mine) does. If someone else made another project the
        session's (a switch), this is the old one: FL opens the new one.
        `quiet`: FL saved by itself, which never makes it everyone's."""
        now = time.monotonic()
        if newest is not None and newest.get("switch") and newest["flp"] not in self.shared:
            self.unshareable = mine["sha"]
            path = self.received.get(newest["flp"])
            title = self.to_open["title"] if self.to_open and self.to_open.get("flp") == newest["flp"] else None
            how = self._launch(path, newest, title, "switched") if path and os.path.isfile(path) else "no"
            self.log("'%s' delas inte: %s bytte sessionens projekt till '%s'%s" % (
                mine["name"], newest["by"], newest["name"], ", som öppnas nu" if how == "now" else ""))
            self._tell_saved("old", v=newest["v"], others=[newest["by"]], open=how)
            return True
        if quiet:
            self.unshareable, self.unrelated = mine["sha"], True
            self._tell_saved("unrelated")
            return True
        if self.unrelated_at is not None and now - self.unrelated_at <= CONFIRM_TIME:
            self.unrelated_at = None
            self.log("'%s' sparades igen, så det blir sessionens projekt" % mine["name"])
            return await self._share_mine(mine, newest, "switched", force=True)
        self.unshareable = mine["sha"]
        self.unrelated_at = now
        if not self.unrelated:
            self.log("'%s' delas inte: projektet hör inte till sessionen. Spara igen (Ctrl+S) inom %d sekunder för "
                     "att göra det till allas projekt, eller öppna sessionens version i FLSync-fönstret"
                     % (mine["name"], CONFIRM_TIME))
        self.unrelated = True
        self._to_relay({"t": "note", "kind": "unrelated", "name": mine["name"][:MAX_NAME]})
        self._tell_saved("unrelated", confirm=int(CONFIRM_TIME))
        return True

    def share_mine(self):
        """Share FL's open project even though it isn't the session's ("Dela
        mitt projekt" on the launcher's page): the next share replaces the
        session's project, newest wins. False if there is no project to share."""
        if not self.watched or not os.path.isfile(self.watched):
            return False
        self.force_share = True
        self.unshareable = self.last_sha = None
        self.changed_at, self.retry_at = time.monotonic() - self.stable_time, 0.0
        return True

    async def _share_mine(self, mine, newest, outcome, why=None, others=(), force=False):
        """Share FL's saved file as it is: it includes the newest version (fast
        forward), has everything the newest has, or replaces it (`force`, or a
        merge that was refused: `why`)."""
        parent = newest["v"] if newest else 0
        # Replacing the session's project (`force`) is a switch: the others don't
        # merge their saves of the old one with it, they open it instead.
        answer = await self._put(mine["data"], mine["sha"], mine["name"], mine["project"], mine["samples"],
                                 parent, self.fl_fp, switch=force and newest is not None)
        if answer[0] == "outdated" and not force:
            return "outdated"
        if answer[0] == "outdated":  # replacing: whatever came meanwhile is replaced too
            newest = self.newest
            answer = await self._put(mine["data"], mine["sha"], mine["name"], mine["project"], mine["samples"],
                                     newest["v"] if newest else 0, self.fl_fp, switch=newest is not None)
        if answer[0] != "ok":
            self.log("Kunde inte dela '%s': %s" % (mine["name"], answer[1]))
            self._tell_saved("failed", why=str(answer[1]))
            return False
        self._remember(mine["sha"], answer[1], mine["name"], mine["tokens"], mine["data"])
        self._set_base(mine["sha"], mine["path"])
        self.unrelated = False
        self._tell_saved(outcome, v=answer[1], others=others, why=why)
        return True

    async def _nothing_new(self, mine, auto):
        """Whether a save FL made by itself has nothing the others lack: FL saw
        no content (it saved to get someone's version in), or the save differs
        from its base only in values live sync has shared already (`check`:
        FL wasn't sure, it had unsaved changes it couldn't tell apart)."""
        if not auto["content"] and not auto["check"]:
            return True
        base = self._cached(self.base)
        if base is None:
            return False
        canon = {"base": dict(self.known.get(self.base, {}).get("tokens") or {}), "mine": mine["tokens"]}
        try:
            changed = await asyncio.to_thread(flp_merge.content_changes, base, mine["data"], canon)
        except flp_merge.MergeRefused:
            return False
        return not changed

    async def _settle(self, mine, newest):
        """A save FL made by itself with nothing new (docs/phase5.md): it isn't
        shared. If someone shared a version FL hasn't got, the save is merged
        with it and FL opens the result (`pulled`): nothing of FL's is lost."""
        if newest is None or newest["flp"] == self.base:
            self._tell_saved("unchanged")
            return True
        if self.base is None or (newest.get("switch") and newest["flp"] not in self.shared):
            return await self._not_ours(mine, newest, quiet=True)
        return await self._merge_and_share(mine, newest, quiet=True)

    async def _merge_and_share(self, mine, newest, quiet=False):
        """Someone shared `newest` since FL's project left the base: merge FL's
        save with it (flp_merge.py). Then share the result and open it in FL;
        or share the save as it is if it has everything; or just open the
        newest if the save has nothing new; or, when they can't be merged,
        share the save anyway (newest wins) and say so. `quiet`: the save has
        nothing new (_settle), so nothing is shared: FL opens the merge, or
        the newest as it is if they can't be merged."""
        what = "version %d av '%s'" % (newest["v"], newest["name"])
        try:
            theirs = await self._version_bytes(newest)
        except (OSError, TransferError, asyncio.TimeoutError) as e:
            self.log("Kunde inte hämta %s för att slå ihop: %s" % (what, str(e) or e.__class__.__name__))
            self._drop_files()
            return False
        folder = inside(self.projects_dir, safe_name(newest["name"], "FLSync-projekt"))
        mapping = await self._place_samples(newest, folder)
        base = self._cached(self.base)
        result = refused = None
        if base is None:
            refused = flp_merge.MergeRefused("base", "versionen ditt projekt utgår från finns inte kvar här")
        else:
            canon = {"base": dict(self.known.get(self.base, {}).get("tokens") or {}), "mine": mine["tokens"],
                     "theirs": {s["path"]: s["h"] for s in newest["samples"] if not s.get("plugin")}}
            try:
                result = await asyncio.to_thread(flp_merge.merge, base, mine["data"], theirs, canon, mapping)
            except flp_merge.MergeRefused as e:
                refused = e
        if refused is not None and quiet:
            # Nothing of FL's is lost by opening theirs as it is: its save had nothing new.
            return await self._pull(newest, folder, flp.rewrite_paths(theirs, mapping, data_path=""), mine)
        if refused is not None:
            reason = getattr(refused, "text", None) or str(refused)
            copy = self._keep_copy(folder, newest, flp.rewrite_paths(theirs, mapping, data_path=""))
            self.log("Din version kunde inte slås ihop med %s (%s), så den ersätter den.%s"
                     % (what, reason, " Den finns kvar som %s" % copy if copy else ""))
            return await self._share_mine(mine, newest, "replaced", why=reason, others=[newest["by"]])
        if not result.theirs_new and quiet:
            self._tell_saved("unchanged")  # FL has all of theirs already
            return True
        if not result.theirs_new:
            return await self._share_mine(mine, newest, "shared")  # it has all of theirs already
        if not result.mine_new or quiet:
            return await self._pull(newest, folder, result.data, mine)
        return await self._share_merged(mine, newest, folder, result)

    async def _share_merged(self, mine, newest, folder, result):
        data = result.data
        sha = hashlib.sha256(data).hexdigest()
        project = await asyncio.to_thread(flp.parse, data)
        files = await self._files()
        samples = await self._local_samples(project, files.max_blob)
        # Whose structure it has decides its fingerprint: FL's own when only this
        # save changed channels or plugins, else the session's (the newest's).
        fp = self.fl_fp if result.structure == "mine" else newest["fp"]
        name = newest["name"]
        answer = await self._put(data, sha, name, project, samples, newest["v"], fp, merged=[newest["by"]])
        if answer[0] == "outdated":
            return "outdated"
        if answer[0] != "ok":
            self.log("Kunde inte dela den sammanslagna versionen: %s" % answer[1])
            self._tell_saved("failed", why=str(answer[1]))
            return False
        tokens = {s["path"]: s["h"] for s in samples if not s.get("plugin")}
        self._remember(sha, answer[1], name, tokens, data)
        # FL still has the save open: that is its base until it opens the merged version.
        self._remember(mine["sha"], None, mine["name"], mine["tokens"], mine["data"])
        self._set_base(mine["sha"], mine["path"])
        self.unrelated = False
        target = await self._write_version(folder, name, data, replaceable={mine["sha"]})
        self.aliases[sha] = sha
        if target:
            self.received[sha] = target  # for the launcher's "Öppna senaste versionen"
        self._save_state()
        self.log("Din version är sammanslagen med %s (%s): från %s %s%s" % (
            "version %d av '%s'" % (newest["v"], newest["name"]), "version %s" % answer[1], newest["by"],
            ", ".join(result.taken[:6]) + (" ..." if len(result.taken) > 6 else ""),
            "; ändrat av er båda, din ändring behölls: %s" % ", ".join(result.conflicts[:6]) if result.conflicts else ""))
        # FL saved again meanwhile (target None): that save is merged with this version next.
        how = self._launch(target, self.newest, project.title, "merged") if target else "no"
        self._tell_saved("merged", v=answer[1], others=[newest["by"]], open=how, conflicts=len(result.conflicts),
                         taken=len(result.taken))
        return True

    async def _pull(self, newest, folder, data, mine):
        """The save has nothing the newest version lacks: nothing to share, but
        FL should have the newest. `data` is it as the merge made it: theirs,
        with this FL's version stamp and this computer's paths (so an FL older
        than the sharer's opens it without a warning). It is written and opened."""
        target = await self._write_version(folder, newest["name"], data, replaceable={mine["sha"]})
        self._know_version(newest, hashlib.sha256(data).hexdigest(), target)
        self.log("Din version har inget nytt jämfört med version %d av '%s' från %s: den öppnas i stället"
                 % (newest["v"], newest["name"], newest["by"]))
        how = self._launch(target, newest, flp.parse(data).title, "pulled") if target else "no"
        self._tell_saved("pulled", v=newest["v"], others=[newest["by"]], open=how)
        return True

    async def _put(self, data, sha, name, project, samples, parent, fp, merged=(), switch=False):
        """Upload a project and its samples, announce it with snap_put and wait
        for the relay's answer: ("ok", version), ("outdated", newest version:
        someone shared since `parent`) or ("error", text)."""
        files = await self._files()
        hashes = sorted({s["h"] for s in samples} - {sha})
        missing = set(await files.missing([sha] + hashes))
        uploaded = 0
        if sha in missing:
            await files.upload_bytes(sha, data)
            uploaded += 1
        for sample in samples:
            if sample["h"] in missing:
                missing.discard(sample["h"])
                await files.upload_file(sample["h"], sample["local"], sample["size"])
                uploaded += 1
        if not self.in_session() or fp is None:
            return "error", "FL är inte med i sessionen"
        plugins = []
        for plugin in project.plugins:
            if isinstance(plugin, str) and 0 < len(plugin) <= MAX_NAME and plugin not in plugins:
                plugins.append(plugin)
        entries = []
        for s in samples:
            entries.append({"h": s["h"], "size": s["size"], "path": s["path"]})
            if s.get("plugin"):
                entries[-1]["plugin"] = True
        msg = {"t": "snap_put", "name": name[:MAX_NAME], "fp": fp, "flp": sha, "size": len(data),
               "plugins": plugins[:MAX_LIST], "samples": entries, "parent": parent}
        if merged:
            msg["merged"] = [n[:40] for n in merged][:10]
        if switch:
            msg["switch"] = True
        version = fl_version(project.version) or self.fl_version
        if version:
            msg["flv"] = version  # the FL that saved it, so everyone can see it
        line = encode(msg)
        if len(line) > MAX_SNAP_PUT:
            self.unshareable = sha
            return "error", "för många samples (%d)" % len(samples)
        future = asyncio.get_running_loop().create_future()
        self.share_answer = (sha, future)
        try:
            self.writer.write(line)
            answer = await asyncio.wait_for(asyncio.shield(future), SNAP_TIMEOUT)
        except asyncio.TimeoutError:
            answer = ("error", "relayn svarade inte")
        finally:
            self.share_answer = None
        if answer[0] != "ok":
            return answer
        self.last_sha = sha
        self.shared.add(sha)
        size = len(data) + sum(s["size"] for s in samples)
        in_plugins = sum(1 for s in samples if s.get("plugin"))
        self.log("Delar '%s' med sessionen: %s%s, %s, %d %s laddades upp" % (
            name, sample_count(len(samples)), " (%d i plugins)" % in_plugins if in_plugins else "", megabytes(size),
            uploaded, "fil" if uploaded == 1 else "filer"))
        return answer

    def _tell_saved(self, outcome, v=None, others=(), open=None, why=None, conflicts=0, taken=0, confirm=None):
        """Tell FL how its save went (docs/phase4.md, Messages). `confirm`: the
        seconds within which saving again shares a project that isn't the session's."""
        msg = {"t": "saved", "outcome": outcome}
        if confirm:
            msg["confirm"] = confirm
        if v:
            msg["v"] = v
        if others:
            msg["with"] = [str(n)[:20] for n in others][:3]
        if open:
            msg["open"] = open
        if why:
            msg["why"] = str(why)[:80]
        if conflicts:
            msg["conflicts"] = conflicts
        if taken:
            msg["taken"] = taken
        if outcome != "unchanged":
            self.last_saved = dict(msg, time=time.time())
        self.to_fl(msg)

    # --- opening a version in FL -----------------------------------------------------

    def _launch(self, path, snapshot, title, kind):
        """Open `path`, written for `snapshot`, in FL: now if FL has nothing
        unsaved and doesn't play, when playback stops, or not at all (FL has
        unsaved changes: the next save merges again). "now", "later" or "no"."""
        if snapshot is None:
            return "no"
        self._expect_open(snapshot, path, title, self.session_fp, time.monotonic())
        if not self.open_merged:
            return "no"
        self.to_launch = {"path": path, "v": snapshot["v"], "flp": snapshot["flp"], "kind": kind,
                          "by": snapshot.get("by")}
        return self._try_launch()

    def _try_launch(self):
        want = self.to_launch
        if want is None:
            return None
        newest = self.newest
        if newest is None or newest["flp"] != want["flp"]:
            self.to_launch = None  # a newer version came meanwhile: the next save brings that one
            return "no"
        if self.fl_dirty:
            self.to_launch = None  # FL has unsaved changes again: opening would ask about them
            return "no"
        if self.fl_playing:
            return "later"
        self.to_launch = None
        try:
            self.opener(want["path"])
        except (OSError, AttributeError) as e:
            self.log("Kunde inte öppna %s: %s" % (want["path"], e))
            return "no"
        self.launched = (want["path"], time.monotonic())
        self.log("Öppnar %s i FL" % want["path"])
        return "now"

    # --- content without Ctrl+S (docs/phase5.md) ----------------------------------------

    def _may_save(self, pull=False):
        """Whether FL may save by itself: it is in a session with other FLs,
        and its project is the session's (or the session switched to another
        one, which a save brings in: `pull`) and a file of its own on disk that
        FL's title names. Never a new, unsaved project: that would ask where."""
        if not self.auto_sync or not self.in_session() or not self._other_fls():
            return False
        if self.autosave_failures >= AUTOSAVE_TRIES or time.monotonic() < self.autosave_retry:
            return False
        if self.base is None and not (pull and self.newest and self.newest.get("switch")):
            return False
        path = self.watched
        if not path or not os.path.isfile(path) or not os.access(path, os.W_OK):
            return False
        factory = self.fl_dirs.get("factory_dir")
        if factory and inside_folder(path, factory):
            return False  # one of FL's own (a template): Ctrl+S would ask where to save it
        try:
            title = self.fl_title()
        except Exception:
            title = None
        return title_shows(title, os.path.splitext(os.path.basename(path))[0])

    def _thin_backups(self, path, asked_at):
        """FL copies a project into its Backup folder each time it saves it, and
        keeps only its newest copies of all projects together (File settings,
        Backup: Maximum, 100 by default): FL saving by itself every few seconds
        would soon push out the copies of every other project. Of the copies
        made for the save FL was asked for at `asked_at` (and before it), one
        per project per BACKUP_EVERY stays; the session keeps the versions
        anyway. A copy made for a save of the user's own is never touched."""
        user = self.fl_dirs.get("user_dir")
        if not user:
            return
        folder = os.path.join(user, "FL Studio", "Projects", "Backup")
        stem = file_stem(path)
        key = stem.casefold()
        since = time.time() - (time.monotonic() - asked_at) - BACKUP_SLACK
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return  # no such folder: FL keeps its backups elsewhere (the project's folder), or none
        copies = []
        for entry in entries:
            name = entry.name
            if name[:len(stem)].casefold() != key or not BACKUP_COPY.fullmatch(name[len(stem):]):
                continue
            try:
                if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime >= since:
                    copies.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
        removed = 0
        for mtime, copy in sorted(copies):
            kept = self.backups_kept.get(key)
            if kept is not None and same_file(copy, kept[1]):
                continue  # the one that stays (an earlier save's, just before this one)
            if kept is None or mtime - kept[0] >= BACKUP_EVERY:
                self.backups_kept[key] = (mtime, copy)
                continue
            try:
                with open(copy, "rb") as f:
                    if f.read(4) != b"FLhd":
                        continue  # not an FL project after all
                os.remove(copy)
                removed += 1
            except OSError:
                continue
        if removed and not self.backups_told:
            self.backups_told = True
            self.log("FL lägger en kopia i sin Backup-mapp varje gång det sparar, och behåller bara de senaste "
                     "för alla projekt. FLSync tar bort kopiorna av sina egna sparningar utom en var tionde "
                     "minut, så att dina andra backups finns kvar")

    def set_auto_sync(self, on):
        """FL saving by itself and getting others' versions in: on or off (the
        launcher's window). FL hears it at once."""
        self.auto_sync = on
        if not on:
            if self.pull_sent is not None:
                self.to_fl({"t": "pull", "cancel": True})
            self.pull_sent = None
        self._offer_autosave(time.monotonic())

    def _offer_autosave(self, now):
        """Tell FL whether it may save by itself, when that changes; and notice
        an FL that said it would save but didn't (a dialog came instead)."""
        asked = self.autosave
        if asked is not None and not asked["seen"] and now - asked["at"] > AUTOSAVE_CHECK:
            self.autosave = None
            self.autosave_failures += 1
            self.autosave_retry = now + AUTOSAVE_RETRY
            again = self.autosave_failures < AUTOSAVE_TRIES
            if again:
                self.log("FL sparade inte när det skulle spara av sig självt (en dialog var kanske öppen), "
                         "FLSync försöker igen om en minut")
            else:
                self.log("FL sparade inte när det skulle spara av sig självt, %d gånger i rad, så FLSync ber inte "
                         "om det mer. Det som inte synkas live delas när du sparar (Ctrl+S)" % AUTOSAVE_TRIES)
            self.to_fl({"t": "autosave", "ok": False, "broken": True, "retry": again})
            self.autosave_sent = False
            return
        ok = self._may_save()
        if ok != bool(self.autosave_sent):  # (FL's script starts with no)
            self.autosave_sent = ok
            self.to_fl({"t": "autosave", "ok": ok})

    def _check_pull(self):
        """Someone shared a version FL hasn't got, and FL can't just open it:
        it has unsaved changes (others' values written make them too). FL is
        asked to save once it is stopped and idle (`pull`); that save is merged
        with the version and FL opens the result (_settle)."""
        newest = self.newest
        if newest is None or self.sharing or newest["flp"] in (self.base, self.pull_sent):
            return
        if self.to_launch is not None and not self.fl_dirty:
            return  # FL opens it by itself once playback stops
        if newest["flp"] not in self.downloaded and newest["flp"] not in self.shared:
            return  # not here yet
        if not self._may_save(pull=True) or newer(newest.get("flv"), self.fl_version):
            return  # (a newer FL's project comes in through Ctrl+S, merged in this FL's version)
        if self._missing_plugins(newest["plugins"]):
            return  # FL would stop at a dialog about them: that one the user opens
        self.pull_sent = newest["flp"]
        self.to_fl({"t": "pull", "by": newest["by"], "v": newest["v"]})

    def _on_status(self, msg):
        """FL's `st`: whether it has unsaved changes and plays."""
        dirty, playing = msg.get("dirty"), msg.get("playing")
        if is_int(dirty) and dirty == 0 and self.fl_dirty:
            self.fl_saved_at = time.monotonic()  # FL saved (or loaded): a .flp it wrote now is its project
        self.fl_dirty = dirty if is_int(dirty) else None
        self.fl_playing = playing if isinstance(playing, bool) else None
        want = self.to_launch
        if want is not None and self._try_launch() == "no" and want["kind"] in ("merged", "pulled"):
            self._tell_saved(want["kind"], v=want["v"], others=[want["by"]] if want["by"] else (), open="no")

    def _on_opened(self, msg):
        """FL loaded a project (its script says so after every load): which
        version is it? And it may be the opening _check_loaded waits for."""
        now = time.monotonic()
        if self.launched is not None and (self.fl_loaded_at or 0) >= self.launched[1]:
            self.launched = None  # FL opened that file before: this load is of another project
        self.fl_loaded_at = now
        self.pull_sent = None  # whatever FL has now: what it lacks is asked for again
        self._reported("opened", fingerprint(msg.get("fp")))
        self._identify_soon()

    # --- which version FL has open: the base ---------------------------------------------

    def _identify_soon(self):
        if self.identifying is not None and not self.identifying.done():
            self.identifying.cancel()
        self.identifying = self._spawn(self._identify(time.monotonic()))

    async def _identify(self, since):
        """Find out which version FL's open project is: the content of the file
        FL shows (recent files, title) as a version known here, or a project
        file whose base is known. A file FL doesn't show within IDENTIFY_TIME
        (a new, unsaved project) has none."""
        deadline = since + IDENTIFY_TIME
        while True:
            path = self._watched_path()
            if path and os.path.isfile(path) and self._fl_shows(path):
                try:
                    digest = (await self._hash(path))[0]
                except OSError:
                    digest = None
                version = self.aliases.get(digest) or (digest if digest in self.known else None)
                if version is None:
                    version = self.lineage.get(file_key(path))
                self._set_base(version, path)
                return
            if time.monotonic() >= deadline:
                self._set_base(None, None)
                return
            await asyncio.sleep(IDENTIFY_INTERVAL)

    def _set_base(self, version, path):
        changed = (version, path) != (self.base, self.base_project)
        self.base, self.base_project = version, path
        if version is not None:
            self.unrelated = False
            if path:
                self.lineage[file_key(path)] = version
                while len(self.lineage) > KNOWN_KEPT:
                    self.lineage.pop(next(iter(self.lineage)))
        if changed:
            self._save_state()

    def _remember(self, sha, v, name, tokens, data=None):
        """A version FL may have open (shared or written here), with its samples'
        tokens; its bytes go into the cache, so it can be a merge's base later."""
        entry = {"v": v, "name": name, "tokens": dict(tokens)}
        self.known.pop(sha, None)
        self.known[sha] = entry
        while len(self.known) > KNOWN_KEPT:
            self.known.popitem(last=False)
        if data is not None:
            target = os.path.join(self.cache_dir, sha)
            if file_size(target) != len(data):
                try:
                    os.makedirs(self.cache_dir, exist_ok=True)
                    temp = temp_name(target)
                    try:
                        with open(temp, "wb") as f:
                            f.write(data)
                        os.replace(temp, target)
                    finally:
                        discard(temp)
                except OSError as e:
                    self.log("Kunde inte spara en kopia i %s: %s" % (self.cache_dir, e))
        self._save_state()

    def _know_version(self, snapshot, written_sha, path):
        """Someone's version, written here as `path` (with sha256 `written_sha`)."""
        tokens = {s["path"]: s["h"] for s in snapshot["samples"] if not s.get("plugin")}
        self._remember(snapshot["flp"], snapshot["v"], snapshot["name"], tokens)
        self.aliases[written_sha] = snapshot["flp"]
        while len(self.aliases) > 2 * KNOWN_KEPT:
            self.aliases.pop(next(iter(self.aliases)))
        if path:
            self.received[snapshot["flp"]] = path
        self._save_state()

    def _cached(self, sha):
        if not sha:
            return None
        try:
            with open(os.path.join(self.cache_dir, sha), "rb") as f:
                data = f.read()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == sha else None

    async def _version_bytes(self, snapshot):
        """A version's .flp, and its samples in the cache (for placing them)."""
        await self._fetch(snapshot["flp"], snapshot["size"])
        for sample in snapshot["samples"]:
            await self._fetch(sample["h"], sample["size"])
        data = self._cached(snapshot["flp"])
        if data is None:
            raise TransferError("versionen gick inte att läsa i cachen")
        return data

    def _other_fls(self):
        return any(p.get("app") == "fl" for p in self.peers.values())

    # --- bridge-state.json ----------------------------------------------------------------

    def _load_state(self):
        self.lineage = collections.OrderedDict()  # file_key(project file) -> the version it comes from
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log("%s gick inte att läsa (%s), börjar om" % (self.state_path, e))
            return
        if not isinstance(state, dict) or state.get("format") != 1:
            return
        for sha, entry in state.get("known") or []:
            if valid_hash(sha) and isinstance(entry, dict) and isinstance(entry.get("tokens"), dict):
                self.known[sha] = {"v": entry.get("v") if is_int(entry.get("v"), 1) else None,
                                   "name": str(entry.get("name") or ""),
                                   "tokens": {str(k): v for k, v in entry["tokens"].items() if valid_hash(v)}}
        for alias, sha in (state.get("aliases") or {}).items():
            if valid_hash(alias) and valid_hash(sha):
                self.aliases[alias] = sha
        for key, sha in state.get("lineage") or []:
            if isinstance(key, str) and valid_hash(sha):
                self.lineage[key] = sha
        base, project = state.get("base"), state.get("base_project")
        if valid_hash(base) and isinstance(project, str):
            self.base, self.base_project = base, project

    def _save_state(self):
        state = {"format": 1, "base": self.base, "base_project": self.base_project,
                 "known": [[sha, entry] for sha, entry in self.known.items()],
                 "aliases": self.aliases, "lineage": [[key, sha] for key, sha in self.lineage.items()]}
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            write_json(self.state_path, state)
        except OSError as e:
            self.log("Kunde inte spara %s: %s" % (self.state_path, e))

    async def _local_samples(self, project, limit):
        """The samples of a project that are sent: not FL's own, and on this
        computer. Those that plugins keep in their state come last, with
        `plugin`: a receiver puts them next to the .flp."""
        found, absent, too_big = [], [[], []], []
        for refs, plugin in ((project.samples, False), (project.plugin_samples, True)):
            seen = set()
            for ref in refs:
                text = ref.path
                if ref.factory or not isinstance(text, str) or not text or text in seen or len(text) > MAX_TEXT:
                    continue
                if plugin and plugin_sample_name(text) is None:
                    continue
                seen.add(text)
                try:
                    local = flp.resolve(text, **self.fl_dirs)
                except (OSError, ValueError):
                    local = None
                if not local or not os.path.isfile(local):
                    absent[plugin].append(text)
                    continue
                try:
                    digest, size = await self._hash(local)
                except OSError:
                    absent[plugin].append(text)
                    continue
                if size > limit:
                    too_big.append(text)
                    continue
                found.append({"h": digest, "size": size, "path": text, "local": local})
                if plugin:
                    found[-1]["plugin"] = True
        for missing, where in zip(absent, ("", " som plugins använder")):
            if missing:
                self.log("%s%s finns inte på den här datorn och skickas inte: %s"
                         % (sample_count(len(missing)), where,
                            ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")))
        if too_big:
            self.log("%s är större än relayns gräns på %s och skickas inte: %s"
                     % (sample_count(len(too_big)), megabytes(limit), ", ".join(too_big[:5])))
        return found

    # --- snapshots: receiving --------------------------------------------------------

    def _how(self):
        """How FL gets a new version: Ctrl+S merges it in when FL's project is
        one we know the base of, else it has to open it."""
        return "save" if self.base is not None else "open"

    def _on_welcome(self, msg):
        self.generation += 1
        self.my_id = msg.get("id") if isinstance(msg.get("id"), str) else None
        self.peers = {p["id"]: p for p in msg.get("peers") or [] if isinstance(p, dict) and isinstance(p.get("id"), str)}
        before, self.session_fp = self.session_fp, fingerprint(msg.get("fp"))
        self.newest = None
        self._set_notes(msg.pop("notes", None))  # for context.json; far too big for FL
        snapshot = clean_snapshot(msg.get("snapshot"))
        msg["snapshot"] = short(snapshot, self._how()) if snapshot else None
        if snapshot:
            self.newest = snapshot
            self._consider(snapshot, before)
        else:
            self.last_sha = None  # a session without a version (a restarted relay): the next save shares

    def _on_snapshot(self, msg):
        snapshot = clean_snapshot(msg)
        if snapshot is None:
            self.log("relayn skickade en ogiltig version, den hoppas över")
            return None
        before = self.session_fp
        if snapshot["fp"] != self.session_fp:
            self._set_notes([])  # the relay drops the old structure's notes
        self.session_fp = snapshot["fp"]
        if self.newest is None or snapshot["v"] >= self.newest["v"]:
            self.newest = snapshot
        if snapshot.get("switch") and snapshot["from"] != self.my_id and self.base not in (None, snapshot["flp"]):
            # Someone made another project the session's: FL's is the old one now,
            # and a save of it mustn't be merged with the new (_not_ours opens that).
            self.log("%s bytte sessionens projekt till '%s'" % (snapshot["by"], snapshot["name"]))
            self._set_base(None, self.base_project)
        if snapshot["from"] == self.my_id:
            self.log("Version %d av '%s' är delad" % (snapshot["v"], snapshot["name"]))
            return dict({"t": "snapshot"}, **short(snapshot))
        merged = " (sammanslagen med %s)" % names_text(snapshot["merged"]) if snapshot.get("merged") else ""
        self.log("%s delade version %d av '%s'%s" % (snapshot["by"], snapshot["v"], snapshot["name"], merged))
        self._consider(snapshot, before)
        # FL with nothing unsaved gets it opened (_receive): no "press Ctrl+S" for it.
        clean = self.open_merged and not self.auto_open and self.fl_dirty == 0 and not self.fl_playing
        return dict({"t": "snapshot"}, **short(snapshot, None if clean else self._how()))

    def _consider(self, snapshot, before=None):
        """Fetch someone else's version, unless we have it already. `before` is
        the session's structure before it."""
        if snapshot["flp"] in self.shared:
            return  # our own (shared before we reconnected)
        written = self.received.get(snapshot["flp"])
        if written and os.path.isfile(written):
            return
        self._spawn(self._receive(snapshot, self.generation, before))

    async def _receive(self, snapshot, generation, before=None):
        what = "version %d av '%s'" % (snapshot["v"], snapshot["name"])
        async with self.lock:
            if generation != self.generation or (self.newest and self.newest["v"] > snapshot["v"]):
                return  # a newer version came meanwhile, and that one is fetched instead
            if self.base in (None, snapshot["flp"]) and await self._is_watched(snapshot["flp"]):
                # (A file that holds it while FL's project comes from another
                # version got it from elsewhere, see _share_now: FL hears of it.)
                self.log("%s är samma som projektet du har öppet" % capital(what))
                return
            blobs = [(snapshot["flp"], snapshot["size"])] + [(s["h"], s["size"]) for s in snapshot["samples"]]
            to_fetch = sum(size for h, size in set(blobs) if file_size(os.path.join(self.cache_dir, h)) != size)
            if to_fetch:
                self.log("Hämtar %s (%s) ..." % (what, megabytes(to_fetch)))
            try:
                for h, size in blobs:
                    await self._fetch(h, size)
                self.downloaded.add(snapshot["flp"])
                path, project = await self._write_project(snapshot)
            except (OSError, TransferError, asyncio.TimeoutError, flp.FlpError) as e:
                self.log("Kunde inte hämta %s: %s" % (what, str(e) or e.__class__.__name__))
                self._drop_files()
                return
            written_at = time.monotonic()
        if path:
            self.log("%s finns i %s" % (capital(what), path))
        else:
            self.log("%s är hämtad. Projektet i FL har sparade ändringar som inte är delade än, så den skrivs "
                     "inte över dem: de slås ihop med den när de delas" % capital(what))
        missing = self._missing_where(self._missing_plugins(snapshot["plugins"]), project)
        if missing:
            self.log("Saknas här: %s" % ", ".join(missing_text(m) for m in missing))
        too_new = newer(project.version, self.fl_version)
        if too_new:
            # FL warns about a project from a newer FL (and asks): it isn't opened
            # by itself. Ctrl+S merges it in, stamped with this FL's version.
            self.log("%s är sparad i FL %s och du har %s. Tryck Ctrl+S i FL, så slås den ihop med ditt projekt "
                     "i din FL-version (öppnar du den direkt varnar FL)" % (capital(what), project.version,
                                                                           self.fl_version))
        current = self.newest is not None and self.newest["v"] == snapshot["v"] and generation == self.generation
        if not current or snapshot["fp"] != self.session_fp:
            return  # outdated already: FL hears of the newer one instead
        # FL has nothing unsaved (it said so: `st`) and doesn't play: opening the
        # new version loses nothing, and a newcomer gets the session's project at
        # once. Then FL needn't hear how to get it.
        opening = (bool(path) and not too_new and not self.auto_open and self.open_merged and self.fl_dirty == 0
                   and not self.fl_playing)
        notice = dict({"t": "snapshot"}, **short(snapshot, None if opening else self._how()))
        if path:
            self._expect_open(snapshot, path, project.title, before, written_at)
            notice["path"] = path
        if missing:
            notice["missing"] = []
            for entry in missing[:MAX_MISSING]:
                entry = dict(entry, plugin=entry["plugin"][:40])
                if "channels" in entry:
                    entry["channels"] = entry["channels"][:MAX_CHANNELS]
                notice["missing"].append(entry)
        if too_new:
            notice["newer"] = project.version
        self.to_fl(notice)
        if too_new or not path:
            return
        if not self.auto_open and await self._add_live(snapshot, before, missing):
            return  # FL has what the version brought, without loading anything (better than opening it)
        if self.auto_open:
            try:
                self.opener(path)
                self.log("Öppnar %s i FL" % path)
            except (OSError, AttributeError) as e:
                self.log("Kunde inte öppna %s: %s" % (path, e))
        elif opening:
            self._launch(path, snapshot, project.title, "received")
        elif snapshot.get("switch") and self.base is None:
            self._launch(path, snapshot, project.title, "switched")  # when playback stops, if nothing is unsaved

    async def _add_live(self, snapshot, before, missing):
        """Channels someone added after all the others (a new instrument, a VST
        with its sound) come into FL at once, as plugin presets FL opens: it
        adds each one at the end of its channel rack, also while it plays,
        without loading the project again (docs/phase5.md). Only when that is
        all the version brought and FL's channels are the session's from
        before it: then FL has the version, and its base moves on. Anything
        else comes in by loading the version (_check_pull)."""
        if not self.auto_sync or self.base is None or missing or not self.fl_fp or not before:
            return False
        if parts_differ(self.fl_fp, before, "c"):
            return False  # FL's channels aren't the ones the version added to
        base, theirs = self._cached(self.base), self._cached(snapshot["flp"])
        if base is None or theirs is None:
            return False
        canon = {"base": dict(self.known.get(self.base, {}).get("tokens") or {}),
                 "mine": {s["path"]: s["h"] for s in snapshot["samples"] if not s.get("plugin")}}
        try:
            added = await asyncio.to_thread(flp_merge.appended_channels, base, theirs)
            changed = await asyncio.to_thread(flp_merge.content_changes, base, theirs, canon)
            presets = [flp_merge.channel_preset(theirs, n) for n in added]
        except flp_merge.MergeRefused:
            return False
        rest = [key for key in changed if not (key[0] == "channel" and key[1] in added)]
        if not added or rest or None in presets:
            return False  # (a sampler's sample or anything else comes with the version)
        try:
            folder = os.path.join(inside(self.projects_dir, safe_name(snapshot["name"], "FLSync-projekt")), LIVE_FOLDER)
        except TransferError:
            return False
        names = []
        self.to_fl({"t": "adding", "fp": snapshot["fp"]})  # FL's next structure is theirs: nothing of its own
        for n, (data, name) in zip(added, presets):
            path = os.path.join(folder, safe_name(name, "Kanal %d" % (n + 1)) + ".fst")  # FL names it after the file
            try:
                os.makedirs(folder, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
                self.opener(path)
            except (OSError, AttributeError, TransferError) as e:
                self.log("Kunde inte lägga in kanal %d live: %s" % (n + 1, e))
                return bool(names)
            names.append(name or "kanal %d" % (n + 1))
            await asyncio.sleep(LIVE_ADD_GAP)
        # FL says its new structure (`fp`) once it has them: then it has the version.
        deadline = time.monotonic() + LIVE_CONFIRM
        while parts_differ(self.fl_fp, snapshot["fp"], "c") and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if parts_differ(self.fl_fp, snapshot["fp"], "c"):
            self.log("FL lade inte till %s som väntat, så versionen öppnas i stället när det går" % names_text(names))
            return False
        self.log("%s lade till %s: finns nu i FL, med samma inställningar" % (snapshot["by"], names_text(names)))
        self.to_fl({"t": "added", "by": snapshot["by"][:40], "what": [n[:40] for n in names[:5]]})
        self._set_base(snapshot["flp"], self.base_project)
        return True

    async def version_file(self, snapshot):
        """The file FL opens for `snapshot` (the launcher's "Öppna senaste
        versionen"): written now if it isn't there yet, also over a save of
        FL's that isn't shared (the user asked for the version; that save
        goes to Versioner first). None if it isn't downloaded yet."""
        written = self.received.get(snapshot["flp"])
        if written and os.path.isfile(written):
            try:
                if self.aliases.get((await self._hash(written))[0]) == snapshot["flp"] or snapshot["flp"] in self.shared:
                    return written
            except OSError:
                pass
        async with self.lock:
            try:
                if snapshot["flp"] in self.shared:
                    # Ours (a merged version): its paths are this computer's already.
                    data = self._cached(snapshot["flp"])
                    if data is None:
                        return None
                    folder = inside(self.projects_dir, safe_name(snapshot["name"], "FLSync-projekt"))
                    path = await self._write_version(folder, snapshot["name"], data, force=True)
                    project = flp.parse(data)
                else:
                    if any(file_size(os.path.join(self.cache_dir, h)) != size for h, size in
                           [(snapshot["flp"], snapshot["size"])] + [(s["h"], s["size"]) for s in snapshot["samples"]]):
                        return None
                    path, project = await self._write_project(snapshot, force=True)
            except (OSError, TransferError, flp.FlpError) as e:
                self.log("Kunde inte skriva version %d: %s" % (snapshot["v"], e))
                return None
        if path:
            self._expect_open(snapshot, path, project.title, self.session_fp, time.monotonic())
        return path

    def _is_version(self, sha):
        """Is this .flp content a version of the session (as shared, or as written here)?"""
        return sha in self.known or sha in self.aliases or (self.newest is not None and self.newest["flp"] == sha)

    async def _is_watched(self, h):
        """Is the project FL has open exactly this content?"""
        if not self.watched:
            return False
        try:
            return (await self._hash(self.watched))[0] == h
        except OSError:
            return False

    async def _fetch(self, h, size):
        target = os.path.join(self.cache_dir, h)
        if file_size(target) == size:
            return target
        os.makedirs(self.cache_dir, exist_ok=True)
        files = await self._files()
        await files.fetch(h, size, target)
        return target

    async def _write_project(self, snapshot, force=False):
        """<projects>/<name>/<name>.flp with the samples in Samples/ and the
        project's sample paths pointing there. The samples that plugins keep
        in their state go next to the .flp under their own names instead: those
        paths can't be rewritten, and FL looks for a missing file by its name
        in the project's folder. Returns the .flp's path (None if FL saved
        something there that isn't shared yet, see _write_version) and what
        the version holds."""
        folder = inside(self.projects_dir, safe_name(snapshot["name"], "FLSync-projekt"))
        with open(os.path.join(self.cache_dir, snapshot["flp"]), "rb") as f:
            data = f.read()
        project = await asyncio.to_thread(flp.parse, data)  # someone else's file: it mustn't hold up MIDI
        mapping = await self._place_samples(snapshot, folder)
        # The sharer's project data folder isn't on this computer, and FL would
        # stop at a dialog about it: the copy has none.
        data = flp.rewrite_paths(data, mapping, data_path="")
        target = await self._write_version(folder, snapshot["name"], data, force=force)
        self._know_version(snapshot, hashlib.sha256(data).hexdigest(), target)
        return target, project

    async def _place_samples(self, snapshot, folder):
        """Copy a version's samples from the cache into its project folder
        (_write_project); {path in the version: path here} of those in Samples/."""
        samples = inside(folder, "Samples")
        os.makedirs(samples, exist_ok=True)
        mapping, placed = {}, {}
        for sample in snapshot["samples"]:
            if sample.get("plugin"):
                await self._place_plugin_sample(folder, sample, placed)
            elif sample["path"] not in mapping:
                mapping[sample["path"]] = await self._place_sample(samples, sample)
        return mapping

    async def _write_version(self, folder, name, data, replaceable=(), force=False):
        """Write <folder>/<name>.flp, FL's working copy of the session's project.
        Only over a file that holds a version known here (or one of the hashes
        in `replaceable`, a save this bridge has read and merged): one FL saved
        that isn't shared yet is never written over, it gets shared (merged)
        first; with `force` (the user asked) it is. What the file held goes to
        its Versioner folder first (the VERSIONS_KEPT newest stay there).
        Returns the path, or None if the file wasn't written."""
        target = inside(folder, safe_name(name, "FLSync-projekt") + ".flp")
        sha = hashlib.sha256(data).hexdigest()
        stat = file_stat(target)
        if stat is not None:
            try:
                digest = (await self._hash(target))[0]
            except OSError:
                return None
            if digest == sha:
                return target  # it holds this already
            if not force and digest not in self.known and digest not in self.aliases and digest not in replaceable:
                return None
            self._back_up(folder, name, target, digest)
        # Our own write isn't the user saving: the watcher must not share it back.
        self.last_sha = sha
        try:
            await write_file(target, data, unchanged=stat)
        except Changed:
            return None  # FL saved it just now: that save is shared (merged) first
        self.wrote[file_key(target)] = file_stat(target)
        return target

    def _back_up(self, folder, name, target, digest):
        """Copy `target` (content `digest`) into the folder's Versioner folder,
        named after the version it held where that is known."""
        version = self.known.get(self.aliases.get(digest, digest)) or {}
        label = "v%d" % version["v"] if version.get("v") else time.strftime("%Y-%m-%d %H.%M.%S")
        try:
            versions = inside(folder, "Versioner")
            os.makedirs(versions, exist_ok=True)
            copy = inside(versions, safe_name("%s %s" % (name, label), "version") + ".flp")
            if not os.path.exists(copy):
                with open(target, "rb") as src:
                    data = src.read()
                with open(copy, "wb") as dst:
                    dst.write(data)
            kept = sorted((os.path.join(versions, f) for f in os.listdir(versions) if f.lower().endswith(".flp")),
                          key=os.path.getmtime, reverse=True)
            for old in kept[VERSIONS_KEPT:]:
                discard(old)
        except (OSError, TransferError) as e:
            self.log("Kunde inte spara en kopia av %s i Versioner: %s" % (target, e))

    def _keep_copy(self, folder, snapshot, data):
        """A version about to be replaced by one it couldn't be merged with:
        keep it (`data`, written for this computer) in the project folder's
        Versioner folder. Returns the copy's path, or None."""
        name = snapshot["name"]
        try:
            versions = inside(folder, "Versioner")
            os.makedirs(versions, exist_ok=True)
            copy = inside(versions, safe_name("%s v%d" % (name, snapshot["v"]), "version") + ".flp")
            with open(copy, "wb") as f:
                f.write(data)
        except (OSError, TransferError) as e:
            self.log("Kunde inte spara en kopia av version %d i Versioner: %s" % (snapshot["v"], e))
            return None
        return copy

    async def _place_sample(self, folder, sample):
        """Copy a sample from the cache into the project's Samples folder. A file
        of the same name with other content (another version's) is left alone."""
        base = sample_name(sample["path"])
        stem, ext = os.path.splitext(base)
        source = os.path.join(self.cache_dir, sample["h"])
        for n in range(1, 1000):
            target = inside(folder, base if n == 1 else "%s (%d)%s" % (stem, n, ext))
            try:
                if not os.path.exists(target):
                    await copy_file(source, target)
                    return target
                if file_size(target) == sample["size"] and (await self._hash(target))[0] == sample["h"]:
                    return target  # already there
            except OSError:
                continue
        raise TransferError("hittade inget ledigt namn för %s" % base)

    async def _place_plugin_sample(self, folder, sample, placed):
        """Copy a sample that a plugin uses from the cache to the project's
        folder, under the file's own name. It replaces an older version's file
        of that name; of two in one version (`placed`: name -> hash) the first
        stays. A file that can't be written there is only logged: FL then
        reports the sample missing, the rest of the version is fine."""
        name = plugin_sample_name(sample["path"])
        if name is None:
            return None
        if name.lower() in placed:
            if placed[name.lower()] != sample["h"]:
                self.log("Två samples i plugins heter %s, bara den första läggs i projektets mapp" % name)
            return None
        placed[name.lower()] = sample["h"]
        target = inside(folder, name)
        try:
            if file_size(target) == sample["size"] and (await self._hash(target))[0] == sample["h"]:
                return target  # already there
            await copy_file(os.path.join(self.cache_dir, sample["h"]), target)
        except OSError as e:
            self.log("Kunde inte lägga %s i projektets mapp: %s" % (name, e))
            return None
        return target

    @staticmethod
    def _missing_where(missing, project):
        """{plugin, channels} for each missing plugin, with the channels that use
        it (from 1, as FL shows them), and `mixer` if it is an effect in the
        mixer; just {plugin} where the project doesn't tell."""
        channels = {}
        for channel in project.channels:
            if channel.plugin:
                channels.setdefault(channel.plugin.lower(), []).append(channel.index + 1)
        effects = {effect.lower() for effect in project.effects}
        where = []
        for plugin in missing:
            entry = {"plugin": plugin}
            if plugin.lower() in channels:
                entry["channels"] = channels[plugin.lower()]
            if plugin.lower() in effects:
                entry["mixer"] = True
            where.append(entry)
        return where

    def _missing_plugins(self, plugins):
        """The plugins of someone's version that FL's plugin database here doesn't
        list, leaving out FL's own. Without a database nothing is reported: it
        can't be told."""
        if not plugins or not self.plugin_db or not os.path.isdir(self.plugin_db):
            return []
        try:
            installed = flp.installed_plugins(self.plugin_db)
            factory = self.fl_dirs.get("factory_dir")
            native = flp.native_plugins(factory) if factory else set()
        except (OSError, ValueError):
            return []
        if not installed:
            return []
        missing = []
        for plugin in plugins:
            name = plugin.lower()
            if (name not in installed and name not in native and name != "fruity wrapper"
                    and plugin not in missing):
                missing.append(plugin)
        return missing

    # --- loaded: FL opened a version written here --------------------------------------

    def _reported(self, kind, fp=None):
        """FL said its structure: in its hello, in `fp` (after a project load,
        or when its channels or plugins changed), or in `opened` (after every
        project load: its own reading, `fp`)."""
        self.reports += 1
        now = time.monotonic()
        self.report = (self.reports, now, fp if kind == "opened" else self.fl_fp, kind)
        if self.to_open is not None:
            self._check_loaded(now)

    def _expect_open(self, snapshot, path, title, before, written_at):
        """`path` is written for someone's newest version: FL may open it now.
        `title` is the project's own (Project info), `before` the session's
        structure before that version."""
        showing = self._fl_shows(path, title)
        self.to_open = {"v": snapshot["v"], "flp": snapshot["flp"], "fp": snapshot["fp"], "name": snapshot["name"],
                        "path": path, "title": title, "at": written_at, "showing": showing, "since": written_at,
                        "inherited": showing,  # FL has the file open already, with an older version in it
                        "follow": showing and self.fl_fp is not None and self.fl_fp == before != snapshot["fp"],
                        "state": "waiting", "local": None, "used": self.reports}

    def _check_loaded(self, now):
        """Tell FL `loaded {v, fp}` once it has opened the project written for the
        newest version, so that it counts its structure as that version's even
        where it reads differently here (a plugin missing on this computer).

        Whether FL shows that file is told by its recent files and its title
        (_fl_shows); what it loaded only by what its script says of its
        structure (hello, fp: `report`). So one `loaded` per opening, sent for
        the report that belongs to it:
          * FL didn't show the file when it was written, or has shown another
            project since: the report from LOAD_LAG before FL began showing it
            to LOAD_TIME after.
          * FL showed it already (the version was written over the one FL had
            open) and FL had the session's structure: FL's first `fp` since,
            from opening it again. (A change of channels before that passes for
            one; nothing here tells them apart.) Otherwise only once FL has had
            another project open and then this one again.
          * FL answers `loaded` with the version's fp. If another report comes
            first, FL didn't take it: that report gets another `loaded`. Later,
            the structure FL had when it took it means FL opened the file anew
            (or its script restarted): `loaded` again.
        Never for one that isn't the newest (nor for our own, except a merged one)."""
        want = self.to_open
        if want is None or not self.in_session():
            return
        newest = self.newest
        if newest is None or (newest["v"], newest["flp"]) != (want["v"], want["flp"]) or want["fp"] != self.session_fp:
            return
        if not self._fl_shows(want["path"], want["title"]):
            if want["showing"]:  # FL has another project now: showing this one again is another opening
                want.update(showing=False, inherited=False, follow=False, state="waiting", local=None)
            return
        if not want["showing"]:
            want.update(showing=True, since=now)
        report = self.report
        if report is None or report[0] <= want["used"] or report[1] < want["at"] or report[2] is None:
            return
        number, when, fp, kind = report
        want["used"] = number
        if want["state"] == "waiting":
            if want["inherited"]:
                # FL showed the file already: only a load since counts (`opened`
                # says so; an older script only by its structure changing).
                if not (kind == "opened" or (want["follow"] and kind == "fp")):
                    return
            elif not want["since"] - LOAD_LAG <= when <= want["since"] + LOAD_TIME:
                return
        elif want["state"] == "sent":
            if fp == want["fp"]:
                want["state"] = "confirmed"
                return
        elif fp != want["local"] or fp == want["fp"]:  # confirmed: only its structure from that opening again
            return
        want.update(state="confirmed" if fp == want["fp"] else "sent", local=fp)
        self.to_fl({"t": "loaded", "v": want["v"], "fp": want["fp"]})
        self.log("FL har öppnat version %d av '%s'%s" % (want["v"], want["name"], "" if fp == want["fp"] else
                 ": det som skiljer här (t.ex. plugins som saknas) pausar inte synken"))

    def _fl_shows(self, path, project_title=""):
        """Whether FL's current project is the file `path` (whose project title
        is `project_title`). A title naming a .flp (FL 2025: "Beat.flp - FL
        Studio 2025") decides with _watched_path, which finds that file.
        Otherwise as far as the newest file in FL's recent-files list and the
        title tell: once the title has named FL's project, a title naming
        neither file means another project though the list still names ours."""
        try:
            title = self.fl_title()
        except Exception:
            title = None
        if title_file(title) is not None:
            self.title_names = True
            current = self._watched_path()
            return current is not None and same_file(current, path)
        try:
            recent = self.mru()
        except Exception:
            recent = None
        recent = recent if isinstance(recent, str) and recent.lower().endswith(".flp") else None
        same = recent is not None and same_file(recent, path)
        if isinstance(title, str) and title:
            name = file_stem(path)
            if title_shows(title, name):
                self.title_names = True
                # ... unless FL's newest recent file is another file of that name
                return same or recent is None or file_stem(recent).casefold() != name.casefold()
            if title_shows(title, project_title):
                self.title_names = True
                return same or recent is None  # a copy of the project elsewhere has that title too
            if recent is not None and title_shows(title, file_stem(recent)):
                self.title_names = True
                return False
            if self.title_names:
                return False
        return same

    # --- piano-roll notes ------------------------------------------------------------

    def _on_pr(self, msg):
        pattern, channel, name = msg.get("p"), msg.get("c"), msg.get("n")
        if not is_int(pattern) or not is_int(channel):
            return
        context = (pattern, channel, name if isinstance(name, str) else None)
        if context != self.pr:
            self.pr = context
            self._write_context()

    def _set_notes(self, entries):
        """The session's notes, all at once (welcome), or none (new structure)."""
        self.notes = {}
        for entry in entries if isinstance(entries, list) else ():
            entry = clean_notes(entry)
            if entry is not None:
                self.notes[(entry["p"], entry["c"])] = entry
        if self.pr is not None:
            self._write_context()

    def _on_notes(self, msg):
        entry = clean_notes(msg)
        if entry is None:
            return None
        where = (entry["p"], entry["c"])
        self.notes[where] = entry
        if self.pr is not None and self.pr[:2] == where:
            self._write_context()
        if entry["from"] == self.my_id:
            self.log("Dina %d noter är skickade" % len(entry["notes"]))
        else:
            self.log("%s skickade %d noter till kanal %d i mönster %d"
                     % (entry["by"], len(entry["notes"]), entry["c"] + 1, entry["p"]))
        return {"t": "notes", "p": entry["p"], "c": entry["c"], "by": entry["by"], "from": entry["from"],
                "n": len(entry["notes"])}

    def _write_context(self):
        """Tell the piano roll scripts what the piano roll shows and the session's
        notes for it. `time` says the bridge is running (they check its age)."""
        pattern, channel, name = self.pr
        entry = self.notes.get((pattern, channel))
        session = None if entry is None else {k: entry[k] for k in ("v", "by", "ppq", "notes")}
        self.next_context = time.monotonic() + self.context_interval
        try:
            os.makedirs(self.notes_dir, exist_ok=True)
            write_json(os.path.join(self.notes_dir, "context.json"),
                       {"pattern": pattern, "channel": channel, "name": name, "time": time.time(),
                        "session": session})
        except PermissionError:
            self.next_context = 0.0  # a script is reading it right now: again on the next round
        except OSError as e:
            self.log("Kunde inte skriva till %s: %s" % (self.notes_dir, e))

    def _watch_outbox(self, now):
        path = os.path.join(self.notes_dir, "outbox.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self.outbox_broken = None
            return
        except (OSError, ValueError):
            # Not whole yet (or held by the script): next round. One that stays broken goes.
            if self.outbox_broken is None:
                self.outbox_broken = now
            elif now - self.outbox_broken > OUTBOX_GIVE_UP:
                self.outbox_broken = None
                self.log("%s gick inte att läsa och togs bort" % path)
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        self.outbox_broken = None
        try:
            os.remove(path)
        except OSError:
            return  # read again next round; sending it now could send it twice
        notes = clean_outbox(data)
        if notes is None:
            self.log("Noterna från pianorullen gick inte att läsa (högst %d noter med %d fält var)"
                     % (MAX_NOTES, NOTE_FIELDS))
            return
        pattern, channel, ppq, rows = notes
        msg = {"t": "notes_put", "p": pattern, "c": channel, "ppq": ppq, "notes": rows}
        if self.fl_fp is not None:
            msg["fp"] = self.fl_fp  # the relay refuses notes made for another structure
        if not self.in_session() or not self._to_relay(msg):
            self.log("Noterna skickades inte: FL är inte med i någon session")
            return
        self.log("Skickar %d noter för kanal %d i mönster %d" % (len(rows), channel + 1, pattern))


def parse_address(text, default_port=31415):
    host, _, port = text.rpartition(":")
    if not host:
        return text, default_port
    return host, int(port)


async def run(args):
    host, port = parse_address(args.relay)
    to_fl, from_fl = args.to_fl or args.port, args.from_fl or args.port
    import flpwatch  # only the real bridge watches the drives; the tests don't

    watcher = flpwatch.FlpWatcher().start()  # where FL saves (FL tells nobody): see Bridge._watched_path
    places = {"flp_writes": watcher.recent}
    if args.data_dir:  # the cache and bridge-state.json there (the piano roll scripts' folder stays)
        places.update(cache_dir=os.path.join(args.data_dir, "cache"),
                      state_path=os.path.join(args.data_dir, STATE_FILE))
    bridge = Bridge(host, port, send_to_fl=None, name=args.name, project=args.project,
                    projects_dir=args.projects_dir, auto_open=args.auto_open, password=args.password,
                    open_merged=not args.no_open, **places)
    log("Bryggan igång: FL <-> MIDI '%s'%s <-> relay %s:%d"
        % (to_fl, "" if to_fl == from_fl else " / '%s'" % from_fl, host, port))
    log("Projektet delas när du sparar det: %s" % (bridge.project or "det som FL senast öppnade eller sparade"))
    log("Andras versioner hamnar i %s%s" % (bridge.projects_dir, " och öppnas i FL" if args.auto_open else ""))
    try:
        await run_midi(bridge, to_fl, from_fl)
    finally:
        watcher.stop()


async def run_midi(bridge, to_fl="FLSync", from_fl=None, logger=log):
    """Start `bridge` and keep it connected to FL over the loopMIDI port(s) until
    cancelled; closes it at the end. The launcher (app/flsync.py) uses this too."""
    import winmidi  # only the real bridge needs Windows MIDI; the tests don't

    from_fl = from_fl or to_fl
    log = logger
    bridge.start()
    last_problem = None
    try:
        while True:
            # Windows invalidates open MIDI ports whenever the device list changes
            # (e.g. a new loopMIDI port), so be ready to open them again.
            try:
                out = winmidi.MidiOut(to_fl)
                try:
                    inp = winmidi.MidiIn(from_fl)
                except winmidi.MidiError:
                    out.close()
                    raise
            except winmidi.MidiError as e:
                if str(e) != last_problem:
                    log("%s. Försöker igen ..." % e)
                    last_problem = str(e)
                await asyncio.sleep(MIDI_RETRY)
                continue
            last_problem = None
            await asyncio.sleep(0.3)  # the first events after opening a port can get lost
            bridge.send_to_fl = out.send
            bridge.incoming = inp.received  # WinMM appends here from its own thread
            log("MIDI-porten är öppen, väntar på FL. Avsluta med Ctrl+C")
            try:
                await bridge.pump()
            except winmidi.MidiError as e:
                log("MIDI-porten slutade fungera (%s), öppnar den igen" % e)
            except Exception:  # a bug: start over with the port rather than stop for good
                log("MIDI-porten slutade fungera (fel i FLSync), öppnar den igen:\n" + traceback.format_exc())
            finally:
                inp.close()
                out.close()
            await asyncio.sleep(MIDI_RETRY / 2)
    finally:
        bridge.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="FLSync-brygga mellan FL Studio och relayn")
    parser.add_argument("--relay", default="127.0.0.1:31415", help="relayns adress, värd:port")
    parser.add_argument("--port", default="FLSync", help="loopMIDI-porten som FL och bryggan delar")
    parser.add_argument("--name", help="namnet de andra ser (standard: ditt Windows-namn)")
    parser.add_argument("--project", help="projektfilen (.flp) som delas när du sparar "
                                          "(standard: den som FL senast öppnade eller sparade)")
    parser.add_argument("--projects-dir", help="mappen där andras versioner sparas "
                                               "(standard: FL Studio\\Projects\\FLSync bland dina dokument)")
    parser.add_argument("--auto-open", action="store_true", help="öppna andras nya versioner i FL direkt")
    parser.add_argument("--no-open", action="store_true",
                        help="öppna aldrig versioner i FL av dig själv (inte ens sammanslagna när du sparar)")
    parser.add_argument("--data-dir", help="mapp för cachen och bryggans tillstånd "
                                           "(standard: %%LOCALAPPDATA%%\\FLSync)")
    parser.add_argument("--password", help="sessionens lösenord, om relayn har ett")
    parser.add_argument("--to-fl", help="annan port för det som går till FL (valfritt)")
    parser.add_argument("--from-fl", help="annan port för det som kommer från FL (valfritt)")
    args = parser.parse_args(argv)
    try:
        # Names and paths can have any characters: print them even into a pipe or a file.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        return asyncio.run(run(args)) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
