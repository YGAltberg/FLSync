"""What the launcher asks this computer (Windows): which programs run and
since when, the MIDI ports, FL's saved MIDI settings, network addresses; and
what it does to it: open files, FLSync's window and the browser. The tests use
a fake System with the same methods, so nothing here runs in them."""

import ipaddress
import itertools
import os
import re
import socket
import subprocess
import sys
import urllib.request
import webbrowser

FL_PROGRAMS = ("fl64.exe", "fl64 (scaled).exe", "fl.exe")  # FL 2024-2026 ship all three
LOOPMIDI = "loopmidi.exe"
PUBLIC_IP_URLS = ("https://api.ipify.org", "https://ipv4.icanhazip.com")
APP_BROWSERS = ((r"Microsoft\Edge\Application", "msedge.exe"), (r"Google\Chrome\Application", "chrome.exe"))
APP_WINDOW = "Chrome_WidgetWin_1"  # the class of Edge's and Chrome's top-level windows
WINDOW_TITLE = "FLSync"             # the page's <title>: its window's
WINDOW_SIZE = (1000, 880)


class Process:
    def __init__(self, name, pid, started=None, exe=None, version=None):
        self.name = name
        self.pid = pid
        self.started = started  # Unix time, or None if Windows didn't say
        self.exe = exe          # its program file, for FL (None if Windows didn't say)
        self.version = version  # an FL's version, "26.1.7.5653" (None if it can't be read)

    @property
    def major(self):
        """An FL's major version as in its settings key: 25 for FL Studio 2025."""
        try:
            return int(self.version.split(".")[0])
        except (AttributeError, ValueError):
            return None

    def __repr__(self):
        return "Process(%r, %r)" % (self.name, self.pid)


class System:
    """The real computer."""

    def processes(self, names=(LOOPMIDI,) + FL_PROGRAMS):
        """The running programs with these exe names (lower case), or None if
        the list can't be read."""
        return running(names)

    def midi_ports(self):
        """(input names, output names), or None if Windows MIDI can't be asked."""
        try:
            import winmidi
            return winmidi.port_names(True), winmidi.port_names(False)
        except Exception:  # not Windows, or WinMM refused
            return None

    def fl_midi_settings(self, port, major=None):
        return fl_midi_settings(port, major)

    def fl_setup_needed(self, port):
        """{major: True if FL Studio <major>'s MIDI settings lack FLSync} for each
        FL version that has settings (it has been started once)."""
        return fl_setup_needed(port)

    def configure_fl(self, major):
        """Put FLSync into FL Studio <major>'s MIDI settings (FL must be closed:
        it writes its own when it exits). The port number, or None."""
        return configure_fl(major)

    def local_addresses(self):
        return local_addresses()

    def default_address(self):
        return default_address()

    def public_address(self):
        return public_address()

    def open_file(self, path):
        if path.lower().endswith(".flp"):
            import bridge  # the launcher has it on the path
            bridge.open_file(path)  # in the FL that runs: Windows may have another FL version for .flp
        else:
            os.startfile(path)  # Explorer for a folder

    def open_browser(self, url):
        return webbrowser.open(url)

    def open_window(self, url, profile):
        """FLSync's own window showing `url`: the browser process (Popen), or
        None if there is no Edge or Chrome to make one (then use open_browser)."""
        return open_window(url, profile)

    def close_window(self, process):
        close_window(process)

    def focus_window(self, pid=None):
        """Bring FLSync's window to the front: True if there was one."""
        return focus_window(pid)

    def message(self, text):
        message_box(text)

    def user_name(self):
        return os.environ.get("USERNAME") or os.environ.get("USER") or "FL"


# --- programs ----------------------------------------------------------------------------

def running(names):
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes, snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    first, following = kernel32.Process32FirstW, kernel32.Process32NextW
    for fn in (first, following):
        fn.argtypes, fn.restype = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = first(handle, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if name in names:
                pid = entry.th32ProcessID
                exe = image_path(kernel32, pid) if name in FL_PROGRAMS else None
                found.append(Process(name, pid, started(kernel32, pid), exe, file_version(exe) if exe else None))
            ok = following(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return found


def image_path(kernel32, pid):
    """Process `pid`'s program file, or None."""
    import ctypes
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return path.value if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def file_version(path):
    """A program file's version ("26.1.7.5653"), or None."""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version")
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(wintypes.UINT)]
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return None
        info, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(length)) or length.value < 52:
            return None
        fixed = (wintypes.DWORD * 13).from_address(info.value)  # VS_FIXEDFILEINFO
        if fixed[0] != 0xFEEF04BD:
            return None
        ms, ls = fixed[2], fixed[3]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def started(kernel32, pid):
    """When process `pid` started (Unix time), or None."""
    import ctypes
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        ticks = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime  # 100 ns since 1601
        return ticks / 1e7 - 11644473600
    finally:
        kernel32.CloseHandle(handle)


