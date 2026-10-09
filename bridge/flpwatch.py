"""Where FL saves: the .flp files being written on this computer's drives.

FL keeps its recent-files list in memory and writes it to the registry only
when it closes, and its script API doesn't tell the project's path. So the
bridge learns where FL saves from Windows itself: ReadDirectoryChangesW on
each local drive reports every file written, and the .flp ones are kept with
when they were written. FL's window title names the file (bridge.py matches
the two). Windows only; elsewhere nothing is watched.

    watcher = FlpWatcher()
    watcher.start()          # one thread per drive
    watcher.recent()         # [(time.monotonic() when written, path), ...], newest last
    watcher.stop()
"""

import collections
import ctypes
import os
import string
import sys
import threading
import time

KEPT = 200                 # writes remembered
BUFFER = 64 << 10          # bytes of changes per read; more in one go and Windows drops them (we read again)
FILE_LIST_DIRECTORY = 0x0001
SHARE_ALL = 0x7            # read, write, delete
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a folder
NOTIFY = 0x1 | 0x8 | 0x10  # file name (created, renamed), size, last write
ADDED, MODIFIED, RENAMED_NEW = 1, 3, 5
DRIVE_FIXED = 3


def changes(buffer, size):
    """(action, name) of each FILE_NOTIFY_INFORMATION record in buffer[:size]."""
    found, at = [], 0
    while at + 12 <= size:
        following, action, length = (int.from_bytes(buffer[at + i:at + i + 4], "little") for i in (0, 4, 8))
        name = bytes(buffer[at + 12:at + 12 + length]).decode("utf-16-le", "replace")
        found.append((action, name))
        if not following:
            break
        at += following
    return found


def fixed_drives():
    """The roots of this computer's local hard drives ("C:\\\\", ...)."""
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = kernel32.GetLogicalDrives()
    return [letter + ":\\" for i, letter in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and kernel32.GetDriveTypeW(letter + ":\\") == DRIVE_FIXED]


class FlpWatcher:
    """Remembers the .flp files written under `roots` (default: every local
    hard drive) while it runs."""

    def __init__(self, roots=None, logger=None):
        self.roots = roots
        self.log = logger or (lambda text: None)
        self.writes = collections.deque(maxlen=KEPT)
        self.lock = threading.Lock()
        self.threads = []
        self.handles = []
        self.stopping = False

    def start(self):
        if sys.platform != "win32" or self.threads:
            return self
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                              ctypes.c_void_p]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        for root in self.roots if self.roots is not None else fixed_drives():
            handle = k32.CreateFileW(root, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                                     FILE_FLAG_BACKUP_SEMANTICS, None)
            if not handle or handle == wintypes.HANDLE(-1).value:
                self.log("Kan inte se ändringar på %s (fel %d)" % (root, ctypes.get_last_error()))
                continue
            self.handles.append(handle)
            thread = threading.Thread(target=self._watch, args=(root, handle), daemon=True,
                                      name="flp-watch %s" % root)
            thread.start()
            self.threads.append(thread)
        return self

    def _watch(self, root, handle):
        from ctypes import wintypes

        buffer = ctypes.create_string_buffer(BUFFER)
        size = wintypes.DWORD()
        while not self.stopping:
            if not self._k32.ReadDirectoryChangesW(handle, buffer, BUFFER, True, NOTIFY, ctypes.byref(size),
                                                   None, None):
                if self.stopping:
                    return
                time.sleep(1.0)  # a hiccup (not stopping): try again, never spin
                continue
            now = time.monotonic()
            for action, name in changes(buffer.raw, size.value):
                if action in (ADDED, MODIFIED, RENAMED_NEW) and name.lower().endswith(".flp"):
                    with self.lock:
                        self.writes.append((now, os.path.join(root, name)))

    def recent(self, since=None):
        """[(when, path)] of the .flp files written (since `since`, a
        time.monotonic()), oldest first; the same path once, at its newest."""
        with self.lock:
            writes = list(self.writes)
        newest = {}
        for when, path in writes:
            if since is None or when >= since:
                newest[os.path.normcase(path)] = (when, path)
        return sorted(newest.values())

    def stop(self):
        self.stopping = True
        for handle in self.handles:
            self._k32.CancelIoEx(handle, None)  # ends the blocked read
        for thread in self.threads:
            thread.join(2)
        for handle in self.handles:
            self._k32.CloseHandle(handle)
        self.handles, self.threads = [], []
