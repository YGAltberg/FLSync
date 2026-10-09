"""The installer, setup/setup.py, on a fake computer (docs/phase3.md, "Setup"):
a registry in a dict, processes in a list and every folder in a temporary
directory. Setup never reaches the real registry, starts a program or touches
a file outside the temporary folder here: FakeSystem refuses to."""

import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_setup():
    spec = importlib.util.spec_from_file_location("setup_under_test", os.path.join(ROOT, "setup", "setup.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_setup()
REG_SZ, REG_DWORD = setup.REG_SZ, setup.REG_DWORD
UNINSTALL = setup.UNINSTALL
PORTS = setup.LOOPMIDI_PORTS
RUN = setup.RUN
CHANGES = {"reg_set", "reg_delete_value", "reg_delete_key", "start", "kill", "url", "shortcuts", "install"}


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_junction(link, target):
    """A directory junction like install.ps1 made for developers; False if Windows won't."""
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return True
    except (ImportError, AttributeError, OSError):
        pass
    done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    return done.returncode == 0


def guard(path):
    """Setup must never open a key named Registrations (FL's license data)."""
    if "registrations" in path.lower().split("\\"):
        raise AssertionError("setup went near FL's license data: %s" % path)
    return path


class FakeRegistry:
    """HKCU and HKLM (both views) as a dict. Names keep their case but are
    found in any case, as in Windows."""

    def __init__(self):
        self.keys = {}  # (root, view, path in lower case) -> [path, {name in lower case: (name, value, type)}]

    def _id(self, root, path, view):
        return root, view if root == "HKLM" else None, path.strip("\\").lower()

    def add(self, root, path, values=None, view=None):
        parts = path.strip("\\").split("\\")
        for i in range(1, len(parts) + 1):
            sub = "\\".join(parts[:i])
            self.keys.setdefault(self._id(root, sub, view), [sub, {}])
        for name, value in (values or {}).items():
            self.set(root, path, name, value, REG_DWORD if isinstance(value, int) else REG_SZ, view)

    def subkeys(self, root, path, view=None):
        root, view, low = self._id(root, path, view)
        if (root, view, low) not in self.keys:
            return []
        prefix = low + "\\"
        return sorted(name.split("\\")[-1] for (r, v, l), (name, _) in self.keys.items()
                      if r == root and v == view and l.startswith(prefix) and "\\" not in l[len(prefix):])

    def values(self, root, path, view=None):
        key = self.keys.get(self._id(root, path, view))
        if key is None:
            return None
        return {name: (value, kind) for name, value, kind in key[1].values()}

    def set(self, root, path, name, value, kind, view=None):
        if self._id(root, path, view) not in self.keys:
            self.add(root, path, view=view)
        values = self.keys[self._id(root, path, view)][1]
        old = values.get(name.lower())
        values[name.lower()] = (old[0] if old else name, value, kind)

    def delete_value(self, root, path, name):
        key = self.keys.get(self._id(root, path, None))
        if key is None or name.lower() not in key[1]:
            raise FileNotFoundError(2, "Det går inte att hitta filen", name)
        del key[1][name.lower()]

    def delete_key(self, root, path):
        root, view, low = self._id(root, path, None)
        if (root, view, low) not in self.keys:
            raise FileNotFoundError(2, "Det går inte att hitta filen", path)
        for key in [k for k in self.keys if k[:2] == (root, view) and (k[2] == low or k[2].startswith(low + "\\"))]:
            del self.keys[key]

    def value(self, path, name, root="HKCU"):
        return setup.lookup(self.values(root, path), name)


class FakeSystem(setup.System):
    """The computer for the tests. File operations are the real ones, but only
    inside `base`; everything else is pretend and written down in `calls`."""

    def __init__(self, base):
        self.base = os.path.abspath(base)
        self.reg = FakeRegistry()
        self.running = []          # (pid, exe name, path)
        self.calls = []            # (what, ...) in order
        self.ports = []            # MIDI ports Windows has right now
        self.midi = True           # False: Windows MIDI can't be asked
        self.winget = None         # path of winget, if it is "installed"
        self.winget_code = 0
        self.on_sleep = None
        self.pid = 1000
        self.pf = os.path.join(self.base, "Program Files")
        self.pf86 = os.path.join(self.base, "Program Files (x86)")
        for folder in (self.pf, self.pf86, self.desktop_dir(), self.programs_dir()):
            os.makedirs(folder)

    # --- folders -----------------------------------------------------------------------

    def local_data_dir(self):
        return os.path.join(self.base, "AppData", "Local", "FLSync")

    def fl_user_dir(self):
        return os.path.join(self.base, "Dokument", "Image-Line")

    def desktop_dir(self):
        return os.path.join(self.base, "Skrivbord")

    def programs_dir(self):
        return os.path.join(self.base, "Start Menu", "Programs")

    def program_files_dirs(self):
        return [self.pf, self.pf86]

    # --- registry ----------------------------------------------------------------------

    def reg_keys(self, root, path, view=None):
        return self.reg.subkeys(root, guard(path), view)

    def reg_values(self, root, path, view=None):
        return self.reg.values(root, guard(path), view)

    def reg_set(self, root, path, name, value, kind):
        assert root == "HKCU", root
        self.calls.append(("reg_set", guard(path), name, value))
        self.reg.set(root, path, name, value, kind)

    def reg_delete_value(self, root, path, name):
        self.calls.append(("reg_delete_value", guard(path), name))
        self.reg.delete_value(root, path, name)

    def reg_delete_key(self, root, path):
        self.calls.append(("reg_delete_key", guard(path)))
        self.reg.delete_key(root, path)

    # --- processes ---------------------------------------------------------------------

    def launch(self, path):
        """A program runs (the user started it)."""
        self.pid += 1
        self.running.append((self.pid, os.path.basename(path), path))
        if os.path.basename(path).lower() == "loopmidi.exe":
            self.ports = self.saved_ports()  # loopMIDI makes its ports when it starts

    def quit(self, name):
        self.running = [p for p in self.running if p[1].lower() != name.lower()]
        if name.lower() == "loopmidi.exe":
            self.ports = []

    def saved_ports(self):
        return [name for name, value in (self.reg.values("HKCU", PORTS) or {}).items() if value == (1, REG_DWORD)]

    def processes(self):
        return [(pid, name) for pid, name, _ in self.running]

    def process_path(self, pid):
        return next((path for p, _, path in self.running if p == pid), None)

    def start(self, exe, minimized=False):
        self.check(exe)
        self.calls.append(("start", exe, minimized))
        self.launch(exe)

    def kill(self, name):
        self.calls.append(("kill", name))
        self.quit(name)
        return True

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        if self.on_sleep:
            self.on_sleep()

    # --- other programs ----------------------------------------------------------------

    def which(self, name):
        return self.winget if name == "winget" else None

    def run(self, args, live=False, timeout=None, env=None):
        if self.winget and args[:1] == [self.winget] and args[1:] == ["--version"]:
            self.calls.append(("run", list(args)))
            return 0, "v1.9.25200"
        if self.winget and args[:2] == [self.winget, "install"]:
            self.calls.append(("install", list(args), live))
            if self.winget_code == 0:
                self.add_loopmidi(autostart=False, ports=())
            return self.winget_code, ""
        raise AssertionError("setup ran a program it shouldn't: %r" % (args,))

    def open_url(self, url):
        self.calls.append(("url", url))

    def make_shortcuts(self, links, target, workdir, description, icon=None, arguments=""):
        self.calls.append(("shortcuts", list(links), target))
        for link in links:
            write(self.check(link), json.dumps({"target": target, "workdir": workdir, "description": description,
                                                "icon": icon, "arguments": arguments}))

    def midi_ports(self):
        return list(self.ports) if self.midi else None

    # --- files -------------------------------------------------------------------------

    def check(self, path):
        full = os.path.normcase(os.path.abspath(path))
        base = os.path.normcase(self.base)
        try:
            inside = os.path.commonpath([full, base]) == base
        except ValueError:
            inside = False
        if not inside:
            raise AssertionError("setup went outside the test folder: %s" % path)
        return path

    def leave(self, folder):
        self.calls.append(("leave", self.check(folder)))  # the tests' own working folder stays

    # --- the computer's contents ---------------------------------------------------------

    def add_fl(self, year=2025, version="25.2.4.5242", key=True, python=True, entry=True, folder=None):
        folder = folder or os.path.join(self.pf86, "Image-Line", "FL Studio %d" % year)
        write(os.path.join(folder, "FL64.exe"))
        write(os.path.join(folder, "uninstall.exe"))
        if python:
            write(os.path.join(folder, "Shared", "Python", "python.exe"))
        if entry:
            self.reg.add("HKLM", UNINSTALL + "\\FL Studio %d" % year, {
                "DisplayName": "FL Studio %d" % year, "DisplayVersion": version, "Publisher": "Image-Line",
                "UninstallString": os.path.join(folder, "uninstall.exe"),
                "DisplayIcon": os.path.join(folder, "Data", "System", "icon.ico") + ",0"}, view=32)
        if key:
            major = setup.major_version(version)
            for kind in ("MIDI input", "MIDI output"):
                self.reg.add("HKCU", r"Software\Image-Line\FL Studio %d\Devices\%s" % (major, kind))
        return folder

    def add_device(self, major, kind, name, port, **values):
        path = r"Software\Image-Line\FL Studio %d\Devices\%s\%s" % (major, kind, name)
        self.reg.add("HKCU", path, dict({"Port": str(port), "IDString": "-"}, **values))

    def add_loopmidi(self, autostart=True, running=False, ports=("Annan port",)):
        exe = os.path.join(self.pf86, "Tobias Erichsen", "loopMIDI", "loopMIDI.exe")
        write(exe)
        self.reg.add("HKLM", UNINSTALL + r"\{6b220f45-42ca-435c-95fd-1764cb849122}", {
            "DisplayName": "loopMIDI", "DisplayVersion": "1.0.16.27", "Publisher": "Tobias Erichsen",
            "DisplayIcon": os.path.join(self.base, "Package Cache", "loopMIDISetup.exe") + ",0"}, view=32)
        self.reg.add("HKLM", UNINSTALL + r"\{AEAF7978-3204-451D-8593-BC53EBDDA31D}",
                     {"DisplayName": "loopMIDIBlockLegacy", "DisplayVersion": "9.9.9.9"}, view=32)
        self.reg.add("HKCU", PORTS, {port: 1 for port in ports})
        if autostart:
            self.reg.add("HKCU", RUN, {"loopMIDI": exe})
        if running:
            self.launch(exe)
        return exe


SOURCE = {
    "app/flsync.py": "launcher",
    "app/__pycache__/flsync.cpython-312.pyc": "cache",
    "bridge/bridge.py": "bridge",
    "relay/relay.py": "relay",
    "tools/monitor.py": "monitor",
    "docs/phase3.md": "docs",
    "fl/FLSync/device_FLSync.py": "# name=FLSync",
    "fl/FLSync/flsync_client.py": "client",
    "fl/FLSync/__pycache__/flsync_client.cpython-312.pyc": "cache",
    "fl/Piano roll scripts/FLSync skicka noter.pyscript": "send",
    "fl/Piano roll scripts/FLSync hämta noter.pyscript": "fetch",
    "setup/setup.py": "setup",
    "tests/test_setup.py": "tests",
    "scratch/notes.txt": "scratch",
    "scripts/install.ps1": "old installer",
    "FLSync.cmd": "@echo off",
    "Installera FLSync.cmd": "@echo off",
    "README.md": "readme",
    ".gitignore": "__pycache__/",
}
INSTALLED = ["FLSync.cmd", "Installera FLSync.cmd", "README.md", "app/flsync.py", "bridge/bridge.py",
             "docs/phase3.md", "fl/FLSync/device_FLSync.py", "fl/FLSync/flsync_client.py",
             "fl/Piano roll scripts/FLSync hämta noter.pyscript",
             "fl/Piano roll scripts/FLSync skicka noter.pyscript", "relay/relay.py", "setup/setup.py",
             "tools/monitor.py"]


def files_in(folder):
    found = []
    for path, _, names in os.walk(folder):
        found += [os.path.relpath(os.path.join(path, n), folder).replace("\\", "/") for n in names]
    return sorted(found)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="flsync-setup-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.pc = FakeSystem(self.base)
        self.src = os.path.join(self.base, "Hämtade filer", "FLSync (1)")
        for name, text in SOURCE.items():
            write(os.path.join(self.src, name), text)
        self.answers, self.asked = [], []

    def run_setup(self, *flags, interactive=False, root=None):
        out = io.StringIO()

        def ask(prompt):
            self.asked.append(prompt)
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)
        runner = setup.Setup(self.pc, setup.parse_args(list(flags)), root=root or self.src,
                             out=lambda text="", end="\n": out.write(text + end), ask=ask, interactive=interactive)
        code = runner.run()
        self.output = out.getvalue()
        return code

    def standard(self):
        """FL 2025 that has run (two controllers on ports 10 and 11), loopMIDI installed but not running."""
        self.pc.add_fl()
        self.pc.add_device(25, "MIDI input", "Keystation 49es", 10, Type="0", Enabled="1", ScriptFolder="")
        self.pc.add_device(25, "MIDI output", "Microsoft GS Wavetable Synth", 11, Sync="0", SendAllNotesOff="1")
        self.pc.add_device(25, "MIDI output", "Microsoft MIDI Mapper", -1, Sync="0", SendAllNotesOff="1")
        return self.pc.add_loopmidi(autostart=False)

    @property
    def app(self):
        return os.path.join(self.pc.local_data_dir(), "app")

    def settings(self, *parts):
        return os.path.join(self.pc.fl_user_dir(), "FL Studio", "Settings", *parts)

    def device(self, major, kind):
        return self.pc.reg.values("HKCU", r"Software\Image-Line\FL Studio %d\Devices\%s\FLSync" % (major, kind))

    def changes(self):
        return [call for call in self.pc.calls if call[0] in CHANGES]

    def snapshot(self):
        files = {}
        for path, dirs, names in os.walk(self.base):
            for name in dirs + names:
                full = os.path.join(path, name)
                files[os.path.relpath(full, self.base)] = read(full) if os.path.isfile(full) else None
        return copy.deepcopy(self.pc.reg.keys), files, list(self.pc.running), list(self.pc.ports)


class InstallTest(SetupTest):
    def test_a_fresh_install_does_everything(self):
        exe = self.standard()
        self.assertEqual(self.run_setup(), 0, self.output)

        self.assertEqual(files_in(self.app), INSTALLED)  # no tests, caches, scratch or old installer
        self.assertEqual(files_in(self.settings("Hardware", "FLSync")), ["device_FLSync.py", "flsync_client.py"])
        self.assertEqual(read(self.settings("Piano roll scripts", "FLSync hämta noter.pyscript")), "fetch")
        self.assertEqual(read(self.settings("Piano roll scripts", "FLSync skicka noter.pyscript")), "send")

        # loopMIDI: the port saved (the other one untouched), loopMIDI started minimized with it, and at login
        self.assertEqual(self.pc.reg.values("HKCU", PORTS), {"Annan port": (1, REG_DWORD), "FLSync": (1, REG_DWORD)})
        self.assertIn(("start", exe, True), self.pc.calls)
        self.assertIn("FLSync", self.pc.ports)
        self.assertEqual(self.pc.reg.value(RUN, "loopMIDI"), ('"%s"' % exe, REG_SZ))

        # FL: the lowest port from 10 that no other device uses, everything as text
        self.assertEqual(self.device(25, "MIDI input"), {
            "Type": ("-3", REG_SZ), "ScriptFolder": ("FLSync", REG_SZ), "IDString": ("-", REG_SZ),
            "Port": ("12", REG_SZ), "Enabled": ("1", REG_SZ)})
        self.assertEqual(self.device(25, "MIDI output"), {
            "Port": ("12", REG_SZ), "Sync": ("0", REG_SZ), "SendAllNotesOff": ("1", REG_SZ),
            "IDString": ("-", REG_SZ)})
        keystation = self.pc.reg.values("HKCU", r"Software\Image-Line\FL Studio 25\Devices\MIDI input\Keystation 49es")
        self.assertEqual(setup.lookup(keystation, "Port"), ("10", REG_SZ))

        # shortcuts to the installed FLSync.cmd
        for folder in (self.pc.desktop_dir(), self.pc.programs_dir()):
            link = json.loads(read(os.path.join(folder, "FLSync.lnk")))
            self.assertEqual(link["target"], os.path.join(self.app, "FLSync.cmd"))
            self.assertEqual(link["workdir"], self.app)
            self.assertIsNone(link["icon"])

        for step in ("Steg 1 av 5: FL Studio", "Steg 2 av 5: loopMIDI", "Steg 3 av 5: Programmet",
                     "Steg 4 av 5: FL Studios MIDI-inställningar", "Steg 5 av 5: Genvägar"):
            self.assertIn(step, self.output)
        self.assertIn("Hittade FL Studio 2025 (version 25.2.4.5242)", self.output)
        self.assertIn("Klart!", self.output)
        self.assertIn("1. Starta FL Studio.", self.output)
        self.assertIn("2. Dubbelklicka på FLSync på skrivbordet", self.output)
        self.assertNotIn("Kvar att göra", self.output)

    def test_running_it_again_changes_nothing_and_says_so(self):
        self.standard()
        self.run_setup()
        before = self.snapshot()
        self.pc.calls.clear()
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual([c[0] for c in self.changes()], ["shortcuts"] * 3)  # remade, the same: FLSync, repair, uninstall
        self.assertEqual(self.snapshot(), before)
        for text in ("loopMIDI är igång och porten FLSync finns", "loopMIDI startar när du loggar in",
                     "Programmet i %s är redan den här versionen" % self.app,
                     "FLSync-scriptet i %s är redan den här versionen" % self.settings("Hardware", "FLSync"),
                     "Notscripten för pianorullen i", "FL Studio 2025: FLSync är redan inställt (port 12)",
                     "Uppdaterar genvägen FLSync"):
            self.assertIn(text, self.output)

    def test_a_dry_run_changes_nothing(self):
        self.pc.add_fl()
        self.pc.add_fl(2024, "24.2.2.4597")
        self.pc.add_loopmidi(autostart=False, running=True, ports=())  # running, without the port
        self.pc.winget = os.path.join(self.base, "winget.exe")
        self.pc.launch(os.path.join(self.pc.pf86, "Image-Line", "FL Studio 2025", "FL64.exe"))  # FL runs
        before = self.snapshot()
        self.assertEqual(self.run_setup("--dry-run", interactive=True), 0, self.output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changes(), [])
        self.assertNotIn("sleep", [c[0] for c in self.pc.calls])  # no waiting for FL either
        self.assertEqual(self.asked, [])
        self.assertIn("PROVKÖRNING", self.output)
        self.assertIn("FL Studio är igång. På riktigt ber installationen dig först stänga FL Studio", self.output)
        for text in ("loopMIDI är igång, men utan porten FLSync", "-> Startar om loopMIDI med porten FLSync",
                     "-> Kopierar programmet till", "-> Kopierar FLSync-scriptet till",
                     "-> Kopierar notscripten för pianorullen", "-> FL Studio 2024: ställer in FLSync på port 10",
                     "-> FL Studio 2025: ställer in FLSync på port 10", "-> Skapar genvägen FLSync",
                     "-> Ställer in att loopMIDI startar när du loggar in", "Ingenting har ändrats"):
            self.assertIn(text, self.output)

    def test_a_dry_run_shows_the_loopmidi_install(self):
        self.pc.add_fl()
        self.pc.winget = os.path.join(self.base, "winget.exe")
        before = self.snapshot()
        self.assertEqual(self.run_setup("--dry-run"), 0, self.output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changes(), [])
        self.assertIn("loopMIDI saknas", self.output)
        self.assertIn("-> Installerar loopMIDI med winget", self.output)
        self.assertIn("install --id TobiasErichsen.loopMIDI -e --source winget", self.output)

    def test_flsync_keeps_its_port_unless_another_device_has_it(self):
        self.standard()
        self.pc.add_device(25, "MIDI input", "FLSync", 13, Type="-3", ScriptFolder="FLSync", Enabled="1",
                           ConnectionCounter="29")
        self.pc.add_device(25, "MIDI output", "FLSync", 13, Sync="0", SendAllNotesOff="1")
        self.run_setup()
        self.assertIn("FL Studio 2025: FLSync är redan inställt (port 13)", self.output)
        self.assertEqual(setup.lookup(self.device(25, "MIDI input"), "ConnectionCounter"), ("29", REG_SZ))  # FL's own

    def test_choosing_the_port(self):
        def devices(**ports):
            return {name.replace("_", " "): {"Port": (str(port), REG_SZ)} for name, port in ports.items()}
        self.assertEqual(setup.choose_port(devices(FLSync=10, Keystation=-1), devices(FLSync=10)), (10, None))
        self.assertEqual(setup.choose_port(devices(FLSync=-1, Keystation=10), devices(Synth=11, Mapper=-1)),
                         (12, None))
        self.assertEqual(setup.choose_port({}, devices(FLSync=14)), (14, None))  # the output's, if the input has none
        port, note = setup.choose_port(devices(FLSync=10, Keystation=10), {})
        self.assertEqual(port, 11)
        self.assertIn("Port 10 används av en annan MIDI-enhet", note)
        self.assertEqual(setup.choose_port({"x": {"Port": ("abc", REG_SZ)}, "y": {}}, {}), (10, None))
        self.assertEqual(setup.choose_port({"flsync": {"port": (7, REG_DWORD)}}, {}), (7, None))  # any case, any type

    def test_every_fl_version_gets_set_up_and_a_new_one_is_mentioned(self):
        self.standard()
        self.pc.add_fl(2024, "24.2.2.4597")
        self.pc.add_fl(2026, "26.1.7.5653", key=False)  # installed, never started
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual(setup.lookup(self.device(24, "MIDI input"), "Port"), ("10", REG_SZ))
        self.assertEqual(setup.lookup(self.device(25, "MIDI input"), "Port"), ("12", REG_SZ))
        self.assertEqual(self.pc.reg.subkeys("HKCU", r"Software\Image-Line"), ["FL Studio 24", "FL Studio 25"])
        self.assertIn("FL Studio 2026 har inte startats än", self.output)
        self.assertIn("Ska du använda FL Studio 2026 med FLSync: starta det en gång", self.output)
        self.assertIn("FLSync kommer att köras med Python från FL Studio 2026", self.output)
        self.assertLess(self.output.index("FL Studio 2026 (version"), self.output.index("FL Studio 2024 (version"))

    def test_only_a_never_started_fl_is_almost_done_and_said_once(self):
        self.pc.add_fl(2026, "26.1.7.5653", key=False)  # installed, never started
        self.pc.add_loopmidi(autostart=False)
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual(self.output.count("starta det en gång"), 1, self.output)
        self.assertNotIn("Starta FL Studio en gång, stäng det", self.output)
        self.assertIn("Nästan klart: gör först det som står under Kvar att göra.", self.output)
        self.assertNotIn("Klart!", self.output)

    def test_the_start_menu_can_repair_and_uninstall(self):
        self.standard()
        self.assertEqual(self.run_setup(), 0, self.output)
        programs = self.pc.programs_dir()
        self.assertEqual(sorted(os.listdir(programs)),
                         ["Avinstallera FLSync.lnk", "FLSync.lnk", "Reparera FLSync.lnk"])
        setup_cmd = os.path.join(self.pc.local_data_dir(), "app", "Installera FLSync.cmd")
        repair = json.loads(read(os.path.join(programs, "Reparera FLSync.lnk")))
        remove = json.loads(read(os.path.join(programs, "Avinstallera FLSync.lnk")))
        self.assertEqual((repair["target"], repair["arguments"]), (setup_cmd, ""))
        self.assertEqual((remove["target"], remove["arguments"]), (setup_cmd, "--uninstall"))
        self.assertIn("Start-menyn > Reparera FLSync eller Avinstallera FLSync", self.output)

    def test_fl_is_found_in_many_ways(self):
        custom = self.pc.add_fl(2025, folder=os.path.join(self.base, "Musik", "FL"), key=True)
        self.pc.reg.add("HKLM", UNINSTALL + r"\FL Studio ASIO", {"DisplayName": "FL Studio ASIO"}, view=32)
        write(os.path.join(self.pc.pf86, "Image-Line", "FL Studio ASIO", "ilasio.dll"))
        write(os.path.join(self.pc.pf86, "Image-Line", "FL Studio 20", "FL.exe"))  # left behind, no FL64.exe
        self.pc.add_fl(2024, "24.2.2.4597", entry=False, python=False)  # only its folder
        latest = os.path.join(self.base, "Annan", "FL Studio 2026")
        self.pc.add_fl(2026, "26.1.7.5653", entry=False, folder=latest)  # only FL's own "Install path"
        self.pc.reg.add("HKCU", r"Software\Image-Line\Shared\Paths", {"Install path": latest})
        self.pc.reg.add("HKCU", r"Software\Image-Line\Registrations", {"Serial": "hemlig"})  # never opened
        self.pc.add_loopmidi()
        self.run_setup("--dry-run")
        found = [line.strip() for line in self.output.splitlines() if line.strip().startswith("Hittade")]
        self.assertEqual(found, ["Hittade FL Studio 2026 i %s" % latest,
                                 "Hittade FL Studio 2025 (version 25.2.4.5242) i %s" % custom,
                                 "Hittade FL Studio 2024 i %s" % os.path.join(self.pc.pf86, "Image-Line",
                                                                               "FL Studio 2024")])
        self.assertIn(os.path.join(latest, "Shared", "Python", "python.exe"), self.output)

    def test_without_fl_nothing_happens(self):
        self.pc.add_loopmidi()
        before = self.snapshot()
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Hittade inget FL Studio på den här datorn", self.output)

    def test_fl_studio_without_devices_yet(self):
        self.pc.add_fl(key=False)
        self.pc.reg.add("HKCU", r"Software\Image-Line\FL Studio 25\General", {"x": "1"})
        self.pc.add_loopmidi()
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual(setup.lookup(self.device(25, "MIDI output"), "Port"), ("10", REG_SZ))

    def test_the_shortcut_gets_the_launchers_icon(self):
        self.standard()
        write(os.path.join(self.src, "app", "flsync.ico"))
        self.run_setup()
        link = json.loads(read(os.path.join(self.pc.desktop_dir(), "FLSync.lnk")))
        self.assertEqual(link["icon"], os.path.join(self.app, "app", "flsync.ico"))

    def test_an_update_removes_what_is_gone(self):
        self.standard()
        mine = self.settings("Piano roll scripts", "Mitt eget.pyscript")
        write(mine, "mine")
        self.run_setup()
        os.remove(os.path.join(self.src, "fl", "FLSync", "flsync_client.py"))
        os.rename(os.path.join(self.src, "fl", "Piano roll scripts", "FLSync skicka noter.pyscript"),
                  os.path.join(self.src, "fl", "Piano roll scripts", "FLSync dela noter.pyscript"))
        shutil.rmtree(os.path.join(self.src, "tools"))
        write(os.path.join(self.src, "relay", "relay.py"), "relay 2")
        write(os.path.join(self.app, "app", "__pycache__", "x.pyc"), "cache")  # the launcher's: stays
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual(files_in(self.settings("Hardware", "FLSync")), ["device_FLSync.py"])
        self.assertEqual(sorted(os.listdir(self.settings("Piano roll scripts"))),
                         ["FLSync dela noter.pyscript", "FLSync hämta noter.pyscript", "Mitt eget.pyscript"])
        self.assertFalse(os.path.exists(os.path.join(self.app, "tools")))
        self.assertEqual(read(os.path.join(self.app, "relay", "relay.py")), "relay 2")
        self.assertTrue(os.path.exists(os.path.join(self.app, "app", "__pycache__", "x.pyc")))
        self.assertIn("Tar bort det gamla notscriptet", self.output)
        self.assertIn("Uppdaterar programmet i", self.output)
        self.assertIn("Är FLSync redan igång: stäng det och starta det igen", self.output)

    def test_a_linked_app_folder_is_replaced_never_followed(self):
        self.standard()
        elsewhere = os.path.join(self.base, "annat")
        write(os.path.join(elsewhere, "viktig.txt"), "behåll")
        os.makedirs(self.pc.local_data_dir())
        if not make_junction(self.app, elsewhere):
            self.skipTest("Windows made no junction here")
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertFalse(setup.is_link(self.app))
        self.assertEqual(files_in(self.app), INSTALLED)
        self.assertEqual(os.listdir(elsewhere), ["viktig.txt"])

    def test_the_installed_copy_has_no_internet_mark(self):
        self.standard()
        marked = os.path.join(self.src, "FLSync.cmd")
        try:
            write(marked + ":Zone.Identifier", "[ZoneTransfer]\nZoneId=3\n")  # unpacked from a download
        except OSError:
            self.skipTest("this file system has no alternate data streams")
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertFalse(os.path.exists(os.path.join(self.app, "FLSync.cmd") + ":Zone.Identifier"))
        self.assertTrue(os.path.exists(marked + ":Zone.Identifier"))  # the download itself is left as it was

    def test_running_from_the_installed_copy(self):
        self.standard()
        self.run_setup()
        self.assertEqual(self.run_setup(root=self.app), 0, self.output)
        self.assertIn("Programmet körs redan från %s, så det kopieras inte" % self.app, self.output)
        self.assertEqual(files_in(self.app), INSTALLED)

    def test_parts_missing_from_the_download(self):
        self.standard()
        shutil.rmtree(os.path.join(self.src, "relay"))
        os.remove(os.path.join(self.src, "app", "flsync.py"))
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("Delar av FLSync saknas (app\\flsync.py, relay\\relay.py)", self.output)

    def test_never_near_license_data(self):
        self.standard()
        self.pc.reg.add("HKCU", r"Software\Image-Line\Registrations", {"Serial": "hemlig"})
        self.pc.reg.add("HKCU", r"Software\Image-Line\FL Plugin Scanner", {"x": "1"})
        self.assertEqual(self.run_setup(), 0, self.output)  # FakeRegistry fails the test otherwise
        self.assertEqual(self.run_setup("--uninstall", "--yes"), 0, self.output)
        self.assertFalse(setup.allowed_key(r"Software\Image-Line\Registrations\FL Studio"))
        self.assertTrue(setup.allowed_key(r"Software\Image-Line\FL Studio 25\Devices"))


class FLRunningTest(SetupTest):
    def setUp(self):
        super().setUp()
        self.exe = self.standard()
        self.fl = os.path.join(self.pc.pf86, "Image-Line", "FL Studio 2025", "FL64.exe")

    def test_without_questions_it_stops_before_changing_anything(self):
        for name in ("FL64.exe", "FL64 (scaled).exe"):
            self.pc.running = []
            self.pc.launch(os.path.join(os.path.dirname(self.fl), name))
            before = self.snapshot()
            self.assertEqual(self.run_setup("--yes"), 2, self.output)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.changes(), [])
            self.assertIn("FL Studio är igång", self.output)
            self.assertIn("Stäng FL Studio och kör Installera FLSync.cmd igen", self.output)
            self.assertIn("Ingenting har ändrats", self.output)

    def test_it_waits_for_fl_to_close(self):
        self.pc.launch(self.fl)
        sleeps = []

        def fl_closes_on_the_third_look():
            sleeps.append(1)
            if len(sleeps) == 3:
                self.pc.quit("FL64.exe")
        self.pc.on_sleep = fl_closes_on_the_third_look
        self.assertEqual(self.run_setup(interactive=True), 0, self.output)
        self.assertEqual(len(sleeps), 3)
        self.assertIn("Spara ditt projekt och stäng FL Studio, så fortsätter installationen", self.output)
        self.assertIn("FL Studio är stängt.", self.output)
        self.assertEqual(setup.lookup(self.device(25, "MIDI input"), "Port"), ("12", REG_SZ))

    def test_skip_fl_config_leaves_fl_and_a_needed_loopmidi_restart(self):
        self.pc.launch(self.exe)  # loopMIDI runs without FLSync
        self.pc.launch(self.fl)
        self.assertEqual(self.run_setup("--yes", "--skip-fl-config"), 0, self.output)
        self.assertIsNone(self.device(25, "MIDI input"))
        self.assertNotIn("kill", [c[0] for c in self.pc.calls])  # a port made now would carry nothing to FL
        self.assertEqual(self.pc.reg.value(PORTS, "FLSync"), (1, REG_DWORD))  # there at loopMIDI's next start
        self.assertIn("Hoppar över FL Studios MIDI-inställningar (--skip-fl-config)", self.output)
        self.assertIn("Stäng FL Studio och kör Installera FLSync.cmd igen, så startas loopMIDI om", self.output)
        self.assertTrue(os.path.isdir(self.app))


