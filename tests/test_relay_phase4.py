"""Phase 4 in the relay (docs/phase4.md): a share must build on the newest
version (`parent`) and says whose versions it includes (`merged`), keepalive
(`ping`) and dropping clients gone quiet, and versions kept on disk across a
restart (`persist`). A real relay on a background loop, raw protocol clients,
and store folders of the tests' own.

    python -m unittest discover -s tests -p "test_relay*.py" -v
"""

import itertools
import json
import os
import shutil
import socket
import tempfile
import time
import unittest

from harness import FilesClient, LoopThread, RawClient, RelayServer, relay_mod, sha256

FP_A, FP_B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
_projects = itertools.count(1)


def rest(client):
    """Everything the relay sent `client` before answering a sync sent now.
    The relay answers in order, so this also shows what did *not* arrive."""
    client.send({"t": "sync"})
    messages = []
    while True:
        msg = client.recv()
        if msg["t"] == "state":
            return messages
        messages.append(msg)


def closed(client):
    """True once the relay has closed the connection (after anything still on the way)."""
    try:
        while client.file.readline():
            pass
        return True
    except socket.timeout:
        return False
    except OSError:  # a reset is closed all the same
        return True


def without(msg, *keys):
    return {k: v for k, v in msg.items() if k not in keys}


def outdated(v):
    return {"t": "error", "code": "outdated", "v": v, "msg": "en nyare version har delats, slå ihop med den först"}


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class RelayCase(unittest.TestCase):
    """A relay and raw clients. `store` is a folder of the test's own (never the
    real %LOCALAPPDATA%); with `persist` it is the relay's store, and restart()
    starts a new relay on it, as a restarted host does."""

    relay_options = {}
    idle_timeout = None

    def setUp(self):
        self.loop = LoopThread()
        self.loop_errors = []  # e.g. an exception in a connection handler
        self.loop.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.tmp = tempfile.mkdtemp(prefix="flsync-test-")
        self.store = os.path.join(self.tmp, "relay-store")
        self.logs = []
        self.addCleanup(setattr, relay_mod, "log", relay_mod.log)
        relay_mod.log = self.logs.append
        self.clients = []
        self.server = None
        self.start()

    def tearDown(self):
        if self.server is not None:
            self.server.stop()  # before the raw clients go: the relay never writes to a closed socket
        for client in self.clients:
            client.close()
        self.loop.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual([c.get("message") for c in self.loop_errors], [])

    def start(self, **options):
        self.options = dict(self.relay_options, **options)
        if self.options.get("persist"):
            self.options.setdefault("store", self.store)
        self.server = RelayServer(self.loop, **self.options)
        if self.idle_timeout is not None:
            self.server.relay.idle_timeout = self.idle_timeout  # before start(): the watcher's pace follows it
        self.port = self.server.start()
        self.files_client = None

    def restart(self):
        """Stop the relay, and start a new one with the same options."""
        self.server.stop()
        for client in self.clients:
            client.close()
        self.clients = []
        self.start(**self.options)

    @property
    def relay(self):
        return self.server.relay

    def raw(self, name, app="monitor", **hello):
        client = RawClient(self.port, name, app, **hello)
        self.clients.append(client)
        return client

    def files(self):
        if self.files_client is None:
            self.files_client = FilesClient(self.port)
            self.clients.append(self.files_client)
        return self.files_client

    def upload(self, data):
        self.assertEqual(self.files().upload(data), {"t": "blob_ok", "h": sha256(data)})
        return sha256(data)

    def share(self, sharer, fp=FP_A, samples=(), **extra):
        """Upload a new project and its `samples` (bytes), then send snap_put as
        `sharer` with the `extra` fields (parent, merged). Returns the project's sha256."""
        project = b"FLhd version %d" % next(_projects)
        sharer.send(dict({"t": "snap_put", "name": "Beat", "fp": fp, "flp": self.upload(project),
                          "size": len(project), "plugins": [],
                          "samples": [{"h": self.upload(data), "size": len(data), "path": "C:\\Samples\\%d.wav" % i}
                                      for i, data in enumerate(samples)]}, **extra))
        return sha256(project)

    def names(self):
        """Who is in the session, read on the relay's own loop."""
        async def names():
            return sorted(c.name for c in self.relay.clients.values())

        return self.loop.run(names())


