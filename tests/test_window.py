"""FLSync as a program of its own: the installer in FLSync's window
(app/installer.py on setup/setup.py), the window's life (closing it ends
FLSync, a second start brings it forward), FLSync.exe and the zip.

Nothing here opens a real window or touches the real computer: the launcher
gets a fake system with a fake window, the installer test_setup's fake
computer in a temporary folder.

    python -m unittest discover -s tests -p "test_window.py" -v
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from fake_fl import ROOT
import test_setup as ts
from test_launcher import FakeSystem, LauncherTest

sys.path.insert(0, os.path.join(ROOT, "app"))
import flsync  # noqa: E402
import installer  # noqa: E402
import winsys  # noqa: E402

EXE = os.path.join(ROOT, "FLSync.exe")


class FakeWindow:
    """The browser process that shows FLSync's window."""

    def __init__(self, pid, url, profile):
        self.pid, self.url, self.profile = pid, url, profile
        self.code = None
        self.closed_by_flsync = False

    def poll(self):
        return self.code


class WindowSystem(FakeSystem):
    """test_launcher's fake computer, with Edge (unless browser=False) for FLSync's window."""

    def __init__(self, browser=True):
        super().__init__()
        self.browser = browser
        self.windows = []
        self.focused = []
        self.can_focus = True
        self.messages = []

    def open_window(self, url, profile):
        if not self.browser:
            return None
        window = FakeWindow(4321 + len(self.windows), url, profile)
        self.windows.append(window)
        return window

    def close_window(self, process):
        process.closed_by_flsync = True
        process.code = 0

    def focus_window(self, pid=None):
        self.focused.append(pid)
        return self.can_focus and any(w.code is None for w in self.windows)

    def message(self, text):
        self.messages.append(text)


def setup_action(launcher, action, **extra):
    """The installer's buttons on the page: POST /api/setup {action, ...}."""
    return launcher.request("POST", "/api/setup", dict(extra, action=action))


def wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# --- the installer in the window --------------------------------------------------------------