class LoopMidiTest(SetupTest):
    def setUp(self):
        super().setUp()
        self.pc.add_fl()

    def test_a_running_loopmidi_without_the_port_is_restarted_with_it(self):
        exe = self.pc.add_loopmidi(running=True)
        self.assertNotIn("FLSync", self.pc.ports)
        self.assertEqual(self.run_setup(), 0, self.output)
        steps = [c[:2] for c in self.pc.calls if c[0] in ("kill", "start") or c[1:3] == (PORTS, "FLSync")]
        # stopped first: loopMIDI can't then save its old list over the new port
        self.assertEqual(steps, [("kill", "loopMIDI.exe"), ("reg_set", PORTS), ("start", exe)])
        self.assertIn("FLSync", self.pc.ports)
        self.assertIn("Porten FLSync finns nu.", self.output)

    def test_a_running_loopmidi_with_the_port_is_left_alone(self):
        self.pc.add_loopmidi(running=True, ports=("FLSync",))
        self.run_setup()
        self.assertEqual([c for c in self.pc.calls if c[0] in ("kill", "start")], [])
        self.assertIn("loopMIDI är igång och porten FLSync finns.", self.output)

    def test_when_windows_midi_cant_be_asked(self):
        self.pc.add_loopmidi(running=True, ports=("FLSync",))
        self.pc.midi = False
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertEqual([c for c in self.pc.calls if c[0] in ("kill", "start")], [])
        self.assertIn("om porten FLSync finns går inte att se", self.output)

    def test_a_port_that_never_shows_up(self):
        self.pc.add_loopmidi()
        self.pc.launch = lambda path: self.pc.running.append((1, os.path.basename(path), path))  # no ports
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertIn("Porten FLSync syns inte än", self.output)
        self.assertIn("Öppna loopMIDI och kontrollera att porten FLSync finns", self.output)

    def test_loopmidi_is_installed_with_winget(self):
        self.pc.winget = os.path.join(self.base, "winget.exe")
        self.answers = ["j"]
        self.assertEqual(self.run_setup(interactive=True), 0, self.output)
        install = [c for c in self.pc.calls if c[0] == "install"]
        self.assertEqual(install, [("install", [self.pc.winget, "install", "--id", "TobiasErichsen.loopMIDI", "-e",
                                                "--source", "winget", "--accept-source-agreements",
                                                "--accept-package-agreements"], True)])
        self.assertIn("Installera loopMIDI nu?", self.asked[0])
        exe = os.path.join(self.pc.pf86, "Tobias Erichsen", "loopMIDI", "loopMIDI.exe")
        self.assertIn(("start", exe, True), self.pc.calls)
        self.assertIn("FLSync", self.pc.ports)
        self.assertEqual(self.pc.reg.value(RUN, "loopMIDI"), ('"%s"' % exe, REG_SZ))
        self.assertNotIn("url", [c[0] for c in self.pc.calls])

    def test_yes_installs_loopmidi_without_asking(self):
        self.pc.winget = os.path.join(self.base, "winget.exe")
        self.assertEqual(self.run_setup("--yes"), 0, self.output)
        self.assertEqual(self.asked, [])
        self.assertEqual(len([c for c in self.pc.calls if c[0] == "install"]), 1)

    def test_without_winget_the_download_page_opens(self):
        self.assertEqual(self.run_setup("--yes"), 0, self.output)
        self.assertEqual([c for c in self.pc.calls if c[0] == "url"],
                         [("url", "https://www.tobias-erichsen.de/software/loopmidi.html")])
        self.assertEqual(self.pc.reg.value(PORTS, "FLSync"), (1, REG_DWORD))  # ready when loopMIDI comes
        self.assertIn("winget (Windows pakethanterare) finns inte", self.output)
        self.assertIn("Installera loopMIDI från https://www.tobias-erichsen.de/software/loopmidi.html", self.output)
        self.assertTrue(os.path.isdir(self.app))  # the rest is installed all the same
        self.assertIsNotNone(self.device(25, "MIDI input"))

    def test_a_failing_winget_opens_the_download_page(self):
        self.pc.winget = os.path.join(self.base, "winget.exe")
        self.pc.winget_code = 0x8A150011
        self.assertEqual(self.run_setup("--yes"), 1, self.output)
        self.assertIn(("url", "https://www.tobias-erichsen.de/software/loopmidi.html"), self.pc.calls)
        self.assertIn("winget slutade med felkod", self.output)

    def test_saying_no_to_winget(self):
        self.pc.winget = os.path.join(self.base, "winget.exe")
        self.answers = ["n"]
        self.run_setup(interactive=True)
        self.assertNotIn("install", [c[0] for c in self.pc.calls])
        self.assertIn(("url", "https://www.tobias-erichsen.de/software/loopmidi.html"), self.pc.calls)

    def test_loopmidi_without_autostart_found_where_it_runs(self):
        exe = os.path.join(self.base, "Verktyg", "loopMIDI.exe")
        write(exe)
        self.pc.reg.add("HKLM", UNINSTALL + r"\{x}", {"DisplayName": "loopMIDI"}, view=32)
        self.pc.launch(exe)
        self.run_setup()
        self.assertIn("loopMIDI är installerat: %s" % exe, self.output)
        self.assertEqual(self.pc.reg.value(RUN, "loopMIDI"), ('"%s"' % exe, REG_SZ))

    def test_skip_loopmidi(self):
        self.assertEqual(self.run_setup("--skip-loopmidi"), 0, self.output)
        self.assertIsNone(self.pc.reg.values("HKCU", PORTS))
        self.assertIn("Hoppar över loopMIDI (--skip-loopmidi)", self.output)