# --- parent and merged --------------------------------------------------------------

class ParentTest(RelayCase):
    def test_a_share_must_build_on_the_newest_version(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        rest(b)
        self.share(a, parent=0)  # nothing shared yet: 0 is the newest
        first = [m for m in rest(a) if m["t"] == "snapshot"]
        self.assertEqual([(m["v"], m["parent"]) for m in first], [(1, 0)])
        self.assertEqual(rest(b), first)  # the parent goes along, like any field of the version
        self.share(b, parent=1)
        self.assertEqual([(m["v"], m["by"]) for m in rest(b)], [(2, "B")])
        self.assertEqual([m["v"] for m in rest(a)], [2])

        self.share(a, fp=FP_B, parent=1)  # A saved on top of version 1, but B's version 2 came since
        self.share(a, parent=0)  # 0 says there's no version yet: there is
        self.share(a, parent=3)  # and one that doesn't exist yet is no better
        self.assertEqual(rest(a), [outdated(2)] * 3)
        self.assertEqual(rest(b), [])  # nothing changed, and nobody heard of it
        self.assertEqual((self.relay.version, self.relay.snapshot["v"], self.relay.fp), (2, 2, FP_A))

        self.share(a, parent=2)  # merged with version 2: taken
        self.share(a)  # no parent: taken as ever, the newest wins
        three, four = rest(a)
        self.assertEqual((three["v"], three["parent"], four["v"]), (3, 2, 4))
        self.assertNotIn("parent", four)
        self.assertEqual(rest(b), [three, four])

    def test_with_no_version_yet_only_0_fits(self):
        a = self.raw("A", "fl", fp=FP_A)
        self.share(a, parent=1)
        self.assertEqual(rest(a), [outdated(0)])
        self.assertEqual((self.relay.version, self.relay.snapshot), (0, None))  # it took no number
        self.share(a, parent=0)
        self.assertEqual([m["v"] for m in rest(a)], [1])

    def test_a_version_dropped_for_another_structure_counts_as_none(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        self.share(a, fp=FP_B, parent=0)  # A shares a new structure; B hasn't opened it ...
        rest(a)
        a.close()  # ... and A leaves: B is alone, and the session takes B's structure again
        self.clients.remove(a)
        self.assertEqual([b.recv()["t"] for _ in range(3)], ["snapshot", "peer_leave", "reshare"])
        self.assertIsNone(self.relay.snapshot)  # version 1 isn't handed to newcomers ...
        self.share(b, parent=1)  # ... so nobody is told to merge with it
        self.assertEqual(rest(b), [outdated(0)])
        self.share(b, parent=0)
        self.assertEqual([(m["v"], m["parent"]) for m in rest(b)], [(2, 0)])  # the numbers go on

    def test_parent_must_be_a_version_number(self):
        a = self.raw("A", "fl", fp=FP_A)
        for parent in ("1", 1.0, 1.5, True, -1, [1], {"v": 1}):
            with self.subTest(parent=parent):
                self.share(a, parent=parent)
                reply, = rest(a)
                self.assertEqual((reply["t"], "code" in reply), ("error", False))
                self.assertIn("parent ska vara ett versionsnummer", reply["msg"])
        self.assertIsNone(self.relay.snapshot)
        self.share(a, parent=None)  # null is no parent at all, as for a batch's fp
        snapshot, = rest(a)
        self.assertEqual(snapshot["v"], 1)
        self.assertNotIn("parent", snapshot)

    def test_a_broken_share_hears_what_is_wrong_first(self):
        a = self.raw("A", "fl", fp=FP_A)
        self.share(a)
        rest(a)
        a.send({"t": "snap_put", "name": "Beat", "fp": FP_A, "flp": sha256(b"never uploaded"), "size": 14,
                "samples": [], "plugins": [], "parent": 0})  # stale as well as broken
        reply, = rest(a)
        self.assertEqual((reply["t"], "code" in reply), ("error", False))
        self.assertIn("projektfilen", reply["msg"])


class MergedTest(RelayCase):
    def test_whose_versions_a_share_includes_goes_with_it(self):
        a, m = self.raw("A", "fl", fp=FP_A), self.raw("M")
        rest(a)
        self.share(a, parent=0, merged=["Bo", "Cia"])
        snapshot, = rest(m)
        self.assertEqual((snapshot["v"], snapshot["parent"], snapshot["merged"]), (1, 0, ["Bo", "Cia"]))
        self.assertEqual(rest(a), [snapshot])
        self.assertEqual(self.relay.snapshot, without(snapshot, "t"))
        self.assertEqual(self.raw("Sen").welcome["snapshot"], without(snapshot, "t"))
        self.assertIn("A delade version 1 av 'Beat' (0 samples, 0.0 MB), sammanslagen med Bo, Cia", self.logs)
        self.assertEqual([m["t"] for m in rest(a)], ["peer_join"])

        for merged in (["x" * 40] * 10, []):  # as many and as long as they get, or none
            self.share(a, merged=merged)
            self.assertEqual(rest(a)[0]["merged"], merged)
        self.share(a, merged=None)  # null: not given
        self.assertNotIn("merged", rest(a)[0])

    def test_merged_must_be_a_short_list_of_names(self):
        a = self.raw("A", "fl", fp=FP_A)
        for merged in ("Bo", ["Bo", ""], ["x" * 41], ["Bo"] * 11, ["Bo", 5], [["Bo"]], {"Bo": 1}):
            with self.subTest(merged=merged):
                self.share(a, merged=merged)
                reply, = rest(a)
                self.assertEqual(reply["t"], "error")
                self.assertIn("merged ska vara en lista", reply["msg"])
        self.assertIsNone(self.relay.snapshot)


# --- keepalive ------------------------------------------------------------------------

class KeepaliveTest(RelayCase):
    idle_timeout = 0.3

    def test_a_ping_gets_a_pong_for_the_sender_only(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B")
        rest(a)
        a.send({"t": "ping"})
        a.send({"t": "ping", "extra": [1]})  # whatever else it says
        self.assertEqual(rest(a), [{"t": "pong"}, {"t": "pong"}])
        self.assertEqual(rest(b), [])
        files = self.files()
        files.send({"t": "ping"})
        self.assertEqual(files.recv(), {"t": "pong"})

    def test_only_a_client_that_pinged_and_went_quiet_is_dropped(self):
        quiet = self.raw("Tyst", "fl", fp=FP_A)        # a bridge whose computer went to sleep
        pinging = self.raw("Pingar", "fl", fp=FP_A)    # a bridge keeping its connection alive
        talking = self.raw("Pratar", "fl", fp=FP_A)    # pinged once; after that anything counts
        old = self.raw("Gammal", "fl", fp=FP_A)        # an older bridge: never pings
        m = self.raw("M")
        files = self.files()                            # pings, then only listens (a long download)
        for client in (quiet, pinging, talking, old, m):
            rest(client)
        for client in (quiet, pinging, talking, files):
            client.send({"t": "ping"})
        self.assertEqual(files.recv(), {"t": "pong"})
        until = time.monotonic() + 3 * self.idle_timeout
        while time.monotonic() < until:
            pinging.send({"t": "ping"})
            talking.send({"t": "sync"})
            time.sleep(self.idle_timeout / 6)

        self.assertTrue(closed(quiet))
        leave = {"t": "peer_leave", "id": quiet.id, "name": "Tyst", "app": "fl"}
        self.assertEqual(rest(m), [leave])
        self.assertEqual(rest(old), [leave])
        self.assertEqual(self.names(), ["Gammal", "M", "Pingar", "Pratar"])
        self.assertEqual([line for line in self.logs if "hörts av" in line],
                         ["Tyst har inte hörts av på länge och kopplas från"])
        files.send({"t": "ping"})
        self.assertEqual(files.recv(), {"t": "pong"})


# --- versions on disk -------------------------------------------------------------------

class PersistTest(RelayCase):
    relay_options = {"persist": True}

    def saved(self):
        with open(os.path.join(self.store, "session.json"), encoding="utf-8") as f:
            return json.load(f)

    def blobs(self):
        """What the store folder holds."""
        return set(os.listdir(self.store))

    def test_persist_needs_a_folder_of_its_own(self):
        for store in (None, ""):
            with self.subTest(store=store):
                with self.assertRaises(ValueError):
                    relay_mod.Relay(persist=True, store=store)
        relay_mod.Relay(persist=True, store=os.path.join(self.tmp, "annan"))

    def test_the_versions_survive_a_restart(self):
        a = self.raw("A", "fl", fp=FP_A)
        kick, snare = b"RIFF kick", b"RIFF snare"
        first = self.share(a, samples=[kick])
        second = self.share(a, samples=[kick, snare], parent=1, merged=["Bo"])
        v1, v2 = [without(m, "t") for m in rest(a)]
        self.assertEqual(self.saved(), {"format": 1, "version": 2, "snapshot": v2, "history": [v1]})
        orphan = self.upload(b"uploaded for a share that never came")
        others = {"anteckningar.txt", sha256(b"x") + ".bak", sha256(b"a folder")}  # not blobs: never touched
        write(os.path.join(self.store, "anteckningar.txt"), b"mine")
        write(os.path.join(self.store, sha256(b"x") + ".bak"), b"mine too")
        os.mkdir(os.path.join(self.store, sha256(b"a folder")))
        write(os.path.join(self.store, "session-x.part"), b"{")  # a write the relay never finished

        self.restart()
        self.assertEqual((self.relay.version, self.relay.snapshot, self.relay.fp), (2, v2, FP_A))
        self.assertEqual((self.relay.state, self.relay.notes, self.relay.seq), ({}, {}, 0))
        self.assertEqual(self.blobs(), {first, second, sha256(kick), sha256(snare), "session.json"} | others)
        m = self.raw("M")
        self.assertEqual((m.welcome["snapshot"], m.welcome["fp"]), (v2, FP_A))
        b = self.raw("B", "fl", fp=FP_A)
        self.assertEqual((b.welcome["seed"], b.welcome["snapshot"]), (True, v2))  # values: the first FL's
        self.assertNotIn(v2["from"], (m.id, b.id))  # nobody new takes version 2 for their own

        self.share(b, parent=1)
        self.assertEqual(rest(b), [outdated(2)])
        self.share(b, parent=2)
        v3, = [without(msg, "t") for msg in rest(b)]
        self.assertEqual(v3["v"], 3)  # the numbers go on
        self.assertEqual(self.saved(), {"format": 1, "version": 3, "snapshot": v3, "history": [v2, v1]})

    def test_the_twenty_versions_before_the_newest_are_kept(self):
        a = self.raw("A", "fl", fp=FP_A)
        loop = b"RIFF loop"  # in every version
        projects, takes = [], []
        for n in range(1, 24):
            take = b"RIFF take %d" % n
            projects.append(self.share(a, samples=[loop, take], parent=n - 1))
            takes.append(sha256(take))
        self.assertEqual([m["v"] for m in rest(a)], list(range(1, 24)))
        saved = self.saved()
        self.assertEqual((saved["version"], saved["snapshot"]["v"]), (23, 23))
        self.assertEqual([s["v"] for s in saved["history"]], list(range(22, 2, -1)))

        self.restart()
        self.assertEqual([s["v"] for s in self.relay.kept], list(range(23, 2, -1)))
        self.assertEqual(self.blobs(), set(projects[2:]) | set(takes[2:]) | {sha256(loop), "session.json"})

    def test_a_version_whose_files_are_gone_is_dropped(self):
        a = self.raw("A", "fl", fp=FP_A)
        kick = b"RIFF kick"
        first = self.share(a, samples=[kick])
        second = self.share(a, fp=FP_B, samples=[b"RIFF snare"])
        self.share(a, fp=FP_B, samples=[b"RIFF hat"], parent=2)
        v1 = without(rest(a)[0], "t")
        os.remove(os.path.join(self.store, sha256(b"RIFF hat")))  # version 3 lost a sample ...
        with open(os.path.join(self.store, second), "ab") as f:  # ... and version 2's project changed
            f.write(b"!")

        self.restart()
        self.assertEqual((self.relay.snapshot, self.relay.fp, self.relay.version), (v1, FP_A, 3))
        self.assertEqual(self.blobs(), {first, sha256(kick), "session.json"})
        self.assertIn("2 av de sparade versionerna saknar filer eller är trasiga och hoppas över", self.logs)
        b = self.raw("B", "fl", fp=FP_A)
        self.assertEqual(b.welcome["snapshot"], v1)
        self.share(b, parent=1)
        self.assertEqual([m["v"] for m in rest(b)], [4])

    def test_a_version_dropped_for_another_structure_is_still_kept(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        project = self.share(a, fp=FP_B)
        v1 = without(b.recv(), "t")
        a.close()  # B is left alone with the old structure: the session takes it again
        self.clients.remove(a)
        self.assertEqual([b.recv()["t"] for _ in range(2)], ["peer_leave", "reshare"])
        self.assertIsNone(self.relay.snapshot)
        self.assertEqual(self.saved()["snapshot"], v1)  # still the last version shared

        self.restart()
        self.assertEqual((self.relay.snapshot, self.relay.fp), (v1, FP_B))
        b = self.raw("B", "fl", fp=FP_A)  # back alone with the old structure: it takes over again
        self.assertEqual((b.welcome["seed"], b.welcome["fp"], b.welcome["snapshot"]), (True, FP_A, None))
        self.restart()
        self.assertIn(project, self.blobs())  # one of the last versions: its files stay

    def test_a_broken_session_file_starts_empty_and_keeps_the_files(self):
        a = self.raw("A", "fl", fp=FP_A)
        project = self.share(a)
        rest(a)
        orphan = self.upload(b"uploaded for a share that never came")
        path = os.path.join(self.store, "session.json")
        for content in (b"inte json", b"\xff\xfe{}", b"[]", b"[" * 100000 + b"]" * 100000,
                        b'{"format": 2, "version": 1, "history": []}', b'{"format": 1, "version": "1", "history": []}',
                        b'{"format": true, "version": 1, "history": []}', b'{"format": 1, "version": 1}'):
            with self.subTest(content=content[:40]):
                write(path, content)
                self.restart()
                self.assertEqual((self.relay.version, self.relay.snapshot, self.relay.fp), (0, None, None))
                # Which files are still needed can't be told, so they all stay.
                self.assertTrue({project, orphan} <= self.blobs())
        self.assertTrue(any("gick inte att läsa" in line for line in self.logs))
        b = self.raw("B", "fl", fp=FP_A)
        self.share(b)
        self.assertEqual([m["v"] for m in rest(b)], [1])
        self.assertEqual(self.saved()["version"], 1)  # a sound file again

    def test_a_session_file_that_cant_be_read_or_written(self):
        self.server.stop()
        os.mkdir(os.path.join(self.store, "session.json"))
        self.restart()
        self.assertEqual((self.relay.version, self.relay.snapshot), (0, None))
        a = self.raw("A", "fl", fp=FP_A)
        self.share(a)
        self.assertEqual([m["v"] for m in rest(a)], [1])  # the session goes on all the same
        self.assertTrue(any(line.startswith("Kunde inte spara versionerna") for line in self.logs))
        self.assertEqual([n for n in os.listdir(self.store) if n.endswith(".part")], [])

    def test_a_new_store_keeps_only_what_isnt_a_blob(self):
        self.server.stop()
        write(os.path.join(self.store, sha256(b"old")), b"old")
        write(os.path.join(self.store, "anteckningar.txt"), b"mine")
        self.restart()
        self.assertEqual(self.blobs(), {"anteckningar.txt"})
        self.assertEqual((self.relay.version, self.relay.snapshot), (0, None))


class NoPersistTest(RelayCase):
    def test_without_persist_nothing_is_kept_or_removed(self):
        self.server.stop()
        self.start(store=self.store)  # a folder of its own, as relay.py --store gives it
        a = self.raw("A", "fl", fp=FP_A)
        project = self.share(a)
        rest(a)
        orphan = self.upload(b"uploaded for a share that never came")
        self.restart()
        self.assertEqual((self.relay.version, self.relay.snapshot), (0, None))  # as before phase 4
        self.assertEqual(set(os.listdir(self.store)), {project, orphan})  # no session.json, nothing removed


if __name__ == "__main__":
    unittest.main()
