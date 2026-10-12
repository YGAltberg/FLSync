"""Phase 2 (docs/protocol_v2.md): structure fingerprints, project snapshots and
piano-roll notes.

  * the relay on its own, with raw protocol clients (session and files connections);
  * real bridges with temporary folders, each driven by a minimal FL that only
    speaks the MIDI wire format, against a real relay: sharing on save,
    receiving into a project folder, names from the network, limits, damage;
  * the piano roll scripts in fl/Piano roll scripts/ passing notes through two bridges;
  * simulated FL Studios (the real FLSync script) end to end.

FL Studio's bundled templates are read (never written) when FL 2025 is installed.

    python -m unittest discover -s tests -p "test_snapshots.py" -v
"""

import base64
import itertools
import json
import os
import queue
import shutil
import sys
import tempfile
import time
import types
import unittest

from fake_fl import ROOT, FakeFL
from harness import (PROTO, BridgeLink, FilesClient, LoopThread, RawClient, RelayServer, bridge_mod, pump,
                     relay_mod, sha256)

import flp  # noqa: E402  (the harness put bridge/ on the path)
import wire  # noqa: E402

TEMPLATES = os.path.join(os.environ.get("FLSYNC_FL_DIR", r"C:\Program Files (x86)\Image-Line\FL Studio 2025"),
                         "Data", "Templates")
BASIC = os.path.join(TEMPLATES, "Minimal", "Basic", "Basic.flp")
SCRIPTS = os.path.join(ROOT, "fl", "Piano roll scripts")
FP_A, FP_B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
FP_C, FP_D, FP_E = "cccccccccccc", "dddddddddddd", "eeeeeeeeeeee"
KICK = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Kicks\Kick.wav"
TAKE = r"%FLStudioUserData%\FL Studio\Audio\Recorded\take.wav"
GONE = r"C:\Nowhere\gone.wav"
NOTE = [0, 48, 60, 0.5, 0.5, 0.5, 0, 0.5, 0.5, 0, 0, 0, 0, 0]
NOTE2 = [96, 24, 67, 0.8, 0.25, 0.5, 3, 0.5, 0.5, -20, 1, 0, 1, 2]


# --- projects --------------------------------------------------------------------------

def varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def event(eid, value):
    if isinstance(value, str):
        value = (value + "\0").encode("utf-16-le")  # text, as FL 11.5 and later store it
    return bytes([eid]) + varint(len(value)) + value


def make_project(samples=(), plugins=(), title="", data_folder=None, padding=0, version="25.2.4.5242",
                 channels=(), effects=()):
    """A small .flp like FL 2025 writes: version, title, plugins, sample paths.
    `channels` are (name, plugin, state) and `effects` (plugin, state) for
    the mixer; a state is the plugin's bytes, or None."""
    body = event(199, (version + "\0").encode("ascii")) + event(194, title)
    for plugin in plugins:
        body += event(201, plugin)
    for path in samples:
        body += event(196, path)
    for index, (name, plugin, state) in enumerate(channels):
        body += bytes([64]) + index.to_bytes(2, "little") + event(201, plugin) + event(203, name)
        body += event(213, state) if state is not None else b""
    if effects:
        body += event(236, bytes(12))  # the mixer's first insert
        for slot, (plugin, state) in enumerate(effects):
            body += event(201, plugin) + (event(213, state) if state is not None else b"")
            body += bytes([98]) + slot.to_bytes(2, "little")
    if data_folder is not None:
        body += event(202, data_folder)
    if padding:
        body += event(235, bytes(padding))  # some state nobody reads
    return (b"FLhd" + (6).to_bytes(4, "little") + bytes(2) + len(samples).to_bytes(2, "little")
            + (96).to_bytes(2, "little") + b"FLdt" + len(body).to_bytes(4, "little") + body)


def state8(*paths):
    """A plugin state that keeps sample paths as DirectWave does: records of an
    id, a length and UTF-8 text."""
    return b"".join((500 + i).to_bytes(4, "little") + len(p.encode()).to_bytes(8, "little") + p.encode()
                    for i, p in enumerate(paths))


def state16(*paths):
    """... and as FPC does: UTF-16 text."""
    return b"".join(bytes(30) + (p + "\0").encode("utf-16-le") for p in paths)


_saves = itertools.count(1)


def save(path, data):
    """Write `path` the way FL saving it looks: new content, a new modification time."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    stamp = 1_700_000_000_000_000_000 + next(_saves) * 1_000_000_000  # never the same twice
    os.utime(path, ns=(stamp, stamp))
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def files_under(folder):
    return sorted(os.path.relpath(os.path.join(where, name), folder)
                  for where, _, names in os.walk(folder) for name in names)


def without(msg, *keys):
    return {k: v for k, v in msg.items() if k not in keys}


# --- a minimal FL ----------------------------------------------------------------------

class WireFL:
    """Just enough of FL for one bridge: it sends messages in the MIDI wire
    format and reads what the bridge sends back. No FL script runs."""

    def __init__(self, name, fp=FP_A, flv=None):
        self.name, self.fp, self.flv = name, fp, flv
        self.from_bridge = queue.SimpleQueue()
        self.to_bridge = None
        self.encoder = wire.Encoder(wire.FROM_FL)
        self.decoder = wire.Decoder(wire.TO_FL)
        self.messages = []
        self.next_heartbeat = 0.0

    def send(self, msg):
        for part in self.encoder.message(msg):
            self.to_bridge(part)

    def hello(self):
        hello = {"t": "hello", "proto": PROTO, "app": "fl", "name": self.name, "api": 40, "fp": self.fp}
        if self.flv:
            hello["flv"] = self.flv
        self.send(hello)

    def report(self, fp):
        """FL's script says its structure changed (a project load or an edit)."""
        self.fp = fp
        self.send({"t": "fp", "fp": fp})

    def idle(self):
        now = time.monotonic()
        if self.to_bridge is not None and now >= self.next_heartbeat:
            self.to_bridge(self.encoder.heartbeat())  # FL is still here
            self.next_heartbeat = now + 0.5
        while True:
            try:
                part = self.from_bridge.get_nowait()
            except queue.Empty:
                return
            msg = self.decoder.feed(*wire.unpack(part))
            if isinstance(msg, dict):
                self.messages.append(msg)

    def got(self, kind):
        return [m for m in self.messages if m.get("t") == kind]


class Session(unittest.TestCase):
    """A real relay and people who join it, each with a WireFL and a bridge."""

    relay_options = {}

    def setUp(self):
        self.loop = LoopThread()
        self.loop_errors = []
        self.loop.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.server = RelayServer(self.loop, **self.relay_options)
        self.port = self.server.start()
        self.tmp = tempfile.mkdtemp(prefix="flsync-test-")
        self.links, self.fls, self.clients = [], [], []

    def tearDown(self):
        for link in self.links:
            link.stop()
        self.server.stop()  # before the raw clients go: the relay never writes to a closed socket
        for client in self.clients:
            client.close()
        self.loop.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual([c.get("message") for c in self.loop_errors], [])
        for link in self.links:
            self.assertEqual([line for line in link.logs if line.startswith("fel")], [])

    @property
    def relay(self):
        return self.server.relay

    def person(self, name, fp=FP_A, project=None, join=True, flv=None, **options):
        """Someone's FL and bridge. `project` is the file FL's recent-files list
        names, `link.title` FL's window title (never the real FL's)."""
        fl = WireFL(name, fp, flv)
        options.setdefault("fl_title", lambda: link.title)
        link = BridgeLink(self.loop, self.port, fl, mru=lambda: link.mru, **options)
        link.mru, link.title = project, None
        self.links.append(link)
        self.fls.append(fl)
        if project:
            self.wait_for(lambda: link.bridge.watched == os.path.abspath(project))
        if join:
            fl.hello()
            self.wait_for(lambda: link.bridge.my_id is not None)
        return fl, link

    def raw(self, name, app="monitor", **hello):
        client = RawClient(self.port, name, app, **hello)
        self.clients.append(client)
        return client

    def files(self):
        client = FilesClient(self.port)
        self.clients.append(client)
        return client

    def wait_for(self, condition, timeout=5.0, why="tog för lång tid"):
        self.assertTrue(pump(self.fls, condition, timeout), why)

    def settle(self, seconds=0.3):
        pump(self.fls, lambda: False, seconds)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def notices(self, link, path=None):
        """The snapshot notices a bridge sent its FL (only those with a path, if path=True)."""
        found = [m for m in link.sent if m.get("t") == "snapshot"]
        return [m for m in found if "path" in m] if path else found

    def received(self, link, count=1, timeout=8.0):
        """Wait until the bridge has told FL where `count` versions are, and return the newest notice."""
        self.wait_for(lambda: len(self.notices(link, path=True)) >= count, timeout,
                      "versionen kom aldrig fram:\n" + "\n".join(link.logs))
        return self.notices(link, path=True)[-1]

    def assertShort(self, link):
        """Nothing big ever goes to FL: no lists of samples, plugins or notes, no blobs."""
        for msg in link.sent:
            self.assertFalse({"samples", "plugins", "flp", "notes", "d"} & set(msg), msg)
            if msg.get("t") == "welcome" and msg.get("snapshot"):
                self.assertEqual(set(msg["snapshot"]) - {"flv", "how", "merged"}, {"v", "name", "fp", "by", "from"})
            self.assertLess(len(wire.to_ascii_payload(msg)), 4000, msg.get("t"))


# --- the relay ---------------------------------------------------------------------------