class InstallerWindowTest(ts.SetupTest):
    def package(self):
        """The download: FLSync.exe and the rest."""
        ts.write(os.path.join(self.src, "FLSync.exe"), "MZ")
        return installer.SetupRun

    def window_run(self, kind="install", dry=False):
        self.package()
        return installer.SetupRun(kind, self.src, setup_module=ts.setup, system=self.pc, dry_run=dry)

    def done(self, run):
        self.assertTrue(wait_until(lambda: run.state == "done"), "\n".join(run.view()["lines"]))
        return run.view()

    def test_installing_in_the_window(self):
        self.standard()
        run = self.window_run()
        self.assertEqual(run.look(), {"present": False, "same": False, "fl_running": False, "here": False})
        self.assertEqual(run.view()["state"], "ready")
        self.assertTrue(run.begin())
        self.assertFalse(run.begin())  # once
        view = self.done(run)
        self.assertEqual(view["code"], 0, "\n".join(view["lines"]))
        self.assertIn("Klart! FLSync är installerat.", view["lines"])
        self.assertIn("     FLSync öppnas i ett eget fönster.", view["lines"])
        self.assertTrue(any(line.startswith("Steg 5 av 5") for line in view["lines"]))
        # The shortcuts start FLSync.exe: FLSync's window, the installer and its removal.
        desktop = json.loads(ts.read(os.path.join(self.pc.desktop_dir(), "FLSync.lnk")))
        self.assertEqual((desktop["target"], desktop["arguments"]), (os.path.join(self.app, "FLSync.exe"), ""))
        programs = self.pc.programs_dir()
        repair = json.loads(ts.read(os.path.join(programs, "Reparera FLSync.lnk")))
        uninstall = json.loads(ts.read(os.path.join(programs, "Avinstallera FLSync.lnk")))
        self.assertEqual((repair["target"], repair["arguments"]), (os.path.join(self.app, "FLSync.exe"), "--repair"))
        self.assertEqual(uninstall["arguments"], "--uninstall")
        self.assertTrue(os.path.isfile(os.path.join(self.app, "FLSync.exe")))
        self.assertEqual(run.look()["same"], True)  # the window's next first view: this version is there

    def test_an_update_replaces_the_console_files(self):
        self.standard()
        self.assertEqual(self.run_setup(), 0, self.output)  # an older FLSync, started with FLSync.cmd
        self.assertTrue(os.path.isfile(os.path.join(self.app, "FLSync.cmd")))
        for name in ("FLSync.cmd", "Installera FLSync.cmd"):  # the new download has FLSync.exe instead
            os.remove(os.path.join(self.src, name))
        run = self.window_run()
        self.assertEqual(run.look()["present"], True)
        run.begin()
        self.assertEqual(self.done(run)["code"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.app, "FLSync.cmd")))
        self.assertFalse(os.path.exists(os.path.join(self.app, "Installera FLSync.cmd")))
        desktop = json.loads(ts.read(os.path.join(self.pc.desktop_dir(), "FLSync.lnk")))
        self.assertEqual(desktop["target"], os.path.join(self.app, "FLSync.exe"))

    def test_a_question_waits_for_the_window(self):
        self.package()
        self.pc.add_fl()
        self.pc.winget = os.path.join(self.base, "winget.exe")  # loopMIDI is missing; winget can get it
        run = self.window_run()
        run.begin()
        self.assertTrue(wait_until(lambda: run.state == "asking"), run.view()["lines"])
        self.assertEqual(run.view()["question"],
                         {"text": "Installera loopMIDI nu? Windows frågar om lov att ändra datorn.", "default": True})
        self.assertTrue(run.answer(True))
        view = self.done(run)
        installs = [call for call in self.pc.calls if call[0] == "install"]
        self.assertEqual(len(installs), 1, view["lines"])
        self.assertFalse(installs[0][2])  # no console for winget: its output is kept, not shown live
        self.assertIsNone(view["question"])

    def test_avbryt_while_it_waits_for_fl(self):
        self.standard()
        self.pc.launch(os.path.join(self.pc.pf86, "Image-Line", "FL Studio 2025", "FL64.exe"))
        self.pc.on_sleep = lambda: time.sleep(0.01)
        self.package()
        before = self.snapshot()
        run = self.window_run()
        self.assertTrue(run.look()["fl_running"])
        run.begin()
        self.assertTrue(wait_until(lambda: any("Väntar på att FL Studio stängs" in line
                                               for line in run.view()["lines"])))
        self.assertFalse(any("Ctrl+C" in line for line in run.view()["lines"]))  # no console to press it in
        run.cancel()
        view = self.done(run)
        self.assertEqual(view["code"], 2)
        self.assertIn("Avbrutet. Ingenting har ändrats.", view["lines"])
        self.assertEqual(self.snapshot(), before)

    def test_uninstalling_asks_first(self):
        self.standard()
        self.assertEqual(self.run_setup(), 0, self.output)
        run = self.window_run("uninstall")
        run.begin()
        self.assertTrue(wait_until(lambda: run.state == "asking"))
        self.assertEqual(run.view()["question"],
                         {"text": "Ta bort FLSync från den här datorn? (loopMIDI får vara kvar)", "default": False})
        run.answer(False)
        view = self.done(run)
        self.assertEqual(view["code"], 1)
        self.assertIn("Avbrutet. Ingenting har ändrats.", view["lines"])
        self.assertTrue(os.path.isdir(self.app))
        run = self.window_run("uninstall")
        run.begin()
        self.assertTrue(wait_until(lambda: run.state == "asking"))
        run.answer(True)
        view = self.done(run)
        self.assertEqual(view["code"], 0, view["lines"])
        self.assertFalse(os.path.exists(self.app))
        self.assertFalse(any("Tryck Enter" in line for line in view["lines"]))

    def test_a_dry_run_in_the_window_changes_nothing(self):
        self.standard()
        self.package()
        before = self.snapshot()
        run = self.window_run(dry=True)
        run.begin()
        view = self.done(run)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Provkörningen är klar. Ingenting har ändrats.", view["lines"])
        self.assertFalse(any("Installera FLSync.cmd" in line for line in view["lines"]))


