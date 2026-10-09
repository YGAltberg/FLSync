"""Phase 3 additions in the relay (docs/phase3.md): the session password, FL
versions in the participant list and the pen (one structure change at a time)."""

import unittest

from harness import BridgeLink, RawClient
from test_snapshots import FP_A, FP_B, Session, WireFL

FP_C = "cccccccccccc"


class RelayTest(Session):
    def rest(self, client):
        """What the relay sent `client` before answering a sync sent now."""
        client.send({"t": "sync"})
        messages = []
        while True:
            msg = client.recv()
            if msg["t"] == "state":
                return messages
            messages.append(msg)


class PasswordTest(RelayTest):
    relay_options = {"password": "hemligt"}

    def test_only_those_with_the_password_get_in(self):
        for hello in ({}, {"pw": "fel"}, {"pw": 5}):
            with RawClient(self.port, "Utanför", "fl", **hello) as outsider:
                self.assertEqual((outsider.welcome["t"], outsider.welcome["code"]), ("error", "password"))
                self.assertIn("lösenord", outsider.welcome["msg"])
        a = self.raw("A", "fl", pw="hemligt", fp=FP_A)
        self.assertEqual(a.welcome["t"], "welcome")
        with RawClient(self.port, "Filer", "files", pw="fel") as files:
            self.assertEqual(files.welcome["code"], "password")
        with RawClient(self.port, "Filer", "files", pw="hemligt") as files:
            self.assertEqual(files.welcome["t"], "welcome")

    def test_a_bridge_brings_the_password_on_both_connections(self):
        fl = WireFL("Anna", FP_A)
        link = BridgeLink(self.loop, self.port, fl, password="hemligt")
        self.links.append(link)
        self.fls.append(fl)
        fl.hello()
        self.wait_for(lambda: link.bridge.my_id is not None)
        files = self.loop.run(link.bridge._files())
        self.assertGreater(files.max_blob, 0)


class PeersTest(RelayTest):
    def test_participants_show_their_fl_version(self):
        a = self.raw("A", "fl", fp=FP_A, flv="25.2.4.4960")
        b = self.raw("B", "fl", fp=FP_A, flv="26.1.7.5512")
        self.assertEqual(b.welcome["peers"], [{"id": a.id, "name": "A", "app": "fl", "flv": "25.2.4.4960"}])
        joined = a.recv_kind("peer_join")
        self.assertEqual(joined["flv"], "26.1.7.5512")
        m = self.raw("M", flv="x" * 100)  # too long: left out
        self.assertNotIn("flv", a.recv_kind("peer_join"))
        self.assertEqual(len(m.welcome["peers"]), 2)


class PenTest(RelayTest):
    def test_one_structure_change_at_a_time(self):
        a, b, c = (self.raw(n, "fl", fp=FP_A) for n in "ABC")
        self.assertIsNone(c.welcome["pen"])
        for client in (a, b, c):
            self.rest(client)
        a.send({"t": "fp", "fp": FP_B})  # A adds a channel: A takes the pen
        pen = {"t": "pen", "id": a.id, "name": "A"}
        self.assertEqual([self.rest(x) for x in (a, b, c)], [[pen], [pen], [pen]])
        b.send({"t": "fp", "fp": FP_C})  # B starts changing the structure too
        self.assertEqual(self.rest(b), [dict(pen, busy=True)])
        self.assertEqual((self.rest(a), self.rest(c)), ([], []))
        late = self.raw("Sen")
        self.assertEqual(late.welcome["pen"], {"id": a.id, "name": "A"})
        a.send({"t": "fp", "fp": FP_A})  # A undoes it: the pen is free again
        free = {"t": "pen", "id": None}
        self.assertEqual([m for m in self.rest(a) if m["t"] == "pen"], [free])
        self.assertEqual([m for m in self.rest(b) if m["t"] == "pen"], [free])

    def test_opening_another_project_takes_no_pen(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        self.rest(a)
        b.send({"t": "fp", "fp": FP_B, "load": True})  # B opened another project (or a version, a plugin missing)
        self.assertEqual((self.rest(a), self.rest(b)), ([], []))
        self.assertIsNone(self.relay.pen)
        b.send({"t": "fp", "fp": FP_C})  # now B edits: that takes it
        self.assertEqual(self.rest(a), [{"t": "pen", "id": b.id, "name": "B"}])

    def test_sharing_or_leaving_frees_the_pen(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        a.send({"t": "fp", "fp": FP_B})
        self.rest(a)
        self.rest(b)
        files = self.files()
        from test_snapshots import make_project, sha256
        project = make_project(title="ny")
        files.upload(project)
        a.send({"t": "snap_put", "name": "Beat", "fp": FP_B, "flp": sha256(project), "size": len(project),
                "samples": [], "plugins": []})
        self.assertEqual([m["t"] for m in self.rest(a)], ["snapshot", "reshare", "pen"])
        self.assertEqual(self.rest(b)[-1], {"t": "pen", "id": None})
        # B now has the old structure: changing it again (or still) doesn't take
        # the pen from nobody... it takes it, as the first one to differ now.
        b.send({"t": "fp", "fp": FP_C})
        self.assertEqual(self.rest(b), [{"t": "pen", "id": b.id, "name": "B"}])
        b.close()
        self.clients.remove(b)
        self.assertEqual(self.rest(a), [{"t": "pen", "id": b.id, "name": "B"},  # B had taken it
                                        {"t": "peer_leave", "id": b.id, "name": "B", "app": "fl"},
                                        {"t": "pen", "id": None}])
        self.assertIsNone(self.relay.pen)


class MonitorTest(RelayTest):
    relay_options = {"password": "hemligt"}

    def test_the_monitor_needs_the_password_and_shows_versions_and_the_pen(self):
        from test_monitor_cli import MONITOR, Output, start
        wrong = start(MONITOR, "--port", str(self.port))
        out, _ = wrong.communicate(timeout=10)
        self.assertIn("Fel lösenord", out)
        self.assertNotEqual(wrong.returncode, 0)
        a = self.raw("A", "fl", pw="hemligt", fp=FP_A, flv="25.2.4.4960")
        monitor = start(MONITOR, "--port", str(self.port), "--password", "hemligt")
        output = Output(monitor)
        try:
            self.assertIsNotNone(output.wait_for("I sessionen: A (fl, FL 25.2.4.4960)"))
            b = self.raw("B", "fl", pw="hemligt", fp=FP_A)
            self.assertIsNotNone(output.wait_for("+ B anslöt (fl)"))
            b.send({"t": "fp", "fp": FP_B})
            self.assertIsNotNone(output.wait_for("* B ändrar projektets struktur"))
            b.send({"t": "fp", "fp": FP_A})
            self.assertIsNotNone(output.wait_for("* Ingen ändrar projektets struktur längre"))
            monitor.stdin.write("quit\n")
            monitor.stdin.flush()
            monitor.wait(10)
        finally:
            if monitor.poll() is None:
                monitor.kill()
            monitor.stdout.close()
            monitor.stdin.close()


if __name__ == "__main__":
    unittest.main()