class RelayStructureTest(Session):
    def rest(self, client):
        """What the relay sent `client` before answering a sync sent now."""
        client.send({"t": "sync"})
        messages = []
        while True:
            msg = client.recv()
            if msg["t"] == "state":
                return messages
            messages.append(msg)

    def put(self, sharer, name="Beat", fp=FP_A, samples=(), plugins=(), **extra):
        """Upload a project and its samples, then send snap_put as `sharer`
        (with the optional fields in `extra`)."""
        files = self.files()
        project = make_project(title=name + str(len(self.clients)))
        refs = []
        for i, data in enumerate((project,) + tuple(samples)):
            self.assertEqual(files.upload(data), {"t": "blob_ok", "h": sha256(data)})
            if i:
                refs.append({"h": sha256(data), "size": len(data), "path": "C:\\s\\%d.wav" % i})
        sharer.send(dict({"t": "snap_put", "name": name, "fp": fp, "flp": sha256(project), "size": len(project),
                          "samples": refs, "plugins": list(plugins)}, **extra))
        return project, refs

    def test_the_first_fl_to_share_sets_the_structure(self):
        m = self.raw("M")
        self.assertIsNone(m.welcome["fp"])  # no FL yet
        a = self.raw("A", "fl", fp=FP_A)
        self.assertEqual((a.welcome["seed"], a.welcome["fp"]), (True, FP_A))
        b = self.raw("B", "fl", fp=FP_B)  # another structure, but A is here: A's stays
        self.assertEqual((b.welcome["seed"], b.welcome["fp"]), (False, FP_A))
        self.assertEqual(self.relay.fp, FP_A)

    def test_only_keys_of_the_part_that_differs_are_refused(self):
        # FL's fingerprint is the channels' 6 digits, then the mixer's: B has
        # other channels than the session, but the same mixer.
        fp_b = "bbbbbb" + FP_A[6:]
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=fp_b)
        for client in (a, b):
            self.rest(client)
        b.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.1], ["c/1/v", 0.2], ["s/tempo", 120]], "fp": fp_b})
        self.assertEqual(self.rest(b), [{"t": "ack", "l": 1, "s": 2, "stale": ["c/1/v"]}])
        self.assertEqual(self.rest(a), [{"t": "sets", "from": b.id, "kv": [["m/1/v", 0.1], ["s/tempo", 120]],
                                         "s": 2}])
        b.send({"t": "sets", "l": 2, "kv": [["c/2/v", 0.3], ["g/1/0/1", True]], "fp": fp_b})
        self.assertEqual(self.rest(b), [{"t": "ack", "l": 2, "s": 2, "stale": True}])
        self.assertEqual(self.relay.state, {"m/1/v": 0.1, "s/tempo": 120})

    def test_a_new_structure_keeps_the_values_of_the_part_that_stayed(self):
        a = self.raw("A", "fl", fp=FP_A)
        a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.1], ["c/1/v", 0.2], ["s/tempo", 120]], "fp": FP_A})
        self.rest(a)
        a.send({"t": "fp", "fp": "bbbbbb" + FP_A[6:]})  # alone: its new channels become the session's
        self.assertEqual(self.rest(a), [{"t": "reshare"}])
        self.assertEqual(self.relay.state, {"m/1/v": 0.1, "s/tempo": 120})
        a.send({"t": "fp", "fp": FP_B})  # now its mixer changed too
        self.assertEqual(self.rest(a), [{"t": "reshare"}])
        self.assertEqual(self.relay.state, {"s/tempo": 120})
        a.send({"t": "sets", "l": 2, "kv": [["m/1/v", 0.4]], "fp": FP_B})
        self.rest(a)
        a.send({"t": "fp", "fp": "x"})  # a fingerprint that isn't FL's two parts differs everywhere
        self.assertEqual(self.rest(a), [{"t": "reshare"}])
        self.assertEqual(self.relay.state, {"s/tempo": 120})

    def test_another_project_starts_everything_over(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.1], ["s/tempo", 120]], "fp": FP_A})
        for client in (a, b):
            self.rest(client)
        self.put(b, name="Annat", switch=True)  # the same structure, but another project ("Dela mitt projekt")
        snapshot, = self.rest(a)
        self.assertEqual((snapshot["t"], snapshot["switch"], snapshot["by"]), ("snapshot", True, "B"))
        self.assertEqual(self.relay.state, {})
        self.assertIn({"t": "reshare"}, self.rest(b))

    def test_a_note_goes_to_the_others(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        for client in (a, b):
            self.rest(client)
        b.send({"t": "note", "kind": "unrelated", "name": "Mitt projekt"})
        self.assertEqual(self.rest(a), [{"t": "note", "kind": "unrelated", "name": "Mitt projekt", "from": b.id,
                                         "by": "B"}])
        self.assertEqual(self.rest(b), [])
        b.send({"t": "note", "kind": "något annat", "name": "x"})
        self.assertEqual([msg["t"] for msg in self.rest(b)], ["error"])

    def test_batches_for_another_structure_are_refused(self):
        a, b, m = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_B), self.raw("M")
        for client in (a, b, m):
            self.rest(client)
        b.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.1]], "fp": FP_B})
        b.send({"t": "set", "k": "m/1/v", "v": 0.2, "l": 2, "fp": FP_B, "seed": True})
        self.assertEqual(self.rest(b), [{"t": "ack", "l": 1, "s": 0, "stale": True},
                                        {"t": "ack", "l": 2, "s": 0, "stale": True}])
        a.send({"t": "sets", "l": 1, "kv": [["m/2/v", 0.5]], "fp": FP_A})
        self.assertEqual(self.rest(a), [{"t": "ack", "l": 1, "s": 1}])
        m.send({"t": "sets", "l": 1, "kv": [["m/3/v", 0.7]]})  # no fp: always taken
        self.assertEqual(self.rest(m), [{"t": "sets", "from": a.id, "kv": [["m/2/v", 0.5]], "s": 1},
                                        {"t": "ack", "l": 1, "s": 2}])
        self.assertEqual(self.rest(b), [{"t": "sets", "from": a.id, "kv": [["m/2/v", 0.5]], "s": 1},
                                        {"t": "sets", "from": m.id, "kv": [["m/3/v", 0.7]], "s": 2}])
        self.assertEqual(self.relay.state, {"m/2/v": 0.5, "m/3/v": 0.7})
        b.send({"t": "sets", "l": 3, "kv": [], "fp": FP_B})  # a broken batch is still an error
        self.assertEqual([msg["t"] for msg in self.rest(b)], ["error"])

    def test_a_snapshot_goes_to_everyone_numbered(self):
        a, b, m = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A), self.raw("M")
        a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3]], "fp": FP_A})
        for client in (a, b, m):
            self.rest(client)
        project, refs = self.put(a, samples=[b"kick", b"snare"], plugins=["Serum"])
        expected = {"t": "snapshot", "v": 1, "name": "Beat", "fp": FP_A, "flp": sha256(project),
                    "size": len(project), "samples": refs, "plugins": ["Serum"], "by": "A", "from": a.id}
        for client in (a, b, m):  # the sharer too; the structure didn't change, so no reshare
            self.assertEqual(self.rest(client), [expected])
        self.assertEqual(self.relay.state, {"m/1/v": 0.3})
        self.put(b, name="Beat 2")
        self.assertEqual(self.rest(m)[0]["v"], 2)
        late = self.raw("Sen")
        self.assertEqual((late.welcome["snapshot"]["v"], late.welcome["snapshot"]["by"], late.welcome["fp"]),
                         (2, "B", FP_A))
        self.assertNotIn("t", late.welcome["snapshot"])

    def test_a_snapshot_must_have_its_files_on_the_relay(self):
        a = self.raw("A", "fl", fp=FP_A)
        files = self.files()
        project = make_project()
        h = sha256(project)
        good = {"t": "snap_put", "name": "Beat", "fp": FP_A, "flp": h, "size": len(project), "samples": [],
                "plugins": []}
        a.send(good)  # not uploaded yet
        self.assertEqual(files.upload(project), {"t": "blob_ok", "h": h})
        broken = [{"flp": "0" * 64}, {"size": len(project) + 1}, {"name": ""}, {"name": 5}, {"fp": None},
                  {"fp": "x" * 65}, {"samples": [{"h": sha256(b"never sent"), "size": 10, "path": "x.wav"}]},
                  {"samples": [{"h": h, "size": len(project)}]}, {"samples": "x"}, {"plugins": [1]},
                  {"plugins": None}, {"flp": "ABC"}]
        for change in broken:
            a.send(dict(good, **change))
        replies = self.rest(a)
        self.assertEqual([r["t"] for r in replies], ["error"] * (1 + len(broken)))
        self.assertTrue(all("versionen togs inte emot" in r["msg"] for r in replies))
        self.assertIsNone(self.relay.snapshot)
        a.send(good)
        self.assertEqual(self.rest(a)[0]["v"], 1)

    def test_a_snapshot_keeps_its_fl_version_and_which_samples_plugins_use(self):
        a = self.raw("A", "fl", fp=FP_A)
        files = self.files()
        project, loop = make_project(), b"RIFF loop"
        for blob in (project, loop):
            files.upload(blob)
        h = sha256(loop)
        put = {"t": "snap_put", "name": "Beat", "fp": FP_A, "flp": sha256(project), "size": len(project),
               "plugins": [], "flv": "26.1.7.5512",
               "samples": [{"h": h, "size": len(loop), "path": "C:\\Loops\\a.wav", "plugin": True},
                           {"h": h, "size": len(loop), "path": "C:\\Loops\\b.wav", "plugin": "ja"},
                           {"h": h, "size": len(loop), "path": "C:\\Loops\\c.wav", "plugin": False}]}
        a.send(put)
        snapshot = self.rest(a)[0]
        self.assertEqual(snapshot["samples"], [{"h": h, "size": len(loop), "path": "C:\\Loops\\a.wav", "plugin": True},
                                               {"h": h, "size": len(loop), "path": "C:\\Loops\\b.wav"},
                                               {"h": h, "size": len(loop), "path": "C:\\Loops\\c.wav"}])
        self.assertEqual((snapshot["flv"], self.relay.snapshot["flv"]), ("26.1.7.5512", "26.1.7.5512"))
        self.assertEqual(self.raw("Sen").welcome["snapshot"]["flv"], "26.1.7.5512")
        for flv in ("x" * 41, "", 26, None):  # not a version text: left out, the version itself is fine
            a.send(dict(put, flv=flv))
            snapshot = self.rest(a)[-1]
            self.assertEqual((snapshot["t"], "flv" in snapshot), ("snapshot", False))
        a.send(without(put, "flv"))
        self.assertNotIn("flv", self.rest(a)[-1])

    def test_a_new_structure_starts_the_values_and_notes_over(self):
        a, b, m = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A), self.raw("M")
        a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3], ["c/0/n", "Kick"]], "fp": FP_A})
        a.send({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [NOTE], "fp": FP_A})
        a.send({"t": "fp", "fp": FP_B})  # A added a channel; B is here, so the session keeps its structure
        for client in (a, b, m):
            self.rest(client)
        self.assertEqual(self.relay.fp, FP_A)
        seq = self.relay.seq
        self.put(a, fp=FP_B)  # A saves the new structure
        replies = self.rest(a)
        self.assertEqual([r["t"] for r in replies], ["snapshot", "reshare", "pen"])  # A's change is shared: the pen is free
        self.assertEqual([r["t"] for r in self.rest(b)], ["snapshot", "pen"])  # only the sharer reshares
        self.assertEqual((self.relay.fp, self.relay.state, self.relay.touched, self.relay.notes),
                         (FP_B, {}, set(), {}))
        a.send({"t": "sets", "l": 2, "kv": [["m/1/v", 0.3]], "fp": FP_B})  # A's values again
        self.assertEqual(self.rest(a), [{"t": "ack", "l": 2, "s": seq + 1}])  # the numbers go on
        b.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.9]], "fp": FP_A})  # B hasn't opened it yet
        self.assertEqual(self.rest(b), [{"t": "sets", "from": a.id, "kv": [["m/1/v", 0.3]], "s": seq + 1},
                                        {"t": "ack", "l": 1, "s": seq + 1, "stale": True}])
        late = self.raw("Sen")
        self.assertEqual((late.welcome["state"], late.welcome["fp"], late.welcome["notes"]),
                         ({"m/1/v": 0.3}, FP_B, []))

    def test_an_fl_left_alone_with_the_old_structure_takes_over(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_A)
        a.send({"t": "sets", "l": 1, "seed": True, "kv": [["m/1/v", 0.3]], "fp": FP_A})
        self.rest(a)
        self.rest(b)
        self.put(a, fp=FP_B)  # A shares a new structure; B hasn't opened it ...
        self.rest(a)
        self.assertEqual([r["t"] for r in self.rest(b)], ["snapshot"])
        self.assertEqual(self.relay.fp, FP_B)
        a.close()  # ... and A leaves: B is alone, paused for a version it never opened
        self.clients.remove(a)
        replies = [b.recv()]
        replies += self.rest(b)
        self.assertEqual([r["t"] for r in replies], ["peer_leave", "reshare"])
        self.assertEqual((self.relay.fp, self.relay.state), (FP_A, {}))
        self.assertIsNone(self.relay.snapshot)  # A's version has another structure: newcomers don't get it
        b.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.5]], "fp": FP_A})
        self.assertEqual(self.rest(b), [{"t": "ack", "l": 1, "s": self.relay.seq}])
        self.assertIsNone(self.raw("Sen").welcome["snapshot"])

    def test_alone_a_new_structure_is_taken_over(self):
        a, m = self.raw("A", "fl", fp=FP_A), self.raw("M")  # monitors don't count
        a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3]], "fp": FP_A})
        a.send({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [NOTE], "fp": FP_A})
        self.files()  # neither do files connections
        self.rest(a)
        self.rest(m)
        a.send({"t": "fp", "fp": FP_A})  # unchanged: nothing happens
        a.send({"t": "fp", "fp": FP_B})
        self.assertEqual(self.rest(a), [{"t": "reshare"}])
        self.assertEqual((self.relay.fp, self.relay.state, self.relay.notes), (FP_B, {}, {}))
        a.send({"t": "sets", "l": 2, "kv": [["m/1/v", 0.4]], "fp": FP_B})
        self.assertEqual(self.rest(a), [{"t": "ack", "l": 2, "s": 2}])
        self.assertEqual(self.rest(m), [{"t": "sets", "from": a.id, "kv": [["m/1/v", 0.4]], "s": 2}])
        a.send({"t": "fp", "fp": 5})
        self.assertEqual([r["t"] for r in self.rest(a)], ["error"])

    def test_an_fl_that_joins_alone_with_another_structure_takes_over(self):
        a = self.raw("A", "fl", fp=FP_A)
        a.send({"t": "sets", "l": 1, "seed": True, "kv": [["m/1/v", 0.3]], "fp": FP_A})
        self.rest(a)
        a.close()
        self.clients.remove(a)
        same = self.raw("A igen", "fl", fp=FP_A)  # the same structure takes the session's values
        self.assertEqual((same.welcome["seed"], same.welcome["state"]), (False, {"m/1/v": 0.3}))
        same.close()
        self.clients.remove(same)
        time.sleep(0.2)
        b = self.raw("B", "fl", fp=FP_B)  # alone, another project: it becomes the session's
        self.assertEqual((b.welcome["seed"], b.welcome["fp"], b.welcome["state"]), (True, FP_B, {}))
        b.send({"t": "sets", "l": 1, "seed": True, "kv": [["m/1/v", 0.8]], "fp": FP_B})
        self.assertEqual(self.rest(b), [{"t": "ack", "l": 1, "s": 2}])

    def test_sharing_passes_to_an_fl_with_the_sessions_structure(self):
        a, b, c = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_B), self.raw("C", "fl", fp=FP_A)
        self.assertEqual([x.welcome["seed"] for x in (a, b, c)], [True, False, False])
        for client in (b, c):
            self.rest(client)
        a.close()
        self.clients.remove(a)
        self.assertEqual(c.recv()["t"], "peer_leave")
        self.assertEqual(c.recv(), {"t": "seed"})
        self.assertEqual([r["t"] for r in self.rest(b)], ["peer_leave"])  # not B: other structure

    def test_sharing_passes_to_the_last_fl_even_with_another_structure(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_B)
        self.rest(b)
        a.close()
        self.clients.remove(a)
        self.assertEqual(b.recv()["t"], "peer_leave")
        self.assertEqual(b.recv(), {"t": "reshare"})  # alone now: its structure is the session's
        self.assertEqual(self.relay.fp, FP_B)

    # --- notes ---------------------------------------------------------------------------

    def test_notes_are_kept_per_pattern_and_channel_and_sent_to_everyone(self):
        a, m = self.raw("A", "fl", fp=FP_A), self.raw("M")
        self.rest(a)
        self.rest(m)
        a.send({"t": "notes_put", "p": 2, "c": 4, "ppq": 96, "notes": [NOTE, NOTE2], "fp": FP_A})
        first = {"t": "notes", "p": 2, "c": 4, "v": 1, "by": "A", "from": a.id, "ppq": 96, "notes": [NOTE, NOTE2]}
        self.assertEqual(self.rest(a), [first])  # the sender too
        self.assertEqual(self.rest(m), [first])
        m.send({"t": "notes_put", "p": 2, "c": 4, "ppq": 192, "notes": []})  # replaces A's; no fp is fine
        m.send({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [NOTE]})
        self.assertEqual([(r["v"], r["by"]) for r in self.rest(a)], [(2, "M"), (3, "M")])
        late = self.raw("Sen")
        self.assertEqual([without(n, "from") for n in late.welcome["notes"]],
                         [{"p": 2, "c": 4, "v": 2, "by": "M", "ppq": 192, "notes": []},
                          {"p": 1, "c": 0, "v": 3, "by": "M", "ppq": 96, "notes": [NOTE]}])

    def test_notes_for_another_structure_or_broken_ones_are_refused(self):
        a, b = self.raw("A", "fl", fp=FP_A), self.raw("B", "fl", fp=FP_B)
        self.rest(a)
        self.rest(b)
        b.send({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [NOTE], "fp": FP_B})
        stale = self.rest(b)
        self.assertEqual([(r["t"], r.get("stale")) for r in stale], [("error", True)])
        broken = [{"p": 0}, {"p": "1"}, {"c": -1}, {"ppq": 0}, {"notes": "x"}, {"notes": [NOTE[:13]]},
                  {"notes": [NOTE + [0]]}, {"notes": [["x"] * 14]}, {"notes": [[float("inf")] * 14]},
                  {"notes": [NOTE] * (relay_mod.MAX_NOTES + 1)}]
        for change in broken:
            a.send(dict({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [NOTE], "fp": FP_A}, **change))
        self.assertEqual([r["t"] for r in self.rest(a)], ["error"] * len(broken))
        self.assertEqual(self.relay.notes, {})

    def test_the_oldest_notes_go_when_too_many_are_kept(self):
        saved, relay_mod.MAX_STORED_NOTES = relay_mod.MAX_STORED_NOTES, 5
        try:
            m = self.raw("M")
            for channel in range(4):
                m.send({"t": "notes_put", "p": 1, "c": channel, "ppq": 96, "notes": [NOTE, NOTE]})
            self.rest(m)
            self.assertEqual(list(self.relay.notes), [(1, 2), (1, 3)])
        finally:
            relay_mod.MAX_STORED_NOTES = saved