# --- the installer first, then FLSync ----------------------------------------------------------

class InstallerFirstTest(LauncherTest):
    def fake_install(self):
        """A download and a computer (test_setup's fake one) to install it on."""
        base = tempfile.mkdtemp(prefix="flsync-window-setup-")
        self.addCleanup(shutil.rmtree, base, True)
        pc = ts.FakeSystem(base)
        src = os.path.join(base, "Hämtade filer", "FLSync")
        for name, text in ts.SOURCE.items():
            ts.write(os.path.join(src, name), text)
        ts.write(os.path.join(src, "FLSync.exe"), "MZ")
        pc.add_fl()
        pc.add_loopmidi(autostart=False)
        run = installer.SetupRun("install", src, setup_module=ts.setup, system=pc)
        run.look()
        return run

    def test_the_installer_comes_first_then_flsync(self):
        anna = self.launcher("Anna", start=False, setup=self.fake_install())
        self.loop.run(anna.app.start())
        status = anna.status()
        self.assertEqual((status["setup"]["kind"], status["setup"]["state"]), ("install", "ready"))
        self.assertEqual(status["summary"]["text"], "Installation")
        self.assertNotIn("mode", status)
        self.assertIsNone(anna.app.bridge)  # nothing of FLSync runs yet
        self.assertEqual(anna.post("host")[0], 400)  # not while installing
        self.assertEqual(setup_action(anna, "launch")[0], 200)  # (its own version is there: start it)
        anna2 = self.launcher("Bo", start=False, setup=self.fake_install())
        self.loop.run(anna2.app.start())
        self.assertEqual(setup_action(anna2, "begin"), (200, {"ok": True}))
        self.wait_for(lambda: anna2.status()["setup"]["state"] == "done", timeout=10)
        self.assertEqual(anna2.status()["setup"]["code"], 0, anna2.status()["setup"]["lines"])
        self.assertEqual(setup_action(anna2, "launch"), (200, {"ok": True}))
        status = anna2.status()
        self.assertNotIn("setup", status)
        self.assertIsNone(status["mode"])
        self.wait_for(lambda: anna2.app.bridge is not None and anna2.fl.to_bridge is not None,
                      why="FLSync startade inte efter installationen")

    def test_a_question_and_its_answer_go_through_the_page(self):
        run = self.fake_install()
        run.system.on_sleep = lambda: time.sleep(0.01)
        run.system.launch(os.path.join(run.system.pf86, "Image-Line", "FL Studio 2025", "FL64.exe"))
        anna = self.launcher("Anna", start=False, setup=run)
        self.loop.run(anna.app.start())
        setup_action(anna, "begin")
        self.wait_for(lambda: any("Väntar på att FL Studio stängs" in line
                                  for line in anna.status()["setup"]["lines"]))
        self.assertEqual(setup_action(anna, "cancel"), (200, {"ok": True}))
        self.wait_for(lambda: anna.status()["setup"]["state"] == "done")
        self.assertEqual(anna.status()["setup"]["code"], 2)
        self.assertEqual(setup_action(anna, "answer", yes=True)[0], 400)  # no question waits


# --- FLSync's window ----------------------------------------------------------------------------