# --- FL's MIDI settings ----------------------------------------------------------------------

def fl_midi_settings(port, major=None):
    """FL's saved settings for the MIDI port `port`: FL Studio <major>'s, or
    without `major` the newest FL version's that has MIDI settings: {"fl",
    "input", "output", "outputs"} with the registry values as text,
    "input"/"output" None if FL doesn't list the port; None if there are none
    (not Windows, FL never configured)."""
    try:
        import winreg
    except ImportError:
        return None
    versions = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Image-Line") as root:
            for i in itertools.count():
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                match = re.fullmatch(r"FL Studio (\d+)", name)
                if match and (major is None or int(match.group(1)) == major):
                    versions.append((int(match.group(1)), name))
    except OSError:
        return None
    for _, name in sorted(versions, reverse=True):
        devices = r"Software\Image-Line\%s\Devices" % name
        inputs = _subkeys(winreg, devices + r"\MIDI input")
        outputs = _subkeys(winreg, devices + r"\MIDI output")
        if inputs is None and outputs is None:
            continue
        inputs, outputs = inputs or {}, outputs or {}
        return {"fl": name, "input": _named(inputs, port), "output": _named(outputs, port),
                "outputs": outputs}
    return None


def _setup():
    """setup/setup.py, which knows what FL's settings need (the launcher's copy has it)."""
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setup")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import setup
    return setup


def fl_setup_needed(port="FLSync"):
    try:
        setup = _setup()
        system = setup.System()
        keys = [k for k in system.reg_keys("HKCU", setup.IMAGE_LINE) if setup.FL_KEY.match(k)]
        return {int(setup.FL_KEY.match(k).group(1)): bool(setup.fl_settings_changes(system, k)[2]) for k in keys}
    except Exception:  # no registry here, or an FL key that can't be read: nothing to say
        return {}


def configure_fl(major):
    try:
        setup = _setup()
        system = setup.System()
        port, _, changes = setup.fl_settings_changes(system, "FL Studio %d" % major)
        for path, name, value in changes:
            system.reg_set("HKCU", path, name, value, setup.REG_SZ)
        return port
    except Exception:
        return None


def _subkeys(winreg, path):
    """{subkey name: {value name: text}} under HKCU\\path, or None if it isn't there."""
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return None
    found = {}
    with root:
        for i in itertools.count():
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            values = {}
            try:
                with winreg.OpenKey(root, name) as key:
                    for j in itertools.count():
                        try:
                            value_name, value, _ = winreg.EnumValue(key, j)
                        except OSError:
                            break
                        values[value_name] = str(value)
            except OSError:
                continue
            found[name] = values
    return found


def _named(devices, port):
    for name, values in devices.items():
        if name.lower() == port.lower():
            return values
    return None


# --- the network ----------------------------------------------------------------------------

def local_addresses():
    """This computer's IPv4 addresses (all adapters)."""
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        if info[4][0] not in found:
            found.append(info[4][0])
    return found


