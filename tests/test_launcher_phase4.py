"""Phase 4 in the launcher (docs/phase4.md, The launcher): FLSync goes back into
its session when it starts again, the host's versions stay on disk, "Dela
mitt projekt", and how the last save went.

    python -m unittest discover -s tests -p "test_launcher_phase4.py" -v
"""

import asyncio
import os
import time
import unittest
from unittest import mock

from harness import free_port
from test_launcher import FL26, LauncherTest, flsync, hello, winsys
from test_snapshots import make_project, save


class ResumeTest(LauncherTest):
    def run_app(self, launcher):
        return asyncio.run_coroutine_threadsafe(launcher.app.run(open_browser=False), self.loop.loop)

    def again(self, launcher, name):
        """`launcher` ends (its window closed); FLSync starts again in the same folder."""
        launcher.stop()
        self.launchers.remove(launcher)
        again = self.launcher(name, start=False)
        running = self.run_app(again)
        self.addCleanup(lambda: running.done() or again.app.request_quit())
        self.wait_for(lambda: again.app.url is not None, why="FLSync startade inte")
        return again

    def test_a_host_that_starts_again_hosts_again(self):
        anna = self.launcher("Anna")
        code = self.host(anna, port=free_port())
        again = self.again(anna, "Anna")
        self.wait_for(lambda: again.app.mode == "host" and again.app.invite == code, timeout=10,
                      why="sessionen startades inte igen")
        self.assertTrue(any("Startar sessionen igen" in line["text"] for line in again.app.lines))

    def test_a_guest_that_starts_again_joins_again(self):
        anna = self.launcher("Anna")
        code = self.host(anna, port=free_port())
        bo = self.launcher("Bo", fp=anna.fl.fp)
        self.join(bo, code)
        again = self.again(bo, "Bo")
        self.wait_for(lambda: again.app.mode == "join", timeout=10, why="gästen gick inte med igen")
        hello(again.fl, FL26)
        self.wait_for(lambda: again.app.bridge.in_session(), timeout=10)

    def test_leaving_forgets_the_session(self):
        anna = self.launcher("Anna")
        code = self.host(anna, port=free_port())
        bo = self.launcher("Bo", fp=anna.fl.fp)
        self.join(bo, code)
        self.assertEqual(bo.post("leave"), (200, {"ok": True}))
        again = self.again(bo, "Bo")
        self.settle(0.5)
        self.assertIsNone(again.app.mode)

    def test_a_guest_whose_host_is_away_keeps_trying(self):
        saved = flsync.RESUME_RETRY
        flsync.RESUME_RETRY = 0.2
        self.addCleanup(lambda: setattr(flsync, "RESUME_RETRY", saved))
        anna = self.launcher("Anna")
        port = free_port()
        code = self.host(anna, port=port)
        bo = self.launcher("Bo", fp=anna.fl.fp)
        self.join(bo, code)
        anna.stop()  # the host's FLSync is closed ...
        self.launchers.remove(anna)
        again = self.again(bo, "Bo")  # ... and the guest's starts again meanwhile
        self.wait_for(lambda: again.status()["resuming"] is not None, timeout=10)
        status = again.status()
        self.assertEqual((status["mode"], status["error"], status["busy"]), (None, None, None))  # quietly
        host = self.launcher("Anna", start=False)  # the host comes back
        running = self.run_app(host)
        self.addCleanup(lambda: running.done() or host.app.request_quit())
        self.wait_for(lambda: again.app.mode == "join", timeout=15, why="gästen kom inte med när värden kom tillbaka")


    def test_stopping_the_retries_or_choosing_something_else(self):
        saved = flsync.RESUME_RETRY
        flsync.RESUME_RETRY = 0.2
        self.addCleanup(lambda: setattr(flsync, "RESUME_RETRY", saved))
        anna = self.launcher("Anna")
        code = self.host(anna, port=free_port())
        bo = self.launcher("Bo", fp=anna.fl.fp)
        self.join(bo, code)
        anna.stop()
        self.launchers.remove(anna)
        again = self.again(bo, "Bo")
        self.wait_for(lambda: again.status()["resuming"] is not None, timeout=10)
        self.assertEqual(again.post("leave"), (200, {"ok": True}))  # "Sluta försöka"
        self.wait_for(lambda: again.status()["resuming"] is None)
        self.assertIsNone(again.app.settings.get("resume"))
        cy = self.again(again, "Bo")  # started again: nothing to go back into
        self.settle(0.5)
        self.assertIsNone(cy.status()["resuming"])
        status, answer = cy.post("host", port=free_port())  # and choosing works as usual
        self.assertEqual((status, answer.get("ok")), (200, True), answer)