class WindowTest(LauncherTest):
    def start(self, system=None, **options):
        system = system or WindowSystem()
        anna = self.launcher("Anna", start=False, system=system, window=True, **options)
        running = asyncio.run_coroutine_threadsafe(anna.app.run(open_browser=True), self.loop.loop)
        self.wait_for(lambda: anna.app.url is not None and (system.windows or system.browsed))
        return anna, system, running

    def has_been_open(self, anna):
        """As if the window had been open a while (not handed over at once)."""
        anna.call(lambda: setattr(anna.app, "window_opened_at", anna.app.window_opened_at - flsync.WINDOW_HANDOFF))

    def bye(self, anna, token=None):
        """What the page sends as its window closes (navigator.sendBeacon)."""
        return anna.request("POST", "/api/bye?t=%s" % (token or anna.app.token), token=False,
                            headers={"Content-Type": "text/plain;charset=UTF-8"})

    def test_flsync_opens_its_window_and_ends_with_it(self):
        anna, system, running = self.start()
        window = system.windows[0]
        self.assertEqual(window.url, anna.app.url)
        self.assertEqual(window.profile, os.path.join(anna.app.data_dir, "window"))
        self.assertEqual(system.browsed, [])
        self.assertEqual(flsync.read_instance(anna.app.data_dir)["window"], window.pid)
        self.has_been_open(anna)
        window.code = 0  # the user closes the window
        running.result(15)
        self.assertTrue(any("FLSync-fönstret stängdes" in line["text"] for line in anna.app.lines))
        self.assertFalse(os.path.exists(flsync.instance_path(anna.app.data_dir)))

    def test_ending_flsync_closes_its_window(self):
        anna, system, running = self.start()
        self.assertEqual(anna.post("quit"), (200, {"ok": True}))
        running.result(15)
        self.assertTrue(system.windows[0].closed_by_flsync)

    def test_a_window_handed_to_a_running_browser_ends_flsync_through_the_page(self):
        anna, system, running = self.start()
        system.windows[0].code = 0  # the browser gave it to one that ran already, and ended at once
        self.settle(1.0)
        self.assertFalse(running.done())
        self.assertEqual(self.bye(anna), (200, {"ok": True}))
        running.result(15)

    def test_a_reload_is_no_goodbye(self):
        anna, system, running = self.start()
        self.assertEqual(self.bye(anna)[0], 200)
        anna.status()  # the reloaded page asks again at once
        self.settle(flsync.BYE_GRACE + 1)
        self.assertFalse(running.done())
        anna.post("quit")
        running.result(15)

    def test_a_goodbye_needs_the_token(self):
        anna, system, running = self.start()
        self.assertEqual(self.bye(anna, token="fel")[0], 403)
        self.assertEqual(anna.request("GET", "/api/bye?t=%s" % anna.app.token, token=False)[0], 403)
        self.settle(flsync.BYE_GRACE + 1)
        self.assertFalse(running.done())
        anna.post("quit")
        running.result(15)

    def test_without_edge_or_chrome_the_browser_shows_it(self):
        anna, system, running = self.start(WindowSystem(browser=False))
        self.assertEqual(system.browsed, [anna.app.url])
        anna.post("quit")
        running.result(15)

    @mock.patch.object(flsync, "WINDOW_HANDOFF", 60.0)  # the window that goes below was handed over, not closed
    def test_a_second_start_brings_the_window_forward(self):
        anna, system, running = self.start()
        args = flsync.parse_args(["--data-dir", anna.app.data_dir])
        with mock.patch.object(flsync.winsys, "claim", return_value=False):
            self.assertEqual(flsync.run_program(args, anna.app.data_dir, system), 0)
        self.assertEqual(system.focused, [system.windows[0].pid])
        self.assertEqual(len(system.windows), 1)
        # Its window was handed to another browser and is gone: FLSync opens a new one.
        system.windows[0].code = 0
        system.can_focus = False
        with mock.patch.object(flsync.winsys, "claim", return_value=False):
            self.assertEqual(flsync.run_program(args, anna.app.data_dir, system), 0)
        self.wait_for(lambda: len(system.windows) == 2)
        anna.post("quit")
        running.result(15)

    def test_installing_ends_the_flsync_that_runs(self):
        anna, system, running = self.start()
        self.assertEqual(flsync.ask_running(anna.app.data_dir, "quit", {}), {"ok": True})
        running.result(15)