class DeveloperTest(SetupTest):
    def test_a_junction_to_a_checkout_is_left_alone_and_never_followed(self):
        self.standard()
        checkout = os.path.join(self.base, "fl-multiplayer", "fl", "FLSync")
        write(os.path.join(checkout, "device_FLSync.py"), "dev")
        hardware = self.settings("Hardware")
        os.makedirs(hardware)
        link = os.path.join(hardware, "FLSync")
        if not make_junction(link, checkout):
            self.skipTest("Windows made no junction here")
        self.assertTrue(setup.is_link(link))
        self.assertEqual(os.path.normcase(setup.link_target(link)), os.path.normcase(checkout))
        self.assertEqual(self.run_setup(), 0, self.output)
        self.assertTrue(setup.is_link(link))
        self.assertEqual(os.listdir(checkout), ["device_FLSync.py"])
        self.assertEqual(read(os.path.join(checkout, "device_FLSync.py")), "dev")
        self.assertIn("är en länk till %s" % checkout, self.output)
        self.assertIn("(en utvecklarinstallation), så den lämnas som den är", self.output)

        self.assertEqual(self.run_setup("--uninstall", "--yes"), 0, self.output)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(read(os.path.join(checkout, "device_FLSync.py")), "dev")  # the checkout stays
        self.assertIn("Tar bort länken %s (filerna i %s är kvar)" % (link, checkout), self.output)

    def test_is_link_and_helpers(self):
        folder = os.path.join(self.base, "mapp")
        os.makedirs(folder)
        self.assertFalse(setup.is_link(folder))
        self.assertFalse(setup.is_link(os.path.join(self.base, "finns inte")))
        self.assertEqual(setup.command_path('"C:\\Program Files (x86)\\x\\a.exe" /uninstall'),
                         "C:\\Program Files (x86)\\x\\a.exe")
        self.assertEqual(setup.command_path("C:\\Program Files (x86)\\x\\loopMIDI.exe"),
                         "C:\\Program Files (x86)\\x\\loopMIDI.exe")
        self.assertEqual(setup.command_path("C:\\x\\Data\\System\\icon.ico,0"), "C:\\x\\Data\\System\\icon.ico")
        self.assertEqual(setup.major_version("25.2.4.5242"), 25)
        self.assertEqual(setup.major_version(None, "FL Studio 2024"), 24)
        self.assertEqual(setup.major_version("", "FL Studio 21"), 21)
        self.assertIsNone(setup.major_version(None, "FL"))
        self.assertEqual([setup.fl_label(n) for n in (20, 21, 24, 26)],
                         ["FL Studio 20", "FL Studio 21", "FL Studio 2024", "FL Studio 2026"])
        self.assertEqual([setup.port_number(v) for v in ("10", " 3 ", "-1", "", "x", 300, ("12", REG_SZ))],
                         [10, 3, None, None, None, None, 12])
        self.assertTrue(setup.FL_PROCESS.match("FL64 (scaled).exe"))
        self.assertFalse(setup.FL_PROCESS.match("FLEngine_x64.exe"))