def default_address():
    """The address this computer's internet traffic leaves by (nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))
            return sock.getsockname()[0]
    except OSError:
        return None


def public_address(urls=PUBLIC_IP_URLS, timeout=4.0):
    """The public address the internet sees, asked of a public service, or None."""
    opener = urllib.request.build_opener()
    for url in urls:
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "FLSync"}), timeout=timeout) as answer:
                text = answer.read(64).decode("ascii", "replace").strip()
            address = ipaddress.IPv4Address(text)
        except (OSError, ValueError):
            continue
        if address.is_global:
            return str(address)
    return None


# --- FLSync's window ---------------------------------------------------------------------------

def app_browser():
    """Edge (in every Windows 10 and 11) or else Chrome: a browser that shows a
    page as an app window of its own (--app). None if neither is there."""
    try:
        import winreg
    except ImportError:
        return None
    for folder, exe in APP_BROWSERS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\App Paths\%s" % exe) as key:
                    path = str(winreg.QueryValue(key, None) or "").strip().strip('"')
            except OSError:
                continue
            if path and os.path.isfile(path):
                return path
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            path = os.path.join(base, folder, exe) if base else None
            if path and os.path.isfile(path):
                return path
    return None


def window_args(browser, url, profile, size=WINDOW_SIZE):
    """The browser's command line for FLSync's window: an app window of its
    own (no tabs, no address bar) with its own profile, so it is a program of
    its own that ends when the window closes. Timers aren't slowed down while
    it is minimized: the page tells FLSync it is still there."""
    return [browser, "--app=" + url, "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
            "--disable-background-mode", "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
            "--disable-session-crashed-bubble", "--hide-crash-restore-bubble", "--disable-features=Translate",
            "--window-size=%d,%d" % size]


def open_window(url, profile):
    browser = app_browser()
    if browser is None:
        return None
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen(window_args(browser, url, profile), close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    each = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [each, wintypes.LPARAM]
    for fn in (user32.GetClassNameW, user32.GetWindowTextW):
        fn.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    for fn in (user32.IsWindowVisible, user32.IsIconic, user32.SetForegroundWindow):
        fn.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32, each


def app_windows(pid=None, title=WINDOW_TITLE):
    """The visible app windows (Edge's or Chrome's) of process `pid`, those
    with a title first (the browser has an untitled one too), or else those
    whose title is FLSync's."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32, each = _user32()
    found, text = [], ctypes.create_unicode_buffer(512)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or not user32.GetClassNameW(hwnd, text, len(text)):
            return True
        if text.value != APP_WINDOW:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        titled = user32.GetWindowTextW(hwnd, text, len(text)) > 0
        if pid is not None:
            if owner.value == pid:
                found.append((not titled, hwnd))
        elif titled and text.value.startswith(title):
            found.append((False, hwnd))
        return True

    user32.EnumWindows(each(visit), 0)
    return [hwnd for _, hwnd in sorted(found, key=lambda item: item[0])]


def focus_window(pid=None):
    """Bring FLSync's window (of browser process `pid`, else by its title) to
    the front, restored if it was minimized. True if there was one."""
    try:
        windows = app_windows(pid) if pid else []
        windows = windows or app_windows()
        if not windows:
            return False
        user32, _ = _user32()
        if user32.IsIconic(windows[0]):
            user32.ShowWindow(windows[0], 9)  # SW_RESTORE
        user32.SetForegroundWindow(windows[0])
        return True
    except (OSError, AttributeError, ValueError):
        return False


def close_window(process, wait=3.0):
    """Close FLSync's window: its browser process is asked as when clicking X
    (WM_CLOSE: it ends tidily, no "restore pages" next time), and ended if it
    doesn't within `wait` seconds."""
    if process is None or process.poll() is not None:
        return
    try:
        user32, _ = _user32()
        for hwnd in app_windows(process.pid):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        process.wait(wait)
    except subprocess.TimeoutExpired:
        process.kill()
    except (OSError, AttributeError, ValueError):
        pass


def message_box(text, title="FLSync"):
    """A Windows message box: what FLSync (no console of its own) has to say
    when it can't show its window."""
    try:
        import ctypes
        ctypes.WinDLL("user32").MessageBoxW(None, str(text), str(title), 0x10 | 0x10000)  # MB_ICONERROR, MB_SETFOREGROUND
    except (OSError, AttributeError):
        pass


# --- one launcher at a time ---------------------------------------------------------------------

def claim(name):
    """Hold a Windows mutex named `name` for as long as this process runs.
    False if another process holds it already (or None when not on Windows):
    then it can be tried again once that one has ended."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: not ours, and our handle mustn't keep it
        kernel32.CloseHandle(handle)
        return False
    claim.handles.append(handle)  # closed by Windows when the process ends
    return True


claim.handles = []


def on_console_close(callback, wait):
    """Call `callback` when the console window is closed (or Windows logs off
    or shuts down), then give it up to `wait()` to finish before Windows ends
    the process. Returns False if it couldn't be arranged."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event):
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            callback()
            wait()
            return True
        return False  # Ctrl+C and Ctrl+Break: Python's own handling

    routine = handler_type(handler)
    on_console_close.routines.append(routine)  # must outlive this call
    try:
        return bool(ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(routine, True))
    except (OSError, AttributeError):
        return False


on_console_close.routines = []