class HostVersionsTest(LauncherTest):
    def test_the_hosts_versions_outlive_a_restart(self):
        project = save(os.path.join(self.tmp, "Anna", "Beat.flp"), make_project(title="v1"))
        old = time.time() - 3600
        os.utime(project, (old, old))
        anna = self.launcher("Anna", project=project)
        port = free_port()
        code = self.host(anna, port=port)
        save(project, make_project(title="v1, sparad"))
        self.wait_for(lambda: anna.app.relay.snapshot is not None)
        snapshot = anna.app.relay.snapshot
        anna.stop()
        self.launchers.remove(anna)
        again = self.launcher("Anna", project=project)
        self.assertEqual(self.host(again, port=port), code)
        self.assertEqual(again.app.relay.snapshot["flp"], snapshot["flp"])  # kept in <data>\relay-store
        bo = self.launcher("Bo", fp=again.fl.fp)
        self.join(bo, code)
        self.wait_for(lambda: bo.app.bridge.received.get(snapshot["flp"]), timeout=10)


class ShareMineTest(LauncherTest):
    def test_a_project_that_isnt_the_sessions_can_be_shared_on_purpose(self):
        old = time.time() - 3600
        beat = save(os.path.join(self.tmp, "Anna", "Beat.flp"), make_project(title="Annas"))
        os.utime(beat, (old, old))
        anna = self.launcher("Anna", project=beat)
        code = self.host(anna, port=free_port())
        save(beat, make_project(title="Annas, sparad"))
        self.wait_for(lambda: anna.app.relay.version == 1)
        mine = save(os.path.join(self.tmp, "Bo", "Eget.flp"), make_project(title="Bos"))
        os.utime(mine, (old, old))
        bo = self.launcher("Bo", fp=anna.fl.fp, project=mine)
        self.join(bo, code)
        self.wait_for(lambda: bo.app.bridge.identifying is not None and bo.app.bridge.identifying.done())
        save(mine, make_project(title="Bos, sparad"))
        self.wait_for(lambda: bo.status()["sync"] is not None, timeout=10)
        status = bo.status()
        self.assertTrue(status["sync"]["share"], status["sync"])
        self.assertIn("hör inte till sessionen", status["sync"]["text"])
        self.assertEqual(anna.app.relay.version, 1)  # not shared
        self.assertEqual(bo.post("share"), (200, {"ok": True}))
        self.wait_for(lambda: anna.app.relay.version == 2, timeout=10)
        self.assertEqual(anna.app.relay.snapshot["name"], "Eget")
        self.wait_for(lambda: bo.status()["saved"] is not None)
        status = bo.status()
        self.assertIsNone(status["sync"])
        self.assertIn("delad", status["saved"]["text"])

    def test_nothing_to_share(self):
        anna = self.launcher("Anna")
        self.host(anna, port=free_port())
        status, answer = anna.post("share")
        self.assertEqual((status, answer["error"]["code"]), (400, "share"))


class FlVersionTest(LauncherTest):
    """Which FL runs, and an FL version installed after FLSync (FL 2026)."""

    def program(self, name, version=None, age=120):
        return winsys.Process(name, 4242 if name != winsys.LOOPMIDI else 7, time.time() - age,
                              r"C:\FL\FL64.exe" if version else None, version)

    def test_an_fl_installed_after_flsync_is_set_up_once_it_is_closed(self):
        anna = self.launcher("Anna")
        anna.fl.to_bridge = None  # this FL isn't set up for FLSync: it says nothing on the port
        loopmidi = self.program(winsys.LOOPMIDI, age=3600)
        anna.system.needs = {25: False, 26: True}
        anna.system.procs = [loopmidi, self.program("fl64.exe", "26.1.7.5653")]
        self.wait_for(lambda: [c for c in anna.status()["checks"] if c["id"] == "fl" and c["state"] == "bad"],
                      why="ingen varning om FL 2026")
        check = [c for c in anna.status()["checks"] if c["id"] == "fl"][0]
        self.assertEqual(check["title"], "FL Studio 2026 är inte inställt för FLSync än")
        self.assertIn("Stäng FL Studio 2026", check["fix"][0])
        self.assertEqual(anna.system.configured, [])  # never while it runs: FL writes its settings when it closes
        self.assertEqual(anna.system.settings_asked, 26)  # the settings looked at are the running FL's
        anna.system.procs = [loopmidi]  # FL 2026 is closed
        self.wait_for(lambda: anna.system.configured == [26], why="FL 2026 ställdes inte in")
        self.wait_for(lambda: any("FL Studio 2026 är nu inställt för FLSync" in line["text"]
                                  for line in anna.app.lines))
        self.settle(0.3)
        self.assertEqual(anna.system.configured, [26])  # once

    def test_nothing_changes_while_an_fl_of_unknown_version_runs(self):
        anna = self.launcher("Anna")
        anna.system.needs = {24: True}
        anna.system.procs = [self.program("fl64.exe")]  # its version can't be read
        self.settle(0.5)
        self.assertEqual(anna.system.configured, [])

    def test_other_fl_versions_are_set_up_while_one_runs(self):
        anna = self.launcher("Anna")
        anna.system.needs = {24: True, 25: False}
        anna.system.procs = [self.program("fl64.exe", "25.2.4.5242")]
        self.wait_for(lambda: anna.system.configured == [24])
        self.wait_for(lambda: any("FL Studio 2024 är nu inställt för FLSync" in line["text"]  # said right after
                                  for line in anna.app.lines))