class UninstallTest(SetupTest):
    def setUp(self):
        super().setUp()
        self.exe = self.standard()
        self.mine = self.settings("Piano roll scripts", "Mitt eget.pyscript")
        write(self.mine, "mine")
        self.run_setup()
        self.pc.calls.clear()

    def test_uninstall_removes_what_setup_added(self):
        self.assertEqual(self.run_setup("--uninstall", "--yes"), 0, self.output)
        self.assertFalse(os.path.exists(self.pc.local_data_dir()))  # nothing else was kept there
        self.assertFalse(os.path.exists(self.settings("Hardware", "FLSync")))
        self.assertEqual(os.listdir(self.settings("Piano roll scripts")), ["Mitt eget.pyscript"])
        self.assertEqual(os.listdir(self.pc.desktop_dir()), [])
        self.assertEqual(os.listdir(self.pc.programs_dir()), [])
        self.assertIsNone(self.device(25, "MIDI input"))
        self.assertIsNone(self.device(25, "MIDI output"))
        self.assertEqual(self.pc.reg.subkeys("HKCU", r"Software\Image-Line\FL Studio 25\Devices\MIDI output"),
                         ["Microsoft GS Wavetable Synth", "Microsoft MIDI Mapper"])
        self.assertEqual(self.pc.reg.values("HKCU", PORTS), {"Annan port": (1, REG_DWORD)})
        # loopMIDI was restarted so the port goes now, and stays installed
        self.assertEqual([c[:2] for c in self.pc.calls if c[0] in ("kill", "start", "reg_delete_value")],
                         [("kill", "loopMIDI.exe"), ("reg_delete_value", PORTS), ("start", self.exe)])
        self.assertEqual(self.pc.ports, ["Annan port"])
        self.assertTrue(os.path.isfile(self.exe))
        self.assertIsNotNone(self.pc.reg.value(RUN, "loopMIDI"))
        self.assertIn("FLSync är borttaget.", self.output)
        self.assertIn("loopMIDI får vara kvar", self.output)

    def test_uninstall_keeps_received_projects_and_says_where(self):
        projects = os.path.join(self.pc.fl_user_dir(), "FL Studio", "Projects", "FLSync", "Beat", "Beat.flp")
        write(projects)
        write(os.path.join(self.pc.local_data_dir(), "cache", "abc"))
        self.run_setup("--uninstall", "--yes")
        self.assertTrue(os.path.isfile(projects))
        self.assertTrue(os.path.isfile(os.path.join(self.pc.local_data_dir(), "cache", "abc")))
        self.assertFalse(os.path.exists(self.app))
        self.assertIn(os.path.dirname(os.path.dirname(projects)), self.output)
        self.assertIn(self.pc.local_data_dir(), self.output)

    def test_uninstall_asks_first(self):
        before = self.snapshot()
        self.assertEqual(self.run_setup("--uninstall"), 1)  # no one to answer: no
        self.answers = ["n"]
        self.assertEqual(self.run_setup("--uninstall", interactive=True), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Avbrutet. Ingenting har ändrats.", self.output)
        self.answers = ["j"]
        self.assertEqual(self.run_setup("--uninstall", interactive=True), 0, self.output)
        self.assertFalse(os.path.exists(self.app))

    def test_uninstall_dry_run(self):
        before = self.snapshot()
        self.assertEqual(self.run_setup("--uninstall", "--dry-run"), 0, self.output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changes(), [])
        for text in ("-> FL Studio 2025: tar bort MIDI-ingången FLSync", "-> Tar bort FLSync-scriptet",
                     "-> Tar bort notscriptet", "-> Tar bort genvägen", "-> Tar bort programmet",
                     "-> Tar bort porten FLSync ur loopMIDI"):
            self.assertIn(text, self.output)

    def test_uninstall_while_fl_runs(self):
        self.pc.launch(os.path.join(self.pc.pf86, "Image-Line", "FL Studio 2025", "FL64.exe"))
        before = self.snapshot()
        self.assertEqual(self.run_setup("--uninstall", "--yes"), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("kör Installera FLSync.cmd --uninstall igen", self.output)
        # with --skip-fl-config the rest goes; loopMIDI isn't restarted under FL
        self.assertEqual(self.run_setup("--uninstall", "--yes", "--skip-fl-config"), 0, self.output)
        self.assertIsNotNone(self.device(25, "MIDI input"))
        self.assertFalse(os.path.exists(self.app))
        self.assertNotIn("kill", [c[0] for c in self.pc.calls])
        self.assertIsNone(self.pc.reg.value(PORTS, "FLSync"))
        self.assertIn("Porten finns kvar tills loopMIDI startas om", self.output)

    def test_uninstall_from_the_installed_copy(self):
        self.answers = ["j", ""]
        self.assertEqual(self.run_setup("--uninstall", interactive=True, root=self.app), 0, self.output)
        self.assertFalse(os.path.exists(self.app))
        self.assertIn(("leave", self.app), self.pc.calls)  # out of the folder before it goes
        self.assertEqual(self.asked[-1], "Tryck Enter för att stänga fönstret.")  # its .cmd is gone too

    def test_uninstalling_twice(self):
        self.run_setup("--uninstall", "--yes")
        self.pc.calls.clear()
        self.assertEqual(self.run_setup("--uninstall", "--yes"), 0, self.output)
        self.assertEqual(self.changes(), [])
        for text in ("FLSync finns inte i FL Studios MIDI-inställningar", "FLSync-scriptet är inte installerat",
                     "Notscripten för pianorullen är inte installerade", "Det finns inga genvägar till FLSync",
                     "Programmet finns inte i", "loopMIDI har ingen port som heter FLSync"):
            self.assertIn(text, self.output)


class CmdFilesTest(unittest.TestCase):
    """Installera FLSync.cmd and FLSync.cmd as cmd.exe needs them."""

    def test_plain_batch_files(self):
        for name in ("Installera FLSync.cmd", "FLSync.cmd"):
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), name)  # cmd would run the BOM as a command
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), name)  # labels need CRLF
            lines = data.decode("utf-8").split("\r\n")
            first_text = next(i for i, line in enumerate(lines) if any(ord(ch) > 127 for ch in line))
            self.assertLess(next(i for i, line in enumerate(lines) if line.startswith("chcp 65001")), first_text)
            for line in lines:  # no ( ) blocks: a path with "(x86)" in one breaks it
                code = line.strip()
                if not code.lower().startswith(("rem ", "echo ")) and not code.startswith("for "):
                    self.assertFalse(code.endswith("(") or code.startswith(")"), (name, line))
            self.assertIn('call :scan "%ProgramFiles(x86)%"', lines)
            self.assertIn("py -3 -c", data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