class RelayFilesTest(Session):
    relay_options = {"max_blob": 3 << 20}

    def test_a_files_connection_is_not_a_peer(self):
        m = self.raw("M")
        files = self.files()
        self.assertEqual(files.welcome, {"t": "welcome", "proto": PROTO, "id": files.id, "name": "filer",
                                         "max_blob": 3 << 20})
        files.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.5]]})  # only blobs here
        self.assertEqual(files.recv()["t"], "error")
        later = self.raw("Sen")
        self.assertEqual([p["name"] for p in later.welcome["peers"]], ["M"])
        files.close()
        self.clients.remove(files)
        self.assertEqual(m.recv()["name"], "Sen")  # a peer_join for Sen, never one for files
        m.send({"t": "blob_has", "h": []})  # and no blobs on the session connection
        self.assertEqual(m.recv()["t"], "error")
        m.send({"t": "sync"})
        self.assertEqual(m.recv()["t"], "state")  # no peer_leave for the files connection either

    def test_blobs_go_up_in_chunks_and_come_down_the_same(self):
        files = self.files()
        data = os.urandom(relay_mod.CHUNK * 2 + 1000)  # three chunks
        h, other = sha256(data), sha256(b"other")
        self.assertEqual(files.missing([h, other, h]), {"t": "blob_missing", "h": [h, other]})
        self.assertEqual(files.upload(data), {"t": "blob_ok", "h": h})
        self.assertEqual(files.missing([h, other]), {"t": "blob_missing", "h": [other]})
        self.assertEqual(read(self.relay.store.path(h)), data)  # on disk, named by its sha256
        self.assertEqual(files.download(h), data)
        self.assertEqual(files.upload(data), {"t": "blob_ok", "h": h})  # again: fine, nothing changes
        self.assertEqual(files.download(sha256(b"")), {"t": "error", "h": sha256(b""),
                                                         "msg": "relayn har ingen sådan fil"})
        self.assertEqual(files.upload(b""), {"t": "blob_ok", "h": sha256(b"")})
        self.assertEqual(files.download(sha256(b"")), b"")
        self.assertEqual([n for n in os.listdir(self.relay.store.root) if n.endswith(".part")], [])

    def test_a_blob_that_isnt_what_its_name_says_is_refused(self):
        files = self.files()
        data = b"x" * (relay_mod.CHUNK + 10)
        answer = files.upload(data, h=sha256(b"something else"))
        self.assertEqual((answer["t"], answer["h"]), ("error", sha256(b"something else")))
        self.assertIn("kontrollsumman", answer["msg"])
        self.assertEqual(files.missing([sha256(b"something else")])["h"], [sha256(b"something else")])
        self.assertEqual(os.listdir(self.relay.store.root), [])

    def test_chunks_out_of_order_fail_once(self):
        files = self.files()
        data = os.urandom(relay_mod.CHUNK * 3)
        h = sha256(data)
        chunks = [base64.b64encode(data[i * relay_mod.CHUNK:(i + 1) * relay_mod.CHUNK]).decode() for i in range(3)]
        for i in (0, 2, 1):  # a chunk is missing: one error, the rest of the blob is ignored
            files.send({"t": "blob", "h": h, "i": i, "n": 3, "d": chunks[i]})
        files.send({"t": "blob_has", "h": [h]})
        answer = files.recv()
        self.assertEqual((answer["t"], answer["h"]), ("error", h))
        self.assertEqual(files.recv(), {"t": "blob_missing", "h": [h]})
        self.assertEqual(files.upload(data), {"t": "blob_ok", "h": h})  # starting over works

    def test_broken_requests_get_an_error(self):
        files = self.files()
        h = sha256(b"x")
        for msg in ({"t": "blob", "h": "nope", "i": 0, "n": 1, "d": ""},
                    {"t": "blob", "h": h, "i": 0, "n": 0, "d": ""},
                    {"t": "blob", "h": h, "i": 1, "n": 1, "d": ""},
                    {"t": "blob", "h": h, "i": 0, "n": 10 ** 6, "d": ""},
                    {"t": "blob", "h": h, "i": True, "n": 1, "d": ""},
                    {"t": "blob", "h": h, "i": 0, "n": 1, "d": "not base64!"},
                    {"t": "blob", "h": h, "i": 0, "n": 1, "d": None},
                    {"t": "blob", "h": h, "i": 0, "n": 1,
                     "d": base64.b64encode(bytes(relay_mod.CHUNK + 1)).decode()},
                    {"t": "blob_has", "h": "x"}, {"t": "blob_has", "h": ["x"]},
                    {"t": "blob_has", "h": [h] * (relay_mod.MAX_LIST + 1)},
                    {"t": "blob_get", "h": None}, {"t": "nonsense"}):
            with self.subTest(msg=str(msg)[:60]):
                files.send(msg)
                self.assertEqual(files.recv()["t"], "error")
        files.sock.sendall(b"inte json\n")
        self.assertEqual(files.recv(), {"t": "error", "msg": "ogiltig JSON"})

    def test_a_big_download_goes_at_the_readers_pace_and_the_session_goes_on(self):
        saved, relay_mod.MAX_OUTBOX = relay_mod.MAX_OUTBOX, 64 << 10  # far less than the blob
        try:
            a, b = self.raw("A"), self.raw("B")
            files = self.files()
            data = os.urandom(3 << 20)
            h = sha256(data)
            self.assertEqual(files.upload(data), {"t": "blob_ok", "h": h})
            files.send({"t": "blob_get", "h": h})
            first = files.recv()
            self.assertEqual((first["t"], first["i"], first["n"]), ("blob", 0, 6))
            time.sleep(0.3)  # a slow reader: the relay waits instead of buffering it all
            a.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.5]]})
            self.assertEqual(b.recv_kind("sets")["kv"], [["m/1/v", 0.5]])  # meanwhile
            parts = [base64.b64decode(first["d"])]
            while len(parts) < 6:
                parts.append(base64.b64decode(files.recv()["d"]))
            self.assertEqual(b"".join(parts), data)
            files.send({"t": "blob_has", "h": [h]})  # still connected
            self.assertEqual(files.recv(), {"t": "blob_missing", "h": []})
        finally:
            relay_mod.MAX_OUTBOX = saved

    def test_the_size_limit(self):
        files = self.files()
        for size, ok in ((3 << 20, True), ((3 << 20) + 1, False)):
            data = b"z" * size
            answer = files.upload(data)
            self.assertEqual(answer["t"] == "blob_ok", ok, answer)
        self.assertEqual(os.listdir(self.relay.store.root), [sha256(b"z" * (3 << 20))])

    def test_a_relay_without_a_store_folder_makes_and_removes_its_own(self):
        root = self.relay.store.root
        self.assertTrue(os.path.isdir(root))
        self.server.stop()
        self.assertFalse(os.path.exists(root))
        kept = os.path.join(self.tmp, "store")
        server = RelayServer(self.loop, store=kept)
        server.start()
        server.stop()
        self.assertTrue(os.path.isdir(kept))  # a folder you name is yours