class WindowPartsTest(unittest.TestCase):
    def test_an_app_window_of_its_own(self):
        args = winsys.window_args("msedge.exe", "http://127.0.0.1:5000/?t=abc", r"C:\Data\window")
        self.assertEqual(args[0], "msedge.exe")
        self.assertIn("--app=http://127.0.0.1:5000/?t=abc", args)
        self.assertIn(r"--user-data-dir=C:\Data\window", args)
        for flag in ("--no-first-run", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"):
            self.assertIn(flag, args)

    def test_the_page_says_goodbye_and_has_the_installer(self):
        with open(os.path.join(ROOT, "app", "ui.html"), encoding="utf-8") as f:
            page = f.read()
        for part in ('id="setup"', "sendBeacon", '"/api/bye?t="', 'translate="no"', "window.close()"):
            self.assertIn(part, page)

    def test_without_a_console_the_output_goes_to_a_log(self):
        data = tempfile.mkdtemp(prefix="flsync-quiet-")
        self.addCleanup(shutil.rmtree, data, True)
        code = ("import sys; sys.path.insert(0, %r); import flsync; sys.stdout = sys.stderr = None; "
                "print(flsync.without_console(%r)); print('hej till loggen'); sys.stdout.flush()"
                % (os.path.join(ROOT, "app"), data))
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(os.path.join(data, flsync.LOG_FILE), encoding="utf-8") as f:
            self.assertEqual(f.read().split(), ["True", "hej", "till", "loggen"])

    @unittest.skipUnless(sys.platform == "win32", "Windows")
    def test_the_lock_is_free_again_once_its_holder_ends(self):
        name = "Local\\FLSync-test-" + uuid.uuid4().hex
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; sys.path.insert(0, %r); import winsys; "
                                   "print(winsys.claim(%r), flush=True); time.sleep(60)"
             % (os.path.join(ROOT, "app"), name)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "True")
            self.assertIs(winsys.claim(name), False)  # held: and our try mustn't keep it alive
        finally:
            holder.kill()
            holder.wait(10)
            holder.stdout.close()
        self.assertIs(winsys.claim(name), True)


# --- FLSync.exe and the zip ----------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "win32" and os.path.isfile(EXE), "FLSync.exe finns inte (scripts/build_exe.py)")
class ExeTest(unittest.TestCase):
    def check(self, *args, exe=EXE):
        done = subprocess.run([exe, "--launcher-check"] + list(args), capture_output=True, text=True, timeout=60)
        if "Hittar ingen Python" in done.stdout:
            self.skipTest("ingen FL Studio-Python på den här datorn")
        return done.returncode, done.stdout.strip()

    def test_it_starts_flsync_with_fls_python_and_installs_from_the_download(self):
        code, line = self.check("--name", "Anna Svensson")
        self.assertEqual(code, 0, line)
        self.assertRegex(line, r'(?i)^"?[^"]*\\pythonw?\.exe"? ')
        self.assertIn(os.path.join("app", "flsync.py"), line)
        self.assertIn(" --install ", line)  # the checkout isn't the installed copy: install from it
        self.assertTrue(line.endswith('--name "Anna Svensson"'), line)

    def test_the_start_menu_repairs_and_uninstalls(self):
        for flag in ("--repair", "--uninstall"):
            code, line = self.check(flag)
            self.assertEqual(code, 0, line)
            self.assertTrue(line.endswith(" " + flag), line)
            self.assertNotIn("--install", line)

    def test_without_the_rest_of_flsync_it_says_so(self):
        alone = tempfile.mkdtemp(prefix="flsync-exe-")
        self.addCleanup(shutil.rmtree, alone, True)
        shutil.copy2(EXE, alone)
        code, text = self.check(exe=os.path.join(alone, "FLSync.exe"))
        self.assertEqual(code, 1)
        self.assertIn("Hittar inte resten av FLSync", text)


@unittest.skipUnless(os.path.isfile(EXE), "FLSync.exe finns inte (scripts/build_exe.py)")
class ZipTest(unittest.TestCase):
    def test_the_zip_has_the_program_and_no_console_files(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import make_zip
        names = [name for _, name in make_zip.entries()]
        for name in ("FLSync/FLSync.exe", "FLSync/README.md", "FLSync/LICENSE", "FLSync/CHANGELOG.md",
                     "FLSync/app/installer.py", "FLSync/app/version.py", "FLSync/app/ui.html", "FLSync/setup/setup.py",
                     "FLSync/bridge/flpwatch.py"):
            self.assertIn(name, names)
        self.assertEqual([name for name in names if name.endswith(".cmd")], [])


if __name__ == "__main__":
    unittest.main()