class SavedTextTest(unittest.TestCase):
    def test_every_outcome_reads_well(self):
        cases = {
            ("shared", None): "Din version är delad med de andra (version 4).",
            ("merged", "now"): "Din version är sammanslagen med Kims ändringar (version 4). FL öppnar den gemensamma "
                               "versionen. 2 delar hade ni båda ändrat: där behölls din ändring.",
            ("pulled", "later"): "Du hade inget nytt att dela, så du får Kims version (version 4). Den öppnas i FL "
                                 "när du stoppar uppspelningen.",
        }
        for (outcome, opening), text in cases.items():
            msg = {"outcome": outcome, "v": 4, "with": ["Kim"], "open": opening,
                   "conflicts": 2 if outcome == "merged" else 0}
            self.assertEqual(flsync.saved_text(msg), text)
        self.assertIn("Versioner", flsync.saved_text({"outcome": "replaced", "with": ["Kim"], "why": "olika FL"}))
        self.assertIn("Dela mitt projekt", flsync.saved_text({"outcome": "unrelated"}))
        self.assertEqual(flsync.saved_text({"outcome": "failed", "why": "nätet"}), "Projektet kunde inte delas: nätet.")
        self.assertEqual(flsync.names_text(["Kim", "Bo", "Cy"]), "Kim, Bo och Cy")


if __name__ == "__main__":
    unittest.main()

class AutoSyncWindowTest(LauncherTest):
    """docs/phase5.md in the window: who runs which FLSync, FL saving and
    getting versions in by itself, and a newer FLSync."""

    def test_everyone_shows_their_flsync_and_another_one_is_warned_about(self):
        anna = self.launcher("Anna")
        code = self.host(anna, flv=FL26)
        bo = self.launcher("Bo")
        self.join(bo, code)
        self.wait_for(lambda: len(anna.status()["people"]) == 2)
        self.assertEqual({p["name"]: p["ver"] for p in anna.status()["people"]},
                         {"Anna": flsync.VERSION, "Bo": flsync.VERSION})
        self.assertIsNone(anna.status()["people_note"])
        self.raw(anna.app.relay_port, "Cy", anna.app.relay.password, ver="0.10.0")  # an FL with an older FLSync
        self.wait_for(lambda: "Cy har FLSync 0.10.0" in (anna.status()["people_note"] or ""))
        self.wait_for(lambda: "Cy har FLSync 0.10.0" in (bo.status()["people_note"] or ""))

    def test_saving_and_getting_in_by_itself_can_be_turned_off(self):
        anna = self.launcher("Anna")
        self.host(anna, flv=FL26)
        self.assertEqual(anna.status()["autosync"]["on"], True)
        self.assertIn("någon mer", anna.status()["autosync"]["text"])  # alone: nobody to share with yet
        self.assertEqual(anna.post("autosync", on=False), (200, {"ok": True}))
        autosync = anna.status()["autosync"]
        self.assertEqual((autosync["on"], autosync["text"][:3]), (False, "Av:"))
        anna.stop()
        self.launchers.remove(anna)
        again = self.launcher("Anna")  # the same folder: the choice stays
        self.assertFalse(again.app.bridge.auto_sync)

    def test_an_older_fl_is_told_to_press_ctrl_s(self):
        anna = self.launcher("Anna")
        self.host(anna, flv="26.1.6.5500")
        self.assertIn("kan inte spara av sig själv", anna.status()["autosync"]["text"])

    def test_a_newer_flsync_is_offered(self):
        newer = {"tag_name": "v99.0.0", "html_url": flsync.REPO + "/releases/tag/v99.0.0"}
        with mock.patch.object(flsync, "fetch_json", lambda url, timeout: newer):
            anna = self.launcher("Anna", update_check=True)
            self.wait_for(lambda: anna.status()["update"] is not None)
        self.assertIn("FLSync 99.0.0 finns", anna.status()["update"]["text"])
        self.assertEqual(anna.post("update"), (200, {"ok": True}))
        self.assertEqual(anna.system.browsed, [flsync.REPO + "/releases/tag/v99.0.0"])

    def test_the_same_flsync_or_a_page_elsewhere_isnt_offered(self):
        same = {"tag_name": "v" + flsync.VERSION}
        with mock.patch.object(flsync, "fetch_json", lambda url, timeout: same):
            anna = self.launcher("Anna", update_check=True)
            self.settle(0.5)
        self.assertIsNone(anna.status()["update"])
        elsewhere = {"tag_name": "v99.0.0", "html_url": "https://example.com/FLSync.exe"}
        with mock.patch.object(flsync, "fetch_json", lambda url, timeout: elsewhere):
            bo = self.launcher("Bo", update_check=True)
            self.wait_for(lambda: bo.status()["update"] is not None)
        bo.post("update")
        self.assertEqual(bo.system.browsed, [flsync.RELEASES_PAGE])  # only ever FLSync's own page