# --- bridges ------------------------------------------------------------------------------

class BridgeSnapshotTest(Session):
    def anna(self, **options):
        """Anna, whose FL has Beat.flp open; her samples are in her own folders."""
        home = os.path.join(self.tmp, "Anna")
        self.kick = self.write(os.path.join(home, "Samples", "kick.wav"), b"RIFF kick " * 300)
        self.project = save(os.path.join(home, "Beat.flp"), make_project(title="first"))
        fl, link = self.person("Anna", project=self.project, **options)
        self.take = self.write(os.path.join(link.places["fl_dirs"]["user_dir"], "FL Studio", "Audio", "Recorded",
                                            "take.wav"), b"take " * 50)
        return fl, link

    def beat(self, title="v1", **more):
        return make_project(samples=[KICK, self.kick, TAKE, GONE, self.kick], plugins=["Sytrus", "Serum"],
                            title=title, **more)

    def test_saving_shares_the_project_and_the_samples_it_needs(self):
        fl, anna = self.anna()
        self.settle(0.3)
        self.assertIsNone(self.relay.snapshot)  # opening a project isn't saving it
        data = self.beat()
        save(self.project, data)
        self.wait_for(lambda: self.relay.snapshot is not None)
        snapshot = self.relay.snapshot
        self.assertEqual(without(snapshot, "samples"),
                         {"v": 1, "name": "Beat", "fp": FP_A, "flp": sha256(data), "size": len(data),
                          "plugins": ["Sytrus", "Serum"], "by": "Anna", "from": anna.bridge.my_id,
                          "flv": "25.2.4.5242",  # the FL that saved it
                          "parent": 0})  # the version it builds on: none
        # FL's own samples exist everywhere and missing ones can't be sent.
        self.assertEqual(snapshot["samples"],
                         [{"h": sha256(read(self.kick)), "size": os.path.getsize(self.kick), "path": self.kick},
                          {"h": sha256(read(self.take)), "size": os.path.getsize(self.take), "path": TAKE}])
        for blob in (data, read(self.kick), read(self.take)):
            self.assertEqual(read(self.relay.store.path(sha256(blob))), blob)
        self.assertTrue(any(GONE in line for line in anna.logs), anna.logs)
        self.wait_for(lambda: self.notices(anna))
        self.assertEqual(self.notices(anna), [{"t": "snapshot", "v": 1, "name": "Beat", "fp": FP_A, "by": "Anna",
                                               "from": anna.bridge.my_id, "flv": "25.2.4.5242"}])
        self.wait_for(lambda: fl.got("snapshot"))
        self.assertEqual(fl.got("snapshot"), self.notices(anna))  # over the MIDI wire
        self.assertFalse(os.path.exists(anna.places["projects_dir"]))  # our own version isn't fetched
        self.assertShort(anna)

    def test_only_a_changed_project_is_shared(self):
        fl, anna = self.anna()
        data = self.beat()
        save(self.project, data)
        self.wait_for(lambda: self.relay.version == 1)
        save(self.project, data)  # saved again without changes
        self.settle(0.5)
        self.assertEqual(self.relay.version, 1)
        other = save(os.path.join(self.tmp, "Anna", "Other.flp"), make_project(title="other"))
        an_hour_ago = time.time() - 3600
        os.utime(other, (an_hour_ago, an_hour_ago))  # an existing project ...
        anna.mru = other  # ... that FL opened: that alone shares nothing
        self.wait_for(lambda: anna.bridge.watched == other)
        self.settle(0.5)
        self.assertEqual(self.relay.version, 1)
        save(other, make_project(title="other, edited"))
        self.wait_for(lambda: self.relay.version == 2)
        self.assertEqual(self.relay.snapshot["name"], "Other")

    def test_save_as_shares_the_new_file(self):
        fl, anna = self.anna()
        save(self.project, self.beat())
        self.wait_for(lambda: self.relay.version == 1)
        copy = self.write(os.path.join(self.tmp, "Anna", "Beat 2.flp"), self.beat("saved as"))  # written now
        anna.mru = copy  # File > Save as: FL now uses a file it wrote a moment ago
        self.wait_for(lambda: self.relay.version == 2)
        self.assertEqual(self.relay.snapshot["name"], "Beat 2")

    def test_a_project_saved_while_fl_was_away_is_shared_when_it_is_back(self):
        fl, anna = self.anna(join=False)
        save(self.project, self.beat())
        self.settle(0.5)
        self.assertIsNone(self.relay.snapshot)
        fl.hello()
        self.wait_for(lambda: self.relay.snapshot is not None)

    def test_the_others_get_a_project_folder_with_the_samples(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        data = self.beat(data_folder=r"C:\Users\Anna\Music\Beat")
        save(self.project, data)
        notice = self.received(bo)
        folder = os.path.join(bo.places["projects_dir"], "Beat")
        written = os.path.join(folder, "Beat.flp")
        short = {"t": "snapshot", "v": 1, "name": "Beat", "fp": FP_A, "by": "Anna", "from": anna.bridge.my_id,
                 "flv": "25.2.4.5242", "how": "open"}  # Bo's FL has no project of the session's: it opens it
        self.assertEqual(self.notices(bo), [short, dict(short, path=notice["path"])])
        self.assertTrue(same_path(notice["path"], written))
        project = flp.parse(read(written))
        self.assertEqual([s.path for s in project.samples],  # FL's own sample and the missing one stay as they were
                         [KICK, os.path.join(folder, "Samples", "kick.wav"),
                          os.path.join(folder, "Samples", "take.wav"), GONE])
        self.assertEqual(project.data_path, "")  # Anna's project data folder isn't on Bo's computer
        self.assertEqual(read(os.path.join(folder, "Samples", "kick.wav")), read(self.kick))
        self.assertEqual(read(os.path.join(folder, "Samples", "take.wav")), read(self.take))
        self.assertEqual(files_under(bo.places["projects_dir"]),
                         [os.path.join("Beat", "Beat.flp"), os.path.join("Beat", "Samples", "kick.wav"),
                          os.path.join("Beat", "Samples", "take.wav")])
        self.assertEqual(sorted(os.listdir(bo.places["cache_dir"])),
                         sorted({sha256(data), sha256(read(self.kick)), sha256(read(self.take))}))
        self.wait_for(lambda: len(bo_fl.got("snapshot")) == 2)
        self.assertEqual(bo_fl.got("snapshot"), self.notices(bo))
        self.assertEqual(bo.opened, [])  # --auto-open is off
        self.assertShort(bo)
        # Bo's bridge doesn't take the version it wrote for a save of Bo's own.
        bo.mru = written
        self.wait_for(lambda: bo.bridge.watched == os.path.abspath(written))
        self.settle(0.4)
        self.assertEqual(self.relay.version, 1)

    def test_a_new_version_replaces_the_old_one_and_keeps_its_samples(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        save(self.project, self.beat())
        written = self.received(bo)["path"]
        bo.mru = written  # Bo opens version 1 ...
        self.wait_for(lambda: bo.bridge.watched == os.path.abspath(written))
        with open(self.kick, "wb") as f:  # ... and Anna replaces kick.wav with another sound of that name
            f.write(b"RIFF other kick " * 100)
        save(self.project, self.beat("v2"))
        notice = self.received(bo, count=2)
        self.assertEqual(notice["v"], 2)
        self.settle(0.5)  # the bridge wrote Bo's open project: that's no save of Bo's
        self.assertEqual(self.relay.version, 2)
        folder = os.path.join(bo.places["projects_dir"], "Beat")
        project = flp.parse(read(os.path.join(folder, "Beat.flp")))
        self.assertEqual(project.title, "v2")
        self.assertEqual(project.samples[1].path, os.path.join(folder, "Samples", "kick (2).wav"))
        self.assertEqual(read(os.path.join(folder, "Samples", "kick (2).wav")), read(self.kick))
        self.assertEqual(read(os.path.join(folder, "Samples", "kick.wav")), b"RIFF kick " * 300)  # v1's, kept

    def test_someone_who_joins_later_gets_the_newest_version(self):
        fl, anna = self.anna()
        save(self.project, self.beat("v1"))
        self.wait_for(lambda: self.relay.version == 1)
        save(self.project, self.beat("v2"))
        self.wait_for(lambda: self.relay.version == 2)
        bo_fl, bo = self.person("Bo")
        notice = self.received(bo)
        self.assertEqual((notice["v"], flp.parse(read(notice["path"])).title), (2, "v2"))
        welcome = bo.sent[0]
        self.assertEqual((welcome["t"], welcome["snapshot"]),
                         ("welcome", {"v": 2, "name": "Beat", "fp": FP_A, "by": "Anna",
                                      "from": anna.bridge.my_id, "flv": "25.2.4.5242", "how": "open"}))
        self.assertNotIn("notes", welcome)
        self.assertShort(bo)

    def test_auto_open_opens_the_new_version(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo", auto_open=True)
        save(self.project, self.beat())
        notice = self.received(bo)
        self.wait_for(lambda: bo.opened)
        self.assertEqual(bo.opened, [notice["path"]])
        self.assertEqual(anna.opened, [])

    def test_plugins_this_fl_cant_find_are_named(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        self.write(os.path.join(bo.places["plugin_db"], "Effects", "Fruity", "Fruity Limiter.fst"), b"")
        os.makedirs(os.path.join(bo.places["fl_dirs"]["factory_dir"], "Plugins", "Fruity", "Generators", "Sytrus"))
        save(self.project, make_project(plugins=["Sytrus", "Fruity Limiter", "Serum", "Fruity Wrapper"]))
        notice = self.received(bo)
        self.assertEqual(notice["missing"], [{"plugin": "Serum"}])  # Sytrus comes with FL, the limiter is installed
        self.assertIn("Saknas här: Serum", bo.logs)  # where it is can't be told in this project

    def test_without_a_plugin_database_nothing_counts_as_missing(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        save(self.project, make_project(plugins=["Serum"]))
        self.assertNotIn("missing", self.received(bo))

    def test_a_damaged_download_is_thrown_away(self):
        fl, anna = self.anna()
        save(self.project, self.beat())
        self.wait_for(lambda: self.relay.snapshot is not None)
        kick = sha256(read(self.kick))
        with open(self.relay.store.path(kick), "r+b") as f:  # same size, other bytes
            f.write(b"X")
        bo_fl, bo = self.person("Bo")
        self.wait_for(lambda: any("kontrollsumman" in line for line in bo.logs), why="\n".join(bo.logs))
        self.settle(0.2)
        self.assertEqual(self.notices(bo, path=True), [])
        self.assertFalse(os.path.exists(os.path.join(bo.places["projects_dir"], "Beat")))
        self.assertNotIn(kick, os.listdir(bo.places["cache_dir"]))
        self.assertEqual([n for n in os.listdir(bo.places["cache_dir"]) if n.endswith(".part")], [])

    def test_names_from_the_network_stay_inside_the_folders(self):
        bo_fl, bo = self.person("Bo")
        mallory = self.raw("Mallory", "fl", fp=FP_A)
        files = self.files()
        hostile = {r"..\..\evil.wav": "evil.wav", r"C:\Windows\System32\drivers\etc\hosts": "hosts",
                   "CON.wav": "_CON.wav", 'a:b*c?"<>|.wav': "a_b_c_____.wav", r"\\server\share\run.exe": "run.exe.bin",
                   "../../../up.wav": "up.wav", "nul": "_nul", "kick.wav.": "kick.wav", " .. ": "sample",
                   "x\u202egnp.exe": "x_gnp.exe.bin"}
        samples = []
        for i, path in enumerate(hostile):
            data = b"sample %d" % i
            self.assertEqual(files.upload(data)["t"], "blob_ok")
            samples.append({"h": sha256(data), "size": len(data), "path": path})
        project = make_project(samples=list(hostile))
        files.upload(project)
        mallory.send({"t": "snap_put", "name": "..\\..\\x", "fp": FP_A, "flp": sha256(project),
                      "size": len(project), "samples": samples, "plugins": []})
        notice = self.received(bo)
        self.assertEqual(notice["name"], "..\\..\\x")  # shown as it is, but never used as a path
        folder = os.path.join(bo.places["projects_dir"], "_.._x")
        self.assertTrue(same_path(notice["path"], os.path.join(folder, "_.._x.flp")))
        self.assertEqual(files_under(bo.places["projects_dir"]),
                         sorted([os.path.join("_.._x", "_.._x.flp")]
                                + [os.path.join("_.._x", "Samples", name) for name in hostile.values()]))
        self.assertEqual(sorted(os.listdir(bo.folder)), ["Projects", "bridge-state.json", "cache"])  # nothing else
        written = flp.parse(read(notice["path"]))
        self.assertEqual([s.path for s in written.samples],
                         [os.path.join(folder, "Samples", name) for name in hostile.values()])

    @unittest.skipUnless(os.path.isdir(TEMPLATES), "FL Studio 2025:s mallar finns inte här")
    def test_a_big_template_from_fl_arrives_whole(self):
        biggest = max((os.path.join(where, name) for where, _, names in os.walk(TEMPLATES)
                       for name in names if name.endswith(".flp")), key=os.path.getsize)
        data = read(biggest)  # several chunks; only FL's own samples, which aren't sent
        original = flp.parse(data)
        home = os.path.join(self.tmp, "Anna")
        project = save(os.path.join(home, "Mall.flp"), make_project())
        fl, anna = self.person("Anna", project=project)
        bo_fl, bo = self.person("Bo")
        save(project, data)
        notice = self.received(bo, timeout=20)
        received = flp.parse(read(notice["path"]))
        self.assertEqual((received.samples, received.plugins, received.data_path),
                         (original.samples, original.plugins, ""))
        if not original.data_path:
            self.assertEqual(read(notice["path"]), data)
        self.assertEqual(self.relay.snapshot["samples"], [])

    def test_a_sanitised_name_is_harmless(self):
        safe = bridge_mod.safe_name
        for text, expected in (("Beat", "Beat"), ("Låt för två 🎹", "Låt för två 🎹"), ("../x", "_x"),
                               ("..", "x"), ("   ", "x"), ("a/b\\c", "a_b_c"), ("CON", "_CON"),
                               ("lpt1.txt", "_lpt1.txt"), ("COM\u00b9", "_COM\u00b9"), ("x" * 100, "x" * 64),
                               ("end. . ", "end"), ("\ud800bad", "_bad"), (None, "x"), (5, "x")):
            with self.subTest(text=text):
                self.assertEqual(safe(text, "x"), expected)
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        self.assertTrue(same_path(bridge_mod.inside(root, "ok"), os.path.join(root, "ok")))
        for name in ("..", "../x", "a/b", "", ".", "C:\\x"):
            with self.subTest(name=name), self.assertRaises(bridge_mod.TransferError):
                bridge_mod.inside(root, name)


# --- phase 3: content ------------------------------------------------------------------------

class ContentTest(Session):
    """docs/phase3.md, Content additions: samples that plugins keep in their
    state, plugins missing here named with their channels, FL versions."""

    def anna(self, **options):
        self.home = os.path.join(self.tmp, "Anna")
        self.project = save(os.path.join(self.home, "Beat.flp"), make_project(title="first"))
        return self.person("Anna", project=self.project, **options)

    def test_samples_inside_plugins_go_next_to_the_project(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        loop = self.write(os.path.join(self.home, "Loops", "Break 174.wav"), b"RIFF break " * 100)
        pad = self.write(os.path.join(self.home, "Ljud", "Pad – lång.flac"), b"fLaC pad " * 80)
        kick = self.write(os.path.join(self.home, "Samples", "kick.wav"), b"RIFF kick " * 50)
        factory = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Kits\Snare.wav"
        gone = r"D:\Borta\gone.wav"
        data = make_project(samples=[kick], channels=[("Drums", "DirectWave", state8(loop, factory, gone)),
                                                      ("Kit", "FPC", state16(pad, kick))])  # kick: a channel's too
        save(self.project, data)
        self.wait_for(lambda: self.relay.snapshot is not None)
        entry = lambda path, **more: dict({"h": sha256(read(path)), "size": os.path.getsize(path), "path": path}, **more)
        self.assertEqual(self.relay.snapshot["samples"],  # FL's own and missing ones aren't sent
                         [entry(kick), entry(loop, plugin=True), entry(pad, plugin=True), entry(kick, plugin=True)])
        self.assertTrue(any(gone in line and "plugins" in line for line in anna.logs), anna.logs)
        notice = self.received(bo)
        folder = os.path.dirname(notice["path"])
        self.assertEqual(files_under(folder), sorted(["Beat.flp", "Break 174.wav", "Pad – lång.flac", "kick.wav",
                                                      os.path.join("Samples", "kick.wav")]))
        for name, original in (("Break 174.wav", loop), ("Pad – lång.flac", pad), ("kick.wav", kick)):
            self.assertEqual(read(os.path.join(folder, name)), read(original))
        written = flp.parse(read(notice["path"]))
        self.assertEqual([s.path for s in written.samples], [os.path.join(folder, "Samples", "kick.wav")])
        self.assertEqual(written.plugin_samples, flp.parse(data).plugin_samples)  # the plugins' own paths stay
        self.assertShort(bo)

    def test_a_new_versions_plugin_sample_replaces_the_old_one(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        loop = self.write(os.path.join(self.home, "Loops", "Break.wav"), b"RIFF one " * 10)
        other = self.write(os.path.join(self.home, "Old", "Break.wav"), b"RIFF two " * 10)  # another file of that name
        save(self.project, make_project(title="v1", channels=[("Slicer", "Fruity Slicer", state8(loop)),
                                                               ("Slicer 2", "Fruity Slicer", state8(other))]))
        folder = os.path.dirname(self.received(bo)["path"])
        self.assertEqual(read(os.path.join(folder, "Break.wav")), b"RIFF one " * 10)  # the first one of the version
        self.assertTrue(any("Två samples" in line and "Break.wav" in line for line in bo.logs), bo.logs)
        with open(loop, "wb") as f:
            f.write(b"RIFF three " * 10)  # Anna records a new take under the same name
        save(self.project, make_project(title="v2", channels=[("Slicer", "Fruity Slicer", state8(loop))]))
        self.received(bo, count=2)
        self.assertEqual(read(os.path.join(folder, "Break.wav")), b"RIFF three " * 10)  # the newest version's
        self.assertEqual(sorted(os.listdir(folder)), ["Beat.flp", "Break.wav", "Samples", "Versioner"])

    def test_plugin_samples_from_the_network_stay_inside_the_folder(self):
        bo_fl, bo = self.person("Bo")
        mallory = self.raw("Mallory", "fl", fp=FP_A)
        files = self.files()
        hostile = {r"..\..\evil.wav": "evil.wav", r"C:\x\Beat.flp": None, r"\\server\share\run.exe": None,
                   r"C:\x\Samples": None, "CON.wav": "_CON.wav", "x\u202egnp.wav": "x_gnp.wav", "nul.mp3": "_nul.mp3",
                   "../up.ogg": "up.ogg", "C:\\x\\ . .wav": None}
        samples = []
        for i, path in enumerate(hostile):
            data = b"sample %d" % i
            self.assertEqual(files.upload(data)["t"], "blob_ok")
            samples.append({"h": sha256(data), "size": len(data), "path": path, "plugin": True})
        project = make_project(title="mallory")
        files.upload(project)
        mallory.send({"t": "snap_put", "name": "Beat", "fp": FP_A, "flp": sha256(project), "size": len(project),
                      "samples": samples, "plugins": []})
        notice = self.received(bo)
        self.assertEqual(files_under(bo.places["projects_dir"]),
                         sorted([os.path.join("Beat", "Beat.flp")]
                                + [os.path.join("Beat", name) for name in hostile.values() if name]))
        self.assertEqual(read(notice["path"]), project)  # no "sample" took the project's place
        self.assertEqual(sorted(os.listdir(bo.folder)), ["Projects", "bridge-state.json", "cache"])  # nothing else

    def test_missing_plugins_are_named_with_their_channels(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        for rel in (r"Generators\VST\Vital.fst", r"Effects\Fruity\Fruity Limiter.fst"):
            self.write(os.path.join(bo.places["plugin_db"], rel), b"")
        os.makedirs(os.path.join(bo.places["fl_dirs"]["factory_dir"], "Plugins", "Fruity", "Generators", "Sytrus"))
        channels = [("Kick", "", None), ("Lead", "Serum", None), ("Pad", "Sytrus", None), ("Keys", "Vital", None),
                    ("Lead 2", "Serum", None)] + [("Strings %d" % i, "Kontakt 7", None) for i in range(20)]
        effects = [("Pro-Q 3", None), ("Fruity Limiter", None), ("Serum", None)]
        save(self.project, make_project(channels=channels, effects=effects))
        notice = self.received(bo)
        self.assertEqual(notice["missing"], [{"plugin": "Serum", "channels": [2, 5], "mixer": True},
                                             {"plugin": "Kontakt 7", "channels": list(range(6, 22))},  # the first 16
                                             {"plugin": "Pro-Q 3", "mixer": True}])
        self.assertIn("Saknas här: Serum (kanal 2, 5), Kontakt 7 (kanal %s), Pro-Q 3 (mixern)"
                      % ", ".join(str(n) for n in range(6, 26)), bo.logs)
        self.assertShort(bo)

    def test_at_most_ten_missing_plugins_go_to_fl(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo")
        self.write(os.path.join(bo.places["plugin_db"], "Generators", "VST", "Vital.fst"), b"")
        names = ["Plugin %02d med ett mycket långt namn som FL inte behöver höra hela" % i for i in range(15)]
        save(self.project, make_project(channels=[(n, n, None) for n in names]))
        notice = self.received(bo)
        self.assertEqual(notice["missing"], [{"plugin": n[:40], "channels": [i + 1]} for i, n in enumerate(names[:10])])
        self.assertShort(bo)

    def test_a_version_from_a_newer_fl(self):
        fl, anna = self.anna()
        bo_fl, bo = self.person("Bo", flv="25.2.4.4960", auto_open=True)
        cy_fl, cy = self.person("Cy")  # an FL that doesn't say its version
        self.assertEqual(bo.bridge.fl_version, "25.2.4.4960")  # from FL's hello
        save(self.project, make_project(version="26.1.7.5512", title="ny"))
        notice = self.received(bo)
        self.assertEqual((notice["newer"], notice["flv"], self.relay.snapshot["flv"]),
                         ("26.1.7.5512", "26.1.7.5512", "26.1.7.5512"))
        self.assertTrue(any("sparad i FL 26.1.7.5512 och du har 25.2.4.4960" in line for line in bo.logs), bo.logs)
        self.assertNotIn("newer", self.received(cy))
        self.settle(0.3)
        self.assertEqual(bo.opened, [])  # FL can't open it: not even with --auto-open
        save(self.project, make_project(version="25.2.4.5242", title="samma"))  # a later build of Bo's release
        notice = self.received(bo, count=2)
        self.assertEqual(notice["flv"], "25.2.4.5242")
        self.assertNotIn("newer", notice)
        self.wait_for(lambda: bo.opened)

    def test_the_fl_version_shared_is_the_projects(self):
        fl, anna = self.anna(flv="25.2.4.4960")
        save(self.project, make_project(version="25.1.0.4000"))  # an older project, saved again unchanged in FL
        self.wait_for(lambda: self.relay.snapshot is not None)
        self.assertEqual(self.relay.snapshot["flv"], "25.1.0.4000")
        save(self.project, make_project(version="nonsense"))
        self.wait_for(lambda: self.relay.version == 2)
        self.assertEqual(self.relay.snapshot["flv"], "25.2.4.4960")  # then FL's own


class LoadedTest(Session):
    """docs/phase3.md, Loaded: the bridge tells FL when FL has opened the project
    it wrote for the newest version, so a plugin missing here doesn't pause it."""

    def setUp(self):
        super().setUp()
        self.project = save(os.path.join(self.tmp, "Anna", "Beat.flp"), make_project(title="first"))
        self.anna_fl, self.anna = self.person("Anna", fp=FP_B, project=self.project)  # the session's structure
        self.bo_fl, self.bo = self.person("Bo", fp=FP_A)
        self.bo.bridge.loaded_interval = 0.02
        self.versions = 0

    def share(self, fp=None):
        """Anna saves (a new structure with `fp`); the notice Bo's FL gets when it is written."""
        if fp is not None:
            self.anna_fl.report(fp)
            self.wait_for(lambda: self.anna.bridge.fl_fp == fp)
        self.versions += 1
        save(self.project, make_project(title="v%d" % self.versions))
        return self.received(self.bo, count=self.versions)

    def loaded(self, link=None):
        return [m for m in (link or self.bo).sent if m.get("t") == "loaded"]

    def opened(self, notice, fp=FP_D, title=None):
        """Bo opens a version in FL: its script reports what it loaded (here with
        a plugin missing: not the version's fp), and FL's recent files (or its
        title) name it."""
        self.bo_fl.report(fp)
        self.wait_for(lambda: self.bo.bridge.fl_fp == fp)
        if title is None:
            self.bo.mru = notice["path"]
        else:
            self.bo.title = title

    def test_fl_hears_it_once_it_has_opened_the_version(self):
        notice = self.share()
        self.settle(0.3)
        self.assertEqual(self.loaded(), [])  # written isn't opened
        self.bo_fl.report(FP_C)  # Bo changes his own project meanwhile
        self.settle(0.3)
        self.assertEqual(self.loaded(), [])
        self.opened(notice)
        self.wait_for(lambda: self.loaded())
        self.assertEqual(self.loaded(), [{"t": "loaded", "v": 1, "fp": FP_B}])
        self.wait_for(lambda: self.bo_fl.got("loaded"))  # over the MIDI wire
        self.bo_fl.report(FP_B)  # the script takes it: its structure counts as the version's
        self.settle(0.2)
        self.bo_fl.report(FP_C)  # then Bo adds a channel: no opening
        self.settle(0.3)
        self.assertEqual(len(self.loaded()), 1)
        self.assertTrue(any("FL har öppnat version 1 av 'Beat'" in line for line in self.bo.logs), self.bo.logs)
        self.assertEqual(self.loaded(self.anna), [])  # never for one's own version
        self.assertShort(self.bo)

    def test_fl_reporting_before_its_recent_files_show_it(self):
        notice = self.share()
        self.bo_fl.report(FP_D)  # the script reports the load first ...
        self.settle(0.2)
        self.bo.mru = notice["path"]  # ... the recent files follow
        self.wait_for(lambda: self.loaded())

    def test_an_old_report_is_no_opening(self):
        saved, bridge_mod.LOAD_LAG = bridge_mod.LOAD_LAG, 0.2
        try:
            notice = self.share()
            self.bo_fl.report(FP_C)  # a change of Bo's own, well before he opens the version
            self.settle(0.5)
            self.bo.mru = notice["path"]  # FL starts loading it ...
            self.settle(0.3)
            self.assertEqual(self.loaded(), [])
            self.bo_fl.report(FP_D)  # ... and the script reports what it loaded
            self.wait_for(lambda: self.loaded())
        finally:
            bridge_mod.LOAD_LAG = saved

    def test_the_window_title_tells_too(self):
        notice = self.share()
        self.bo.title = "Untitled - FL Studio 2025"
        self.bo_fl.report(FP_C)
        self.settle(0.3)
        self.assertEqual(self.loaded(), [])
        self.opened(notice, title="Beat - FL Studio 2025")  # FL's recent files don't say (another FL version's list)
        self.wait_for(lambda: self.loaded())
        self.assertEqual(self.loaded(), [{"t": "loaded", "v": 1, "fp": FP_B}])

    def test_a_title_naming_another_project_wins_over_the_recent_files(self):
        notice = self.share()
        self.bo.title = "Beat - FL Studio 2025"
        self.opened(notice)
        self.wait_for(lambda: len(self.loaded()) == 1)
        self.bo_fl.report(FP_B)
        self.bo.title = "Untitled - FL Studio 2025"  # File > New: the recent files still name the version
        self.settle(0.2)
        self.bo_fl.report(FP_C)
        self.settle(0.3)
        self.assertEqual(len(self.loaded()), 1)
        self.opened(notice, title="*Beat.flp - FL Studio 2025")  # the version again
        self.wait_for(lambda: len(self.loaded()) == 2)

    def test_a_title_showing_the_projects_own_title(self):
        notice = self.share()  # its title in Project info is "v1"
        self.bo.mru = os.path.join(self.tmp, "Bo", "Kopia.flp")  # Bo's own copy of the project has that title too
        self.opened(notice, title="v1 - FL Studio 2025")
        self.settle(0.4)
        self.assertEqual(self.loaded(), [])
        self.bo.mru = None  # FL's recent files don't say
        self.wait_for(lambda: self.loaded())

    def test_a_new_version_written_over_the_open_one(self):
        notice = self.share()
        self.opened(notice)
        self.wait_for(lambda: len(self.loaded()) == 1)
        self.bo_fl.report(FP_B)  # taken: Bo follows the session
        self.settle(0.2)
        notice = self.share(fp=FP_E)  # Anna changes the structure and saves: written over the file Bo has open
        self.assertEqual(notice["path"], self.bo.mru)
        self.settle(0.4)
        self.assertEqual(len(self.loaded()), 1)  # FL hasn't opened it again yet
        self.bo_fl.report(FP_C)  # now it has: what it read from the new version
        self.wait_for(lambda: len(self.loaded()) == 2)
        self.assertEqual(self.loaded()[-1], {"t": "loaded", "v": 2, "fp": FP_E})

    def test_written_over_a_project_fl_has_changed(self):
        notice = self.share()
        self.opened(notice)
        self.wait_for(lambda: len(self.loaded()) == 1)
        self.bo_fl.report(FP_B)
        self.bo_fl.report(FP_C)  # Bo changes the structure: he no longer follows the session
        self.settle(0.2)
        notice = self.share(fp=FP_E)
        self.bo_fl.report(FP_D)  # another change, or opening the new version? Can't be told
        self.settle(0.4)
        self.assertEqual(len(self.loaded()), 1)
        self.bo.mru = os.path.join(self.tmp, "Bo", "Annat.flp")  # Bo opens another project ...
        self.settle(0.2)
        self.opened(notice, FP_C)  # ... and then the version
        self.wait_for(lambda: len(self.loaded()) == 2)

    def test_fl_starting_over_with_the_version_open_hears_it_again(self):
        notice = self.share()
        self.opened(notice, FP_D)
        self.wait_for(lambda: len(self.loaded()) == 1)
        self.bo_fl.report(FP_B)
        self.settle(0.2)
        self.bo_fl.fp = FP_D  # FL's script starts over (or FL opens the file again) and reads it as before
        self.bo_fl.hello()
        self.wait_for(lambda: len(self.loaded()) == 2)
        self.bo_fl.report(FP_B)
        self.bo_fl.report(FP_C)
        self.settle(0.3)
        self.assertEqual(len(self.loaded()), 2)

    def test_fl_that_didnt_take_it_gets_it_with_its_next_report(self):
        notice = self.share()
        self.opened(notice, FP_C)
        self.wait_for(lambda: len(self.loaded()) == 1)
        self.bo_fl.report(FP_D)  # not the version's: FL's script didn't take that one
        self.wait_for(lambda: len(self.loaded()) == 2)

    def test_not_for_a_version_that_isnt_the_newest(self):
        notice = self.share()
        files = self.files()
        own = make_project(title="Bos")
        files.upload(own)
        line = bridge_mod.encode({"t": "snap_put", "name": "Bos", "fp": FP_A, "flp": sha256(own), "size": len(own),
                                  "samples": [], "plugins": []})
        self.bo.call(self.bo.bridge.writer.write, line)  # Bo shares a version of his own
        self.wait_for(lambda: self.bo.bridge.newest["v"] == 2)
        self.opened(notice)  # opening Anna's older one now is no opening of the session's version
        self.settle(0.4)
        self.assertEqual(self.loaded(), [])


class ContentHelpersTest(unittest.TestCase):
    def test_title_shows(self):
        shows = bridge_mod.title_shows
        for title in ("Beat - FL Studio 2025", "FL Studio 2025 - Beat", "Beat.flp - FL Studio 2025", "*Beat - FL Studio",
                      "Beat* - FL Studio 2025", "FL Studio 2025 - Beat.flp *", "beat", "BEAT \u2013 FL Studio 2025",
                      "  Beat | FL Studio  "):
            with self.subTest(title):
                self.assertTrue(shows(title, "Beat"))
        for title in ("Beat 2 - FL Studio 2025", "FL Studio 2025", "Untitled - FL Studio 2025", "Beatles - FL Studio",
                      "Anna - Beat - FL Studio 2025", "FL Studio 2025 - Beat 2", "Beat-FL Studio", "", None):
            with self.subTest(title):
                self.assertFalse(shows(title, "Beat"))
        self.assertTrue(shows("Anna - Beat - FL Studio 2025", "Anna - Beat"))
        self.assertTrue(shows("Låt (2) [mix] - FL Studio", "Låt (2) [mix]"))
        self.assertFalse(shows("Beat - FL Studio", ""))

    def test_fl_versions(self):
        self.assertEqual([bridge_mod.fl_version(v) for v in ("25.2.4.4960", "11.1", "25", "25.2.x", "", None, 25,
                                                               "1.2.3.4.5.6.7", "9" * 50)],
                         ["25.2.4.4960", "11.1", None, None, None, None, None, None, None])
        newer = bridge_mod.newer
        self.assertTrue(newer("26.1.7.5512", "25.2.4.4960"))
        self.assertTrue(newer("25.2.10.1", "25.2.4.4960"))
        self.assertTrue(newer("25.2.99.4720", "25.2.4.4960"))  # a beta of the next release
        self.assertFalse(newer("25.2.4.5242", "25.2.4.4960"))  # a later build of the same release
        self.assertFalse(newer("24.2.99.4720", "25.2.4.4960"))
        self.assertFalse(newer("26.1.7.5512", None))
        self.assertFalse(newer("", "25.2.4.4960"))

    def test_plugin_sample_names(self):
        name = bridge_mod.plugin_sample_name
        self.assertEqual([name(p) for p in (r"C:\Loops\Break 174.WAV", "D:/x/Pad – lång.flac", r"\\nas\a\CON.wav",
                                            r"C:\x\run.exe", r"C:\x\Beat.flp", r"C:\x\Samples", r"C:\x\.wav",
                                            "x\u202egnp.ogg", None, "C:\\x\\" + "a" * 300 + ".wv")],
                         ["Break 174.WAV", "Pad – lång.flac", "_CON.wav", None, None, None, None, "x_gnp.ogg", None,
                          "a" * 200 + ".wv"])

    def test_missing_text(self):
        self.assertEqual([bridge_mod.missing_text(e) for e in ({"plugin": "Serum", "channels": [3, 7]},
                                                               {"plugin": "Pro-Q 3", "mixer": True},
                                                               {"plugin": "Serum", "channels": [1], "mixer": True},
                                                               {"plugin": "Kontakt"})],
                         ["Serum (kanal 3, 7)", "Pro-Q 3 (mixern)", "Serum (kanal 1)", "Kontakt"])

    @unittest.skipUnless(sys.platform == "win32", "bara Windows har FL:s fönster")
    def test_reading_window_titles(self):
        saved = bridge_mod.FL_WINDOW
        try:
            bridge_mod.FL_WINDOW = "FLSync test: no such window"
            self.assertIsNone(bridge_mod.fl_window_title())
            bridge_mod.FL_WINDOW = "Shell_TrayWnd"  # the taskbar stands in for FL's window: one, without a title
            self.assertIn(bridge_mod.fl_window_title(), ("", None))
        finally:
            bridge_mod.FL_WINDOW = saved


class FakeRegistry:
    """Stands in for `winreg`: HKEY_CURRENT_USER as {key path: {value name: value}}."""

    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, keys):
        self.keys = keys

    def OpenKey(self, root, path):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self.Key(path)

    def EnumKey(self, key, index):
        children = sorted({name[len(key.path) + 1:].split("\\")[0] for name in self.keys
                           if name.startswith(key.path + "\\")})
        if index >= len(children):
            raise OSError("no more")
        return children[index]

    def QueryValueEx(self, key, name):
        values = self.keys[key.path]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass


class PlacesTest(unittest.TestCase):
    """Where the bridge finds FL's newest project and folders (a fake registry)."""

    def with_registry(self, keys, function):
        saved = sys.modules.get("winreg")
        sys.modules["winreg"] = FakeRegistry(keys) if keys is not None else None
        try:
            return function()
        finally:
            if saved is None:
                sys.modules.pop("winreg", None)
            else:
                sys.modules["winreg"] = saved

    def test_the_newest_project_of_the_newest_fl(self):
        keys = {r"Software\Image-Line": {},
                r"Software\Image-Line\FL Studio 3\MRU": {"0": r"C:\gammal.flp"},
                r"Software\Image-Line\FL Studio 21\MRU": {"0": r"C:\21.flp", "1": r"C:\äldre.flp"},
                r"Software\Image-Line\FL Studio 25\MRU": {"0": r"C:\Låtar\Beat.flp"},
                r"Software\Image-Line\FL Studio 25\General": {},
                r"Software\Image-Line\Shared\Paths": {}}
        self.assertEqual(self.with_registry(keys, bridge_mod.read_mru), r"C:\Låtar\Beat.flp")
        del keys[r"Software\Image-Line\FL Studio 25\MRU"]  # FL 25 has opened nothing yet
        self.assertEqual(self.with_registry(keys, bridge_mod.read_mru), r"C:\21.flp")
        self.assertIsNone(self.with_registry({}, bridge_mod.read_mru))
        self.assertIsNone(self.with_registry(None, bridge_mod.read_mru))  # not Windows: no winreg

    def test_fls_folders(self):
        user = tempfile.mkdtemp(prefix="flsync-user-")
        self.addCleanup(shutil.rmtree, user, True)
        keys = {r"Software\Image-Line\Shared\Paths": {"Install path": r"C:\Program Files\Image-Line\FL Studio 2025",
                                                      "Shared data": user + "\\"}}
        self.assertEqual(self.with_registry(keys, bridge_mod.fl_folders),
                         {"factory_dir": r"C:\Program Files\Image-Line\FL Studio 2025", "user_dir": user,
                          "downloads_dir": os.path.join(user, "Downloads")})
        found = self.with_registry(None, bridge_mod.fl_folders)
        self.assertIsNone(found["factory_dir"])
        self.assertEqual(os.path.basename(found["user_dir"]), "Image-Line")  # <Documents>\Image-Line

    def test_a_bridge_given_no_places_uses_flsyncs_own(self):
        bridge = bridge_mod.Bridge("127.0.0.1", 1, None, logger=lambda m: None, mru=lambda: None,
                                   fl_dirs={"factory_dir": None, "user_dir": r"C:\U\Image-Line", "downloads_dir": None})
        self.assertEqual(bridge.projects_dir, os.path.join(r"C:\U\Image-Line", "FL Studio", "Projects", "FLSync"))
        self.assertEqual(bridge.plugin_db,
                         os.path.join(r"C:\U\Image-Line", "FL Studio", "Presets", "Plugin database", "Installed"))
        self.assertEqual(bridge.cache_dir, os.path.join(bridge_mod.local_data_dir(), "cache"))
        self.assertEqual(bridge.notes_dir, os.path.join(bridge_mod.local_data_dir(), "notes"))
        self.assertFalse(bridge.auto_open)


class BridgeLimitTest(Session):
    relay_options = {"max_blob": 64 << 10}

    def test_files_over_the_relays_limit_are_left_out(self):
        home = os.path.join(self.tmp, "Anna")
        big = self.write(os.path.join(home, "big.wav"), bytes(100 << 10))
        small = self.write(os.path.join(home, "small.wav"), b"small")
        project = save(os.path.join(home, "Beat.flp"), make_project())
        fl, anna = self.person("Anna", project=project)
        save(project, make_project(padding=70 << 10))  # the project itself is too big
        self.wait_for(lambda: any("större än relayns gräns" in line for line in anna.logs))
        self.settle(0.3)
        self.assertIsNone(self.relay.snapshot)
        save(project, make_project(samples=[big, small]))
        self.wait_for(lambda: self.relay.snapshot is not None)
        self.assertEqual([s["path"] for s in self.relay.snapshot["samples"]], [small])
        self.assertTrue(any("större än relayns gräns" in line and big in line for line in anna.logs))


class BridgeNotesTest(Session):
    def context(self, link):
        """context.json as the piano roll scripts read it, or {} if it isn't there."""
        for _ in range(50):
            try:
                with open(os.path.join(link.places["notes_dir"], "context.json"), encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return {}
            except PermissionError:  # the bridge is replacing it this very moment
                time.sleep(0.005)
        return {}

    def outbox(self, link, data):
        path = os.path.join(link.places["notes_dir"], "outbox.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        for attempt in range(50):
            try:
                os.replace(path + ".tmp", path)  # as the script does it
                break
            except PermissionError:  # Windows: the bridge is reading the old one this very moment
                time.sleep(0.005)
        return path

    def test_what_the_piano_roll_shows_stays_with_the_bridge(self):
        fl, anna = self.person("Anna", join=False)
        fl.send({"t": "pr", "p": 3, "c": 1, "n": "Bas"})  # before any session: the scripts still need it
        self.wait_for(lambda: self.context(anna))
        context = self.context(anna)
        self.assertEqual(without(context, "time"), {"pattern": 3, "channel": 1, "name": "Bas", "session": None})
        self.assertLess(abs(time.time() - context["time"]), 5)
        fl.hello()
        self.wait_for(lambda: anna.bridge.my_id is not None)
        fl.send({"t": "pr", "p": 4, "c": 2, "n": "Lead"})
        self.wait_for(lambda: self.context(anna).get("pattern") == 4)
        self.settle(0.2)
        self.assertEqual([m for m in anna.sent if m.get("t") == "error"], [])  # the relay never heard "pr"
        anna.bridge.context_interval = 0.05
        before = self.context(anna)["time"]
        self.wait_for(lambda: self.context(anna).get("time", 0) > before)  # kept fresh
        anna.call(anna.bridge.close)  # the bridge ends: the scripts can tell at once
        self.assertFalse(os.path.exists(os.path.join(anna.places["notes_dir"], "context.json")))

    def test_notes_go_from_one_bridge_to_the_others(self):
        anna_fl, anna = self.person("Anna")
        bo_fl, bo = self.person("Bo")
        bo_fl.send({"t": "pr", "p": 2, "c": 4, "n": "Bas"})
        self.wait_for(lambda: self.context(bo))
        path = self.outbox(anna, {"pattern": 2, "channel": 4, "name": "Bas", "ppq": 96, "notes": [NOTE, NOTE2],
                                  "time": time.time()})
        self.wait_for(lambda: self.context(bo).get("session") is not None)
        self.assertEqual(self.context(bo)["session"], {"v": 1, "by": "Anna", "ppq": 96, "notes": [NOTE, NOTE2]})
        self.assertFalse(os.path.exists(path))
        notice = {"t": "notes", "p": 2, "c": 4, "by": "Anna", "from": anna.bridge.my_id, "n": 2}
        self.wait_for(lambda: bo_fl.got("notes") and anna_fl.got("notes"))
        self.assertEqual(bo_fl.got("notes"), [notice])  # FL only hears how many
        self.assertEqual(anna_fl.got("notes"), [notice])
        self.assertEqual(self.relay.notes[(2, 4)]["notes"], [NOTE, NOTE2])
        bo_fl.send({"t": "pr", "p": 2, "c": 5, "n": "Lead"})  # another channel: nothing for it yet
        self.wait_for(lambda: self.context(bo).get("channel") == 5)
        self.assertIsNone(self.context(bo)["session"])
        self.assertShort(bo)

    def test_someone_who_joins_later_finds_the_notes(self):
        anna_fl, anna = self.person("Anna")
        self.outbox(anna, {"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE]})
        self.wait_for(lambda: self.relay.notes)
        bo_fl, bo = self.person("Bo", join=False)
        bo_fl.send({"t": "pr", "p": 1, "c": 0, "n": "Kick"})
        bo_fl.hello()
        self.wait_for(lambda: self.context(bo).get("session") is not None)
        self.assertEqual(self.context(bo)["session"]["notes"], [NOTE])
        self.assertNotIn("notes", [m for m in bo.sent if m["t"] == "welcome"][0])

    def test_an_outbox_still_being_written_waits_and_broken_notes_are_dropped(self):
        fl, anna = self.person("Anna")
        path = self.outbox(anna, '{"pattern": 1, "chan')  # not whole yet
        self.settle(0.3)
        self.assertTrue(os.path.exists(path))
        self.outbox(anna, {"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE]})
        self.wait_for(lambda: self.relay.notes)
        for broken in ({"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE[:13]]},
                       {"pattern": 0, "channel": 0, "ppq": 96, "notes": []},
                       {"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE] * 20001}, [1, 2]):
            path = self.outbox(anna, broken)
            self.wait_for(lambda: not os.path.exists(path))
        self.settle(0.2)
        self.assertEqual(self.relay.notes_version, 1)
        self.assertTrue(any("gick inte att läsa" in line for line in anna.logs))

    def test_notes_for_another_structure_are_refused(self):
        anna_fl, anna = self.person("Anna", fp=FP_A)
        bo_fl, bo = self.person("Bo", fp=FP_B)
        self.outbox(bo, {"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE]})
        self.wait_for(lambda: [m for m in bo.sent if m.get("t") == "error"])
        self.assertTrue([m for m in bo.sent if m.get("t") == "error"][0]["stale"])
        self.assertEqual(self.relay.notes, {})

    def test_a_new_structure_drops_the_old_notes(self):
        anna_fl, anna = self.person("Anna")
        bo_fl, bo = self.person("Bo")
        bo_fl.send({"t": "pr", "p": 1, "c": 0, "n": "Kick"})
        self.outbox(anna, {"pattern": 1, "channel": 0, "ppq": 96, "notes": [NOTE]})
        self.wait_for(lambda: self.context(bo).get("session") is not None)
        files = self.files()
        project = make_project()
        files.upload(project)
        anna_fl.send({"t": "fp", "fp": FP_B})  # Anna added a channel ...
        self.relay_snap(anna, project)  # ... and saved it
        self.wait_for(lambda: self.context(bo).get("session", "-") is None)

    def relay_snap(self, link, project):
        """Send a snap_put on a bridge's session connection, as if it had shared."""
        line = bridge_mod.encode({"t": "snap_put", "name": "Beat", "fp": FP_B, "flp": sha256(project),
                                  "size": len(project), "samples": [], "plugins": []})
        link.call(link.bridge.writer.write, line)


class PianoRollTest(Session):
    """FL's piano roll scripts, with a fake `flpianoroll`, sending notes through
    two real bridges and a relay."""

    class Note:
        def __init__(self, **fields):
            self.time, self.length, self.number, self.velocity, self.pan, self.release = 0, 0, 60, 0.8, 0.5, 0.5
            self.color, self.fcut, self.fres, self.pitchofs, self.repeats = 0, 0.5, 0.5, 0, 0
            self.slide = self.porta = self.muted = self.selected = False
            self.group = 0
            self.__dict__.update(fields)

    def run_script(self, name, link, notes, ppq):
        """Run fl/Piano roll scripts/<name>.pyscript in `link`'s exchange folder;
        returns the piano roll's notes afterwards and the messages it showed."""
        test, messages = self, []

        class Score:
            PPQ = ppq

            def __init__(self):
                self.notes = list(notes)

            @property
            def noteCount(self):
                return len(self.notes)

            def getNote(self, index):
                return self.notes[index]

            def addNote(self, note):
                self.notes.append(note)

            def clearNotes(self, all=False):
                self.notes = []

        utils = types.SimpleNamespace(ShowMessage=messages.append, log=lambda text: None)
        module = types.ModuleType("flpianoroll")
        module.score, module.Note, module.Utils = Score(), test.Note, utils
        module.__all__ = ["score", "Note", "Utils"]
        base = os.path.dirname(os.path.dirname(link.places["notes_dir"]))  # <base>\FLSync\notes
        saved_env, saved_module = os.environ.get("LOCALAPPDATA"), sys.modules.get("flpianoroll")
        os.environ["LOCALAPPDATA"] = base
        sys.modules["flpianoroll"] = module
        try:
            path = os.path.join(SCRIPTS, name + ".pyscript")
            with open(path, encoding="utf-8") as f:
                exec(compile(f.read(), path, "exec"), {"__name__": "__main__"})
        finally:
            if saved_env is None:
                del os.environ["LOCALAPPDATA"]
            else:
                os.environ["LOCALAPPDATA"] = saved_env
            if saved_module is None:
                sys.modules.pop("flpianoroll", None)
            else:
                sys.modules["flpianoroll"] = saved_module
        return module.score.notes, messages

    def person_with_scripts(self, name):
        base = os.path.join(self.tmp, name)  # the scripts find the folder from %LOCALAPPDATA%
        return self.person(name, notes_dir=os.path.join(base, "FLSync", "notes"))

    def test_notes_from_one_piano_roll_land_in_the_other(self):
        anna_fl, anna = self.person_with_scripts("Anna")
        bo_fl, bo = self.person_with_scripts("Bo")
        for fl in (anna_fl, bo_fl):
            fl.send({"t": "pr", "p": 2, "c": 4, "n": "Bas"})
        self.wait_for(lambda: all(os.path.exists(os.path.join(link.places["notes_dir"], "context.json"))
                                  for link in (anna, bo)))
        played = [self.Note(time=0, length=48, number=60, velocity=0.5, slide=True),
                  self.Note(time=96, length=24, number=67, muted=True, repeats=3, color=5)]
        _, shown = self.run_script("FLSync skicka noter", anna, played, ppq=96)
        self.assertEqual(shown, [])
        self.wait_for(lambda: bo_fl.got("notes"))
        notes, shown = self.run_script("FLSync hämta noter", bo, [self.Note(number=40)], ppq=192)
        self.assertEqual(shown, [])
        self.assertEqual([(n.time, n.length, n.number, n.velocity, n.slide, n.muted, n.repeats, n.color)
                          for n in notes],
                         [(0, 96, 60, 0.5, True, False, 0, 0), (192, 48, 67, 0.8, False, True, 3, 5)])

    def test_without_notes_for_that_channel_the_script_says_so(self):
        bo_fl, bo = self.person_with_scripts("Bo")
        bo_fl.send({"t": "pr", "p": 1, "c": 0, "n": "Kick"})
        self.wait_for(lambda: os.path.exists(os.path.join(bo.places["notes_dir"], "context.json")))
        notes, shown = self.run_script("FLSync hämta noter", bo, [self.Note()], ppq=96)
        self.assertEqual(len(notes), 1)
        self.assertIn("ingen har skickat noter till Kick i mönster 1", shown[0])


# --- simulated FL Studios end to end ------------------------------------------------------

class EndToEnd(unittest.TestCase):
    """The real FLSync script in simulated FL Studios, real bridges and a real relay."""

    def setUp(self):
        self.loop = LoopThread()
        self.server = RelayServer(self.loop)
        self.port = self.server.start()
        self.tmp = tempfile.mkdtemp(prefix="flsync-test-")
        self.fls, self.links = [], {}

    def tearDown(self):
        for link in self.links.values():
            link.stop()
        invalid = {}
        for fl in self.fls:
            fl.close()
            if fl.project.invalid:
                invalid[fl.name] = fl.project.invalid
        self.server.stop()
        self.loop.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(invalid, {}, "scriptet anropade FL med index som inte finns")

    @property
    def relay(self):
        return self.server.relay

    def fl(self, name, project=None):
        fl = FakeFL(name)
        self.fls.append(fl)
        link = BridgeLink(self.loop, self.port, fl, mru=lambda: link.mru, fl_title=lambda: None)
        link.mru = project
        self.links[fl] = link
        return fl

    def wait_for(self, condition, timeout=10.0, why="tog för lång tid"):
        self.assertTrue(pump(self.fls, condition, timeout), why)

    def ready(self, *fls):
        self.wait_for(lambda: all(fl.client.joined and not fl.client.pending for fl in fls), timeout=30)

    def project_file(self, name="Beat"):
        """Anna's project, saved: FL's Basic template if FL is installed, else a small one,
        with one of her own samples added."""
        home = os.path.join(self.tmp, "Anna")
        sample = os.path.join(home, "Samples", "vox.wav")
        os.makedirs(os.path.dirname(sample))
        with open(sample, "wb") as f:
            f.write(b"RIFF vox " * 200)
        if os.path.isfile(BASIC):
            data = read(BASIC)
            first = flp.parse(data).samples[0].path
            data = flp.rewrite_paths(data, {first: sample})  # one of her own instead of FL's
        else:
            data = make_project(samples=[KICK, sample])
        return os.path.join(home, name + ".flp"), data, sample


class EndToEndTest(EndToEnd):
    def test_a_save_reaches_the_other_fl_as_a_project_folder(self):
        path, data, sample = self.project_file()
        save(path, data)
        a, b = self.fl("A", project=path), self.fl("B")
        self.ready(a, b)
        save(path, data)  # A presses Ctrl+S (the first save since the bridge started counts)
        self.wait_for(lambda: any("version 1 av Beat ligger i" in line for line in b.logs),
                      why="\n".join(self.links[b].logs))
        written = os.path.join(self.links[b].places["projects_dir"], "Beat", "Beat.flp")
        project = flp.parse(read(written))
        self.assertEqual([s.path for s in project.samples if not s.factory],
                         [os.path.join(os.path.dirname(written), "Samples", "vox.wav")])
        self.wait_for(lambda: any("din version (1) är delad" in line for line in a.logs))
        self.wait_for(lambda: "FLSync: A delade en ny version av Beat" in b.project.hints)
        self.assertTrue(b.client.synced)  # same structure: nothing pauses
        a.set_volume(4, 0.3)
        self.wait_for(lambda: b.volume(4) == 0.3)

    def test_a_new_structure_pauses_the_others_until_they_open_it(self):
        path, data, _ = self.project_file()
        save(path, data)
        a, b = self.fl("A", project=path), self.fl("B")
        self.ready(a, b)
        a.set_volume(3, 0.4)
        self.wait_for(lambda: b.volume(3) == 0.4)
        a.project.rack.add_channel(len(a.project.rack.channels), "Perc")  # A adds a channel
        self.wait_for(lambda: a.client.my_fp != b.client.my_fp and not a.client.synced)
        self.assertEqual(self.relay.fp, b.client.my_fp)  # B is here: the session keeps its structure
        save(path, data)  # A saves: the new structure becomes the session's
        self.wait_for(lambda: self.relay.fp == a.client.my_fp and a.client.synced and not b.client.synced
                      and self.relay.state.get("m/3/v") == 0.4, timeout=20)
        self.wait_for(lambda: any("ligger i" in line for line in b.logs))
        # Only the channels differ: the mixer still syncs both ways, the channels don't.
        b.set_volume(6, 0.2)
        a.set_volume(7, 0.6)
        self.wait_for(lambda: b.volume(7) == 0.6 and a.volume(6) == 0.2, timeout=10)
        a_channels, b_channels = a.project.modules["channels"], b.project.modules["channels"]
        a_channels.setChannelName(0, "Annas", True)
        self.wait_for(lambda: self.relay.state.get("c/0/n") == "Annas", timeout=10)
        pump(self.fls, lambda: False, 0.5)
        self.assertNotEqual(b_channels.getChannelName(0, True), "Annas")
        b.project.rack.add_channel(len(b.project.rack.channels), "Perc")  # B opens A's version
        b.load_project({})
        self.wait_for(lambda: b.client.synced and b_channels.getChannelName(0, True) == "Annas", timeout=20)
        a.set_volume(5, 0.3)
        self.wait_for(lambda: b.volume(5) == 0.3)

    def test_alone_a_new_structure_becomes_the_sessions(self):
        a = self.fl("A")
        self.ready(a)
        old = self.relay.fp
        self.assertEqual(old, a.client.my_fp)
        a.project.rack.add_channel(len(a.project.rack.channels), "Perc")
        a.set_volume(2, 0.35)
        self.wait_for(lambda: self.relay.fp == a.client.my_fp != old and self.relay.state.get("m/2/v") == 0.35
                      and a.client.synced, timeout=20)

    def test_someone_who_joins_later_gets_the_project(self):
        path, data, _ = self.project_file()
        save(path, data)
        a = self.fl("A", project=path)
        self.ready(a)
        save(path, data)
        self.wait_for(lambda: self.relay.snapshot is not None)
        b = self.fl("B")
        self.wait_for(lambda: any("version 1 av Beat ligger i" in line for line in b.logs),
                      why="\n".join(self.links[b].logs))
        self.ready(b)
        self.assertTrue(b.client.synced)


if __name__ == "__main__":
    unittest.main()
