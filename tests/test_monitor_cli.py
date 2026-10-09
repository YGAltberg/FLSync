"""The monitor (tools/monitor.py), run as a real program against relay.py: the
test types commands and plays someone else in the session with a raw protocol
client. Also how the monitor describes every kind of key, and how it keeps its
own changes pending (protocol v2, docs/protocol_v2.md).

    python -m unittest discover -s tests -p "test_monitor_cli.py" -v
"""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay", "relay.py")
MONITOR = os.path.join(ROOT, "tools", "monitor.py")
PROTO = 2

_spec = importlib.util.spec_from_file_location("monitor_under_test", MONITOR)
monitor_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor_mod)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(script, *args):
    # Without these the pipes have Windows' own encoding, which can't show everything.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    return subprocess.Popen([sys.executable, script, *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)


class Output:
    """A program's output, read line by line on a thread so a test can wait for a line."""

    def __init__(self, process):
        self.lines = []
        self.done = False
        self.changed = threading.Condition()
        self.thread = threading.Thread(target=self._read, args=(process.stdout,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        for line in stream:
            with self.changed:
                self.lines.append(line.rstrip("\n"))
                self.changed.notify_all()
        with self.changed:
            self.done = True
            self.changed.notify_all()

    def wait_for(self, text, timeout=5.0):
        """The first line containing `text`, or None if it doesn't come in time."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                for line in self.lines:
                    if text in line:
                        return line
                left = deadline - time.monotonic()
                if self.done or left <= 0:
                    return None
                self.changed.wait(left)

    def text(self):
        with self.changed:
            return "\n".join(self.lines)


class Peer:
    """Someone else in the session, speaking the protocol over a plain socket."""

    def __init__(self, port, name, app="fl"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rb")
        self.send({"t": "hello", "proto": PROTO, "name": name, "app": app})
        self.welcome = self.recv()

    def send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def recv(self):
        return json.loads(self.file.readline())

    def recv_kind(self, kind):
        while True:
            msg = self.recv()
            if msg.get("t") == kind:
                return msg

    def close(self):
        self.file.close()
        self.sock.close()


class MonitorProgramTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.store = tempfile.mkdtemp(prefix="flsync-store-")
        self.relay = start(RELAY, "--port", str(self.port), "--store", self.store)
        self.relay_out = Output(self.relay)  # also keeps the relay's output pipe from filling up
        self.assertIsNotNone(self.relay_out.wait_for("lyssnar"), self.relay_out.text())
        self.monitor = self.out = None
        self.peers = []

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        for process, output in ((self.monitor, self.out), (self.relay, self.relay_out)):
            if process is None:
                continue
            if process.poll() is None:
                process.kill()
            process.wait(5)
            output.thread.join(5)
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        shutil.rmtree(self.store, ignore_errors=True)
        self.assertNotIn("Traceback", self.relay_out.text())

    def join(self, name, app="fl"):
        peer = Peer(self.port, name, app)
        self.peers.append(peer)
        return peer

    def start_monitor(self, port=None):
        self.monitor = start(MONITOR, "--port", str(port or self.port), "--name", "mon")
        self.out = Output(self.monitor)

    def type(self, *lines):
        self.monitor.stdin.write("".join(line + "\n" for line in lines))
        self.monitor.stdin.flush()

    def shows(self, text, timeout=5.0):
        line = self.out.wait_for(text, timeout)
        self.assertIsNotNone(line, "monitorn skrev aldrig %r. Den skrev:\n%s" % (text, self.out.text()))
        return line

    def quit(self):
        self.type("quit")
        self.assertEqual(self.monitor.wait(5), 0)
        self.out.thread.join(5)
        self.assertNotIn("Traceback", self.out.text())

    def test_commands_send_the_right_changes(self):
        other = self.join("A")
        self.start_monitor()
        monitor_id = other.recv_kind("peer_join")["id"]
        # Typed at once: the monitor takes them only after its welcome.
        self.type("vol 1 0.5", "pan 1 -0.25", "mute 1", "mute 1 off", "chvol 2 0.7", "chpan 1 0.3",
                  "vol x 0.5", "chvol 0 0.5", "tempo 128", "play", "stop", "vol 1 2", "tempo 5",
                  "set c/0/v 0.5", "set s/play true", 'set pt/1/n "Min vers"', 'set g/1/0 "1000100010001000"',
                  'set mr/3 "0:0.8,5:0.5"', "set pl/1/n Trummor på slutet", "set bad/1 [1]", "set x/1 null")
        expected = [["m/1/v", 0.5], ["m/1/p", -0.25], ["m/1/mu", True], ["m/1/mu", False],
                    ["c/1/v", 0.7], ["c/0/p", 0.3],  # channels are typed from 1, as they are shown
                    ["s/tempo", 128.0], ["s/play", True], ["s/play", False],
                    ["m/1/v", 1.0], ["s/tempo", 10.0],  # kept within what FL takes
                    ["c/0/v", 0.5], ["s/play", True], ["pt/1/n", "Min vers"], ["g/1/0", "1000100010001000"],
                    ["mr/3", "0:0.8,5:0.5"], ["pl/1/n", "Trummor på slutet"], ["x/1", None]]
        for pair in expected:
            msg = other.recv_kind("sets")
            self.assertEqual((msg["from"], msg["kv"]), (monitor_id, [pair]))
        self.shows("Kommandon:")  # "vol x 0.5" and "chvol 0 0.5" got the help
        self.shows("! relayn avvisade bad/1: ogiltigt värde")

        self.type("get pt/", "get m/1", "get nope/")
        self.shows('  pt/1/n = "Min vers"')
        self.shows("  m/1/mu = false")
        self.shows("  m/1/v = 1.0")
        self.shows('Inga värden i sessionen börjar med "nope/".')

        # The relay acked the monitor's last change to m/1/v before A saw it,
        # so A's newer change is no longer ignored.
        other.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3]]})
        self.shows("[A] Insert 1 volym 0.300")
        self.type("get m/1/v")
        self.shows("  m/1/v = 0.3")
        self.quit()

    def test_demo_sweeps_a_fader(self):
        other = self.join("A")
        self.start_monitor()
        other.recv_kind("peer_join")
        self.type("demo 3")
        sweep = [other.recv_kind("sets")["kv"] for _ in range(5)]
        self.assertEqual({kv[0][0] for kv in sweep}, {"m/3/v"})
        self.assertEqual(len({kv[0][1] for kv in sweep}), 5)  # it moves
        self.shows("Demo: Insert 3 svepar i 5 sekunder")
        self.quit()  # in the middle of the sweep

    def test_shows_what_others_change(self):
        other = self.join("A")
        other.send({"t": "sets", "l": 1, "kv": [["m/2/v", 0.25], ["s/tempo", 140.0], ["s/play", True]]})
        other.recv_kind("ack")
        self.start_monitor()
        self.shows("Ansluten som mon. 3 värden i sessionen.")
        self.shows("I sessionen: A (fl)")
        other.recv_kind("peer_join")
        other.send({"t": "sets", "l": 2, "kv": [
            ["m/3/v", 0.64], ["c/1/p", -0.25], ["s/tempo", 128.0], ["g/1/2", "1000100010001000"],
            ["pc/3/Serum/12", 0.5], ["mx/3/1/m", 0.5], ["mr/3", "0:1.0,5:0.8"], ["s/play", True]]})
        for line in ("[A] Insert 3 volym 0.640", "[A] Kanal 2 pan -0.25", "[A] Tempo 128.0 BPM",
                     "[A] Mönster 1 steg 1000100010001000 (kanal 3)",
                     "[A] Plugin Serum (kanal 4) param 12 = 0.500", "[A] Insert 3 slot 2 mix 0.500",
                     "[A] Insert 3 routing → 0:1.0, 5:0.8", "[A] Uppspelning startad"):
            self.shows(line)
        self.type("state")
        self.shows("  Insert 2 volym 0.250")  # from the session the monitor joined
        self.shows("  Plugin Serum (kanal 4): 1 parameter")
        self.shows("värden till")  # s/play is left out, and so are defaults
        self.quit()

    def test_a_big_batch_is_counted_rather_than_printed(self):
        other = self.join("A")
        self.start_monitor()
        other.recv_kind("peer_join")
        other.send({"t": "sets", "l": 1, "kv": [["pc/1/Serum/%d" % n, 0.5] for n in range(200)]})
        self.shows("... och 160 ändringar till")
        self.type("state")
        self.shows("  Plugin Serum (kanal 2): 200 parametrar")
        self.quit()
        self.assertEqual(sum("[A] Plugin Serum (kanal 2) param" in line for line in self.out.lines), 40)

    def test_shows_who_comes_and_goes_and_quits_when_its_input_ends(self):
        self.start_monitor()
        self.shows("Ansluten som mon. 0 värden i sessionen.")
        self.shows("I sessionen: ingen annan")
        guest = self.join("Gäst 🎹", app="monitor")  # no Windows code page has 🎹
        self.shows("+ Gäst 🎹 anslöt (monitor)")
        guest.close()
        self.shows("- Gäst 🎹 lämnade")
        self.monitor.stdin.close()
        self.assertEqual(self.monitor.wait(5), 0)
        self.out.thread.join(5)
        self.assertNotIn("Traceback", self.out.text())

    def share(self, peer, name, fp, samples=()):
        """Play `peer`'s bridge sharing a version: upload the blobs, then snap_put."""
        files = self.join(peer.welcome["name"], app="files")
        project = b"FLhd" + name.encode("utf-8")  # the relay only checks hashes and sizes
        for data in (project,) + tuple(samples):
            h = hashlib.sha256(data).hexdigest()
            files.send({"t": "blob", "h": h, "i": 0, "n": 1, "d": base64.b64encode(data).decode("ascii")})
            self.assertEqual(files.recv(), {"t": "blob_ok", "h": h})
        peer.send({"t": "snap_put", "name": name, "fp": fp, "flp": hashlib.sha256(project).hexdigest(),
                   "size": len(project), "plugins": ["Serum"],
                   "samples": [{"h": hashlib.sha256(d).hexdigest(), "size": len(d), "path": "C:\\s\\%d.wav" % i}
                               for i, d in enumerate(samples)]})
        return peer.recv_kind("snapshot")

    def test_shows_new_versions_and_notes(self):
        other = self.join("A")
        self.start_monitor()
        other.recv_kind("peer_join")
        self.assertEqual(self.share(other, "Beat", "0123456789ab", [b"kick", b"snare"])["v"], 1)
        self.shows("* A delade version 1 av 'Beat' (2 samples)")
        self.share(other, "Beat", "0123456789ab", [b"kick"])
        self.shows("* A delade version 2 av 'Beat' (1 sample)")
        note = [0, 48, 60, 0.5, 0.5, 0.5, 0, 0.5, 0.5, 0, 0, 0, 0, 0]
        other.send({"t": "notes_put", "p": 2, "c": 4, "ppq": 96, "notes": [note, note], "fp": "0123456789ab"})
        self.shows("* A skickade 2 noter till kanal 5 i mönster 2")
        self.quit()

    def test_a_new_structure_starts_the_values_over(self):
        other = self.join("A")
        other.send({"t": "sets", "l": 1, "kv": [["m/2/v", 0.25]]})
        other.recv_kind("ack")
        self.start_monitor()
        self.shows("Ansluten som mon. 1 värden i sessionen.")
        other.recv_kind("peer_join")
        self.share(other, "Beat", "0123456789ab")  # A's first structure: the relay drops the old values
        self.assertEqual(other.recv(), {"t": "reshare"})
        self.shows("* A delade version 1 av 'Beat' (0 samples)")
        other.send({"t": "sets", "l": 2, "kv": [["m/3/v", 0.5]], "fp": "0123456789ab"})
        self.shows("[A] Insert 3 volym 0.500")
        self.type("state")
        self.shows("  Insert 3 volym 0.500")
        self.quit()
        self.assertNotIn("Insert 2 volym 0.250", self.out.text().split("[A] Insert 3 volym 0.500")[1])

    def test_shows_the_newest_version_when_it_joins(self):
        other = self.join("A")
        self.share(other, "Låt för två", "0123456789ab", [b"kick"])
        other.send({"t": "notes_put", "p": 1, "c": 0, "ppq": 96, "notes": [], "fp": "0123456789ab"})
        other.recv_kind("notes")
        self.start_monitor()
        self.shows("Senaste versionen av projektet: 1 av 'Låt för två' från A (1 sample)")
        self.shows("Sessionen har noter för 1 kanal")
        self.quit()

    def test_refuses_a_relay_that_speaks_another_protocol(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(10)
            self.start_monitor(server.getsockname()[1])
            conn, _ = server.accept()
            with conn, conn.makefile("rb") as incoming:
                conn.settimeout(5)
                hello = json.loads(incoming.readline())
                self.assertEqual((hello["t"], hello["proto"], hello["app"]), ("hello", PROTO, "monitor"))
                conn.sendall(b'{"t":"welcome","proto":0,"id":"c1","name":"mon","seed":false,'
                             b'"state":{},"peers":[]}\n')
                self.assertEqual(self.monitor.wait(5), 1)
        self.shows("Relayn kör protokoll 0 men monitorn 2")


class DescribeTest(unittest.TestCase):
    def test_every_kind_of_key_reads_as_swedish(self):
        cases = [  # numbered as FL and the FLSync script show them: channels and slots from 1
            ("m/3/v", 0.64, "Insert 3 volym 0.640"),
            ("m/0/p", -0.25, "Master pan -0.25"),
            ("m/3/mu", True, "Insert 3 mute på"),
            ("m/3/n", "Bas", 'Insert 3 namn "Bas"'),
            ("m/3/c", -10261391, "Insert 3 färg #636c71"),
            ("m/3/s", 0.5, "Insert 3 stereobredd 0.50"),
            ("m/3/e0g", 0.6, "Insert 3 EQ band 1 gain 0.600"),
            ("m/3/e1f", 0.25, "Insert 3 EQ band 2 frekvens 0.250"),
            ("m/3/e2w", 0.5, "Insert 3 EQ band 3 bredd 0.500"),
            ("m/3/fx", False, "Insert 3 effekter av"),
            ("mx/3/1/m", 0.5, "Insert 3 slot 2 mix 0.500"),
            ("mx/3/1/on", True, "Insert 3 slot 2 på"),
            ("mr/3", "0:1.0,5:0.8", "Insert 3 routing → 0:1.0, 5:0.8"),
            ("mr/0", "", "Master routing → ingenstans"),
            ("c/1/v", 0.7, "Kanal 2 volym 0.700"),
            ("c/1/p", -0.25, "Kanal 2 pan -0.25"),
            ("c/1/pi", -50, "Kanal 2 tonhöjd -50 cent"),
            ("c/1/mu", False, "Kanal 2 mute av"),
            ("c/1/n", "Kick", 'Kanal 2 namn "Kick"'),
            ("c/1/c", 0x123456, "Kanal 2 färg #123456"),
            ("c/1/t", 3, "Kanal 2 kopplad till Insert 3"),
            ("g/1/2", "1000100010001000", "Mönster 1 steg 1000100010001000 (kanal 3)"),
            ("pc/3/Serum/12", 0.5, "Plugin Serum (kanal 4) param 12 = 0.500"),
            ("pm/3/1/Fruity Limiter/5", 0.25, "Plugin Fruity Limiter (Insert 3 slot 2) param 5 = 0.250"),
            ("pc/0/Kontakt|Factory/1", 1, "Plugin Kontakt/Factory (kanal 1) param 1 = 1.000"),
            ("s/tempo", 128, "Tempo 128.0 BPM"),
            ("s/num", 3, "Taktart 3/4"),
            ("s/den", 8, "Taktart 4/8"),
            ("s/loop", 0, "Mönsterläge"),
            ("s/loop", 1, "Låtläge"),
            ("s/play", True, "Uppspelning startad"),
            ("s/play", False, "Uppspelning stoppad"),
            ("s/pos", 0.25, "Startposition 0.25 fjärdedelar"),
            ("s/pos", -1, "Ingen startposition (stoppad)"),
            ("pt/1/n", "Vers", 'Mönster 1 namn "Vers"'),
            ("pt/1/c", 0xFF0000, "Mönster 1 färg #ff0000"),
            ("pl/2/n", "Trummor", 'Spellistespår 2 namn "Trummor"'),
            ("pl/2/c", 255, "Spellistespår 2 färg #0000ff"),
            ("pl/2/mu", True, "Spellistespår 2 mute på"),
            ("gp/1/2", "0:60,100,64,120,64,128,128,0;4:62,80,64,120,64,128,128,0",
             "Mönster 1 stegparametrar (kanal 3): steg 1, 5"),
            ("gp/3/0", "", "Mönster 3 stegparametrar (kanal 1): inga steg på"),
            ("gp/1/0", ";".join("%d:60,100,64,120,-5,128,128,0" % n for n in range(10)),
             "Mönster 1 stegparametrar (kanal 1): steg 1, 2, 3, 4, 5, 6, 7, 8 och 2 till"),
            ("gp/1/2", "0:60,100", 'gp/1/2 = "0:60,100"'),
            ("gp/1/2", 5, "gp/1/2 = 5"),
            # what the monitor doesn't recognise is shown as it is
            ("zz/1", 2, "zz/1 = 2"),
            ("m/x/v", 0.3, "m/x/v = 0.3"),
            ("m/1/v", "hög", 'Insert 1 volym "hög"'),
            ("s/loop", 7, "s/loop = 7"),
            ("pc/1/Serum", 0.5, "pc/1/Serum = 0.5"),
            ("c/1/t", None, "c/1/t = null"),
        ]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(monitor_mod.describe(key, value, {"s/num": 4, "s/den": 4}), expected)


class Wire:
    """Stands in for the monitor's connection and keeps what it sends."""

    def __init__(self):
        self.sent = []

    def is_closing(self):
        return False

    def write(self, data):
        self.sent.append(json.loads(data))


class PendingTest(unittest.TestCase):
    """The monitor's side of the ordering rules, without a relay."""

    def setUp(self):
        self.monitor = monitor_mod.Monitor("127.0.0.1", 0, "mon")
        self.monitor.writer = self.wire = Wire()
        self.monitor.peers = {"c2": {"id": "c2", "name": "A", "app": "fl"}}

    def handle(self, msg):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.monitor._handle(msg)
        return out.getvalue()

    def test_own_changes_win_until_the_relay_has_handled_the_newest(self):
        m = self.monitor
        m.change("m/1/v", 0.5)
        m.change("m/1/v", 0.6)
        self.assertEqual(self.wire.sent, [{"t": "sets", "l": 1, "kv": [["m/1/v", 0.5]]},
                                          {"t": "sets", "l": 2, "kv": [["m/1/v", 0.6]]}])
        self.handle({"t": "sets", "from": "c2", "kv": [["m/1/v", 0.3], ["m/2/v", 0.4]], "s": 2})
        self.assertEqual(m.state, {"m/1/v": 0.6, "m/2/v": 0.4})  # A's value is older than ours
        self.assertEqual(m.unprinted, {"m/2/v": ("A", 0.4)})
        self.handle({"t": "ack", "l": 1, "s": 3})  # not our newest change to the key
        self.handle({"t": "sets", "from": "c2", "kv": [["m/1/v", 0.2]], "s": 4})
        self.assertEqual(m.state["m/1/v"], 0.6)
        self.handle({"t": "ack", "l": 2, "s": 5})
        self.handle({"t": "sets", "from": "c2", "kv": [["m/1/v", 0.1]], "s": 6})
        self.assertEqual(m.state["m/1/v"], 0.1)
        self.assertEqual(m.unprinted["m/1/v"], ("A", 0.1))
        self.assertEqual(m.pending, {})

    def test_a_snapshot_keeps_changes_still_on_the_way(self):
        m = self.monitor
        m.change("m/5/v", 0.1)
        self.handle({"t": "state", "state": {"m/5/v": 0.9, "m/6/v": 0.7}, "s": 9})
        self.assertEqual(m.state, {"m/5/v": 0.1, "m/6/v": 0.7})

    def test_a_version_with_another_structure_asks_for_the_session_again(self):
        m = self.monitor
        m.session_fp = "aaa"
        version = {"t": "snapshot", "v": 2, "name": "Beat", "fp": "aaa", "by": "A", "from": "c2", "samples": []}
        self.assertIn("* A delade version 2 av 'Beat' (0 samples)", self.handle(version))
        self.assertEqual(self.wire.sent, [])  # same structure: the session's values still hold
        self.handle(dict(version, v=3, fp="bbb"))
        self.assertEqual((self.wire.sent, m.session_fp), ([{"t": "sync"}], "bbb"))
        self.assertIn("* Kim skickade 1 not till kanal 1 i mönster 4",
                      self.handle({"t": "notes", "p": 4, "c": 0, "v": 1, "by": "Kim", "from": "c3", "ppq": 96,
                                   "notes": [[0] * 14]}))

    def test_reject_and_keep_end_the_wait(self):
        m = self.monitor
        m.change("x", [1])
        self.assertIn("! relayn avvisade x: ogiltigt värde",
                      self.handle({"t": "reject", "l": 1, "k": "x", "msg": "ogiltigt värde"}))
        self.assertEqual(m.pending, {})
        self.assertEqual(self.wire.sent[-1], {"t": "sync"})  # to get the session's value back
        self.handle({"t": "state", "state": {}, "s": 0})
        self.assertEqual(m.state, {})
        m.change("m/1/v", 0.5)
        self.handle({"t": "keep", "l": 2, "kv": [["m/1/v", 0.3]]})
        self.assertEqual((m.state, m.pending), ({"m/1/v": 0.3}, {}))


if __name__ == "__main__":
    unittest.main()
