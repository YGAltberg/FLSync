"""The relay on its own (protocol v2, docs/protocol_v2.md): a real relay on a
background event loop, and raw protocol clients over TCP. Snapshots, blobs,
structure fingerprints and notes are in test_snapshots.py.

    python -m unittest discover -s tests -p "test_relay*.py" -v
"""

import json
import socket
import time
import unittest

from harness import LoopThread, RawClient, RelayServer, relay_mod

PROTO = 2
OK = object()  # in a list of pairs: this one is accepted
Client = RawClient


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


class Silent:
    """A connection that doesn't start with a proper hello."""

    def __init__(self, port, first=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.file = self.sock.makefile("rb")
        if first is not None:
            self.sock.sendall(first)

    def recv(self):
        return json.loads(self.file.readline())

    def close(self):
        self.file.close()
        self.sock.close()


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.loop = LoopThread()
        self.loop_errors = []  # e.g. an exception in a connection handler
        self.loop.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.server = RelayServer(self.loop)
        self.port = self.server.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.stop()
        self.loop.stop()
        self.assertEqual([c.get("message") for c in self.loop_errors], [])

    @property
    def relay(self):
        return self.server.relay

    def join(self, name="raw", app="monitor"):
        client = Client(self.port, name, app)
        self.clients.append(client)
        return client

    def session(self, *who):
        """Clients that have joined in order and heard about each other.
        `who` are names, or (name, app)."""
        clients = [self.join(*(w if isinstance(w, tuple) else (w,))) for w in who]
        for client in clients:
            rest(client)
        return clients

    def without_text(self, messages):
        """The messages without their human-readable `msg`, which must be there."""
        for msg in messages:
            if msg["t"] in ("reject", "error"):
                self.assertTrue(isinstance(msg.get("msg"), str) and msg["msg"], msg)
        return [{k: v for k, v in msg.items() if k != "msg"} for msg in messages]

    # --- joining ------------------------------------------------------------

    def test_welcome_says_who_is_here_and_who_shares_the_project(self):
        m = self.join("Mon")
        self.assertEqual(m.welcome, {"t": "welcome", "proto": PROTO, "id": m.id, "name": "Mon",
                                     "seed": False, "state": {}, "peers": [], "s": 0,
                                     "fp": None, "snapshot": None, "notes": [], "pen": None})
        a = self.join("A", app="fl")
        self.assertTrue(a.welcome["seed"])  # the first FL shares its project
        self.assertEqual(a.welcome["peers"], [{"id": m.id, "name": "Mon", "app": "monitor"}])
        self.assertEqual(m.recv(), {"t": "peer_join", "id": a.id, "name": "A", "app": "fl"})

        b = self.join("A", app="fl")
        self.assertFalse(b.welcome["seed"])  # A shares, even though it hasn't yet
        self.assertEqual(b.welcome["name"], "A (2)")
        self.assertEqual([p["name"] for p in b.welcome["peers"]], ["Mon", "A"])
        self.assertEqual(rest(m), [{"t": "peer_join", "id": b.id, "name": "A (2)", "app": "fl"}])

        odd = self.join("Jonas 🎹" * 10, app="monitor")
        self.assertEqual(odd.welcome["name"], ("Jonas 🎹" * 10)[:40])
        nameless = self.join(None)
        self.assertEqual(nameless.welcome["name"], nameless.id)

        b.close()
        self.assertEqual(m.recv_kind("peer_leave"), {"t": "peer_leave", "id": b.id, "name": "A (2)",
                                                     "app": "fl"})

    def test_a_client_with_another_protocol_is_refused(self):
        m, = self.session("M")
        for proto in (0, 1, 3, None, "2", True, [2]):
            with self.subTest(proto=proto):
                old = RawClient(self.port, "Gammal", app="fl", proto=proto)
                self.assertEqual(old.welcome["t"], "error")
                self.assertIn("protokoll 2", old.welcome["msg"])
                self.assertTrue(closed(old))
                old.close()
        missing = Silent(self.port, b'{"t":"hello","name":"Gammal","app":"fl"}\n')
        self.assertEqual(missing.recv()["t"], "error")
        self.assertTrue(closed(missing))
        missing.close()
        self.assertEqual(rest(m), [])  # nobody heard them join
        self.assertEqual(list(self.relay.clients), [m.id])
        self.assertIsNone(self.relay.seeder)  # and none of them was asked to share
        self.assertTrue(self.join("Ny", app="fl").welcome["seed"])

    def test_the_first_message_must_be_hello(self):
        for first in (b'{"t":"sets","l":1,"kv":[["m/1/v",0.5]]}\n', b"inte json\n", b"[]\n"):
            with self.subTest(first=first):
                client = Silent(self.port, first)
                self.assertEqual(client.recv(), {"t": "error", "msg": "första meddelandet måste vara hello"})
                self.assertTrue(closed(client))
                client.close()
        self.assertEqual(self.relay.state, {})

    # --- batches --------------------------------------------------------------

    def test_every_accepted_pair_gets_the_next_number(self):
        a, b = self.session("A", "B")
        first = [["m/1/v", 0.5], ["m/2/v", 0.6], ["m/1/v", 0.7]]  # the later of two pairs wins
        a.send({"t": "sets", "l": 1, "kv": first})
        self.assertEqual(a.recv(), {"t": "ack", "l": 1, "s": 3})
        self.assertEqual(b.recv(), {"t": "sets", "from": a.id, "kv": first, "s": 3})

        second = [["c/0/n", "Kick"], ["s/play", True]]
        b.send({"t": "sets", "l": 1, "kv": second})
        self.assertEqual(b.recv(), {"t": "ack", "l": 1, "s": 5})
        self.assertEqual(a.recv(), {"t": "sets", "from": b.id, "kv": second, "s": 5})

        state = {"m/1/v": 0.7, "m/2/v": 0.6, "c/0/n": "Kick", "s/play": True}
        self.assertEqual((self.relay.state, self.relay.seq), (state, 5))
        late = self.join("C")
        self.assertEqual((late.welcome["state"], late.welcome["s"]), (state, 5))

    def test_numbers_are_global_and_everyone_sees_them_in_order(self):
        a, b, watcher = self.session("A", "B", "C")
        lines = {a: [], b: []}
        for n in range(1, 51):
            lines[a].append({"t": "sets", "l": n, "kv": [["a/%d" % n, n], ["x", "a%d" % n]]})
            lines[b].append({"t": "sets", "l": n, "kv": [["b/%d" % n, n], ["x", "b%d" % n], ["y", n]]})
        for client, batches in lines.items():  # all at once, so the relay interleaves them
            client.sock.sendall(b"".join((json.dumps(msg) + "\n").encode() for msg in batches))

        replica, last = {}, 0
        for _ in range(100):
            msg = watcher.recv()
            self.assertEqual(msg["t"], "sets")
            self.assertEqual(msg["s"], last + len(msg["kv"]))  # no gaps, no number twice
            last = msg["s"]
            replica.update(dict(msg["kv"]))
        self.assertEqual(last, 250)
        self.assertEqual(replica, self.relay.state)

        for client in (a, b):
            acks, last = [], 0
            for _ in range(100):  # its own 50 acks and the other's 50 batches
                msg = client.recv()
                self.assertGreater(msg["s"], last)
                last = msg["s"]
                if msg["t"] == "ack":
                    acks.append(msg["l"])
            self.assertEqual(acks, list(range(1, 51)))
            self.assertEqual(rest(client), [])

    def test_the_sender_gets_an_ack_and_everyone_else_the_changes(self):
        a, b, c = self.session("A", ("B", "fl"), "C")
        a.send({"t": "sets", "l": 7, "kv": [["m/1/v", 0.5]]})
        self.assertEqual(rest(a), [{"t": "ack", "l": 7, "s": 1}])  # no copy of its own change
        for other in (b, c):
            self.assertEqual(rest(other), [{"t": "sets", "from": a.id, "kv": [["m/1/v", 0.5]], "s": 1}])

    def test_a_single_set_is_a_batch_of_one_pair(self):
        a, b = self.session("A", "B")
        a.send({"t": "set", "k": "m/2/p", "v": -0.5, "l": 3})
        self.assertEqual(rest(a), [{"t": "ack", "l": 3, "s": 1}])
        self.assertEqual(rest(b), [{"t": "sets", "from": a.id, "kv": [["m/2/p", -0.5]], "s": 1}])

        a.send({"t": "set", "k": "m/2/n", "l": 4})  # no value
        a.send({"t": "set", "v": 1, "l": 5})        # no key
        a.send({"t": "set", "k": "m/2/n", "v": None, "l": 6})  # null is a value
        self.assertEqual(self.without_text(rest(a)), [
            {"t": "reject", "l": 4, "k": "m/2/n"}, {"t": "ack", "l": 4, "s": 1},
            {"t": "reject", "l": 5, "k": None}, {"t": "ack", "l": 5, "s": 1},
            {"t": "ack", "l": 6, "s": 2}])
        self.assertEqual(rest(b), [{"t": "sets", "from": a.id, "kv": [["m/2/n", None]], "s": 2}])

    def test_bad_pairs_are_rejected_and_the_rest_of_the_batch_applies(self):
        a, b = self.session("A", "B")
        cases = [  # (pair, OK or the key the reject names)
            (["m/1/v", 0.5], OK), (["", 1], ""), ([5, 1], 5), (["k" * 256, 1], OK),
            (["k" * 257, 1], "k" * 257), ([None, 1], None), (["nan", float("nan")], "nan"),
            (["text", "x" * 4096], OK), (["inf", float("-inf")], "inf"), (["long", "x" * 4097], "long"),
            (["none", None], OK), (["list", [1]], "list"), (["dict", {"a": 1}], "dict"),
            (["flag", False], OK), (["huge", 10 ** 400], "huge"), (["alone"], "alone"), ("pair", None),
            (["int", -3], OK), (["three", 1, 2], "three"), ([], None), ([["nested"], 1], None),
        ]
        a.send({"t": "sets", "l": 9, "kv": [pair for pair, _ in cases]})
        good = [pair for pair, outcome in cases if outcome is OK]
        rejects = [{"t": "reject", "l": 9, "k": outcome} for _, outcome in cases if outcome is not OK]
        self.assertEqual(self.without_text(rest(a)), rejects + [{"t": "ack", "l": 9, "s": len(good)}])
        self.assertEqual(rest(b), [{"t": "sets", "from": a.id, "kv": good, "s": len(good)}])
        self.assertEqual(self.relay.state, dict(good))

        a.send({"t": "sets", "l": 10, "kv": [["", 1], ["x", [2]]]})  # nothing valid at all
        self.assertEqual(self.without_text(rest(a)), [{"t": "reject", "l": 10, "k": ""},
                                                 {"t": "reject", "l": 10, "k": "x"},
                                                 {"t": "ack", "l": 10, "s": len(good)}])
        self.assertEqual(rest(b), [])  # so nothing for the others

    def test_a_batch_holds_at_most_256_pairs(self):
        a, b = self.session("A", "B")
        full = [["k/%d" % n, n] for n in range(256)]
        a.send({"t": "sets", "l": 1, "kv": full})
        self.assertEqual(rest(a), [{"t": "ack", "l": 1, "s": 256}])
        self.assertEqual(rest(b), [{"t": "sets", "from": a.id, "kv": full, "s": 256}])

        for kv in ([["x/%d" % n, n] for n in range(257)], [], None, {"x": 1}, "x"):
            with self.subTest(kv=str(kv)[:30]):
                a.send({"t": "sets", "l": 2, "kv": kv})
                self.assertEqual(self.without_text(rest(a)), [{"t": "error"}])  # no ack: none of it applied
        a.send({"t": "sets", "l": 3})
        self.assertEqual(self.without_text(rest(a)), [{"t": "error"}])
        self.assertEqual(rest(b), [])
        self.assertEqual((len(self.relay.state), self.relay.seq), (256, 256))

    # --- starting values --------------------------------------------------------

    def test_starting_values_never_overwrite_what_someone_changed(self):
        fl, m = self.session(("A", "fl"), "M")
        self.assertTrue(fl.welcome["seed"])
        m.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3]]})  # someone moves a fader while A shares
        self.assertEqual(rest(m), [{"t": "ack", "l": 1, "s": 1}])

        fl.send({"t": "sets", "l": 1, "seed": True,
                 "kv": [["m/1/v", 0.8], ["m/2/v", 0.8], ["", 0], ["m/3/v", 0.8]]})
        self.assertEqual(self.without_text(rest(fl)), [
            {"t": "sets", "from": m.id, "kv": [["m/1/v", 0.3]], "s": 1},
            {"t": "reject", "l": 1, "k": ""},              # rejects first,
            {"t": "keep", "l": 1, "kv": [["m/1/v", 0.3]]},  # then keep,
            {"t": "ack", "l": 1, "s": 3}])                  # then the ack
        self.assertEqual(rest(m), [{"t": "sets", "from": fl.id, "kv": [["m/2/v", 0.8], ["m/3/v", 0.8]],
                                    "s": 3}])
        self.assertEqual(self.relay.state, {"m/1/v": 0.3, "m/2/v": 0.8, "m/3/v": 0.8})

        # Starting values don't count as changes, so newer ones replace them...
        fl.send({"t": "sets", "l": 2, "seed": True, "kv": [["m/2/v", 0.5]]})
        # ...while a normal change always applies, and then the key is taken.
        fl.send({"t": "sets", "l": 3, "kv": [["m/2/v", 0.4]]})
        fl.send({"t": "set", "k": "m/2/v", "v": 0.9, "l": 4, "seed": True})
        fl.send({"t": "sets", "l": 5, "seed": True, "kv": [["m/1/v", 0.1], ["m/2/v", 0.1]]})
        self.assertEqual(rest(fl), [{"t": "ack", "l": 2, "s": 4}, {"t": "ack", "l": 3, "s": 5},
                                    {"t": "keep", "l": 4, "kv": [["m/2/v", 0.4]]}, {"t": "ack", "l": 4, "s": 5},
                                    {"t": "keep", "l": 5, "kv": [["m/1/v", 0.3], ["m/2/v", 0.4]]},
                                    {"t": "ack", "l": 5, "s": 5}])
        self.assertEqual(rest(m), [{"t": "sets", "from": fl.id, "kv": [["m/2/v", 0.5]], "s": 4},
                                   {"t": "sets", "from": fl.id, "kv": [["m/2/v", 0.4]], "s": 5}])
        self.assertEqual(self.relay.state, {"m/1/v": 0.3, "m/2/v": 0.4, "m/3/v": 0.8})

    def test_sharing_passes_on_if_the_sharer_leaves_first(self):
        a, b, m = self.session(("A", "fl"), ("B", "fl"), "M")
        self.assertEqual([c.welcome["seed"] for c in (a, b, m)], [True, False, False])
        a.close()
        leave = {"t": "peer_leave", "id": a.id, "name": "A", "app": "fl"}
        self.assertEqual(b.recv(), leave)
        self.assertEqual(b.recv(), {"t": "seed"})  # B shares instead
        self.assertEqual(m.recv(), leave)
        self.assertEqual(rest(m), [])  # monitors never share

        b.send({"t": "sets", "l": 1, "seed": True, "kv": [["m/1/v", 0.8]]})
        self.assertEqual(rest(b), [{"t": "ack", "l": 1, "s": 1}])
        c = self.join("C", app="fl")
        self.assertFalse(c.welcome["seed"])
        self.assertEqual(c.welcome["state"], {"m/1/v": 0.8})
        b.close()  # B has shared, so nobody needs to again
        self.assertEqual(c.recv(), {"t": "peer_leave", "id": b.id, "name": "B", "app": "fl"})
        self.assertEqual(rest(c), [])

    def test_the_next_fl_to_join_shares_if_the_sharer_left_alone(self):
        a, m = self.session(("A", "fl"), "M")
        a.close()
        self.assertEqual(m.recv()["t"], "peer_leave")
        self.assertEqual(rest(m), [])
        self.assertTrue(self.join("B", app="fl").welcome["seed"])

    # --- sync -------------------------------------------------------------------

    def test_sync_returns_the_whole_state(self):
        a, = self.session("A")
        a.send({"t": "sets", "l": 1, "kv": [["s/tempo", 128.0], ["pt/1/n", "Vers"]]})
        a.send({"t": "sync"})
        self.assertEqual(a.recv(), {"t": "ack", "l": 1, "s": 2})
        self.assertEqual(a.recv(), {"t": "state", "state": {"s/tempo": 128.0, "pt/1/n": "Vers"}, "s": 2})
        a.send({"t": "sync"})
        self.assertEqual(a.recv()["s"], 2)

    # --- robustness ---------------------------------------------------------------

    def test_garbage_gets_an_error_and_the_connection_keeps_working(self):
        a, b = self.session("A", "B")
        deep = b'{"t":"sets","l":1,"kv":' + b"[" * 100000 + b"]" * 100000 + b"}\n"
        a.sock.sendall(b"inte json\n[1, 2]\n\"text\"\n\xff\xfe\n\n" + deep +
                       b'{"t":"nonsense"}\n{"kv":[]}\n{"t":[[["deep"]]]}\n')
        errors = rest(a)
        self.assertEqual(self.without_text(errors), [{"t": "error"}] * 9)
        self.assertEqual([e["msg"] for e in errors[:6]], ["ogiltig JSON"] * 6)
        self.assertIn("okänd meddelandetyp", errors[6]["msg"])

        a.send({"t": "sets", "l": {"odd": [1]}, "kv": [["m/1/v", 0.5]]})  # a batch number we can't echo
        self.assertEqual(rest(a), [{"t": "ack", "l": None, "s": 1}])
        self.assertEqual(rest(b), [{"t": "sets", "from": a.id, "kv": [["m/1/v", 0.5]], "s": 1}])

    def test_an_over_long_line_drops_only_that_client(self):
        a, b = self.session("A", "B")
        try:
            a.sock.sendall(b'{"t":"set","k":"x","v":"' + b"x" * relay_mod.MAX_LINE + b'"}\n')
        except OSError:
            pass  # the relay may hang up before it has read all of it
        self.assertTrue(closed(a))
        self.assertEqual(b.recv(), {"t": "peer_leave", "id": a.id, "name": "A", "app": "monitor"})
        b.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.5]]})
        self.assertEqual(rest(b), [{"t": "ack", "l": 1, "s": 1}])
        self.assertEqual(self.join("C").welcome["state"], {"m/1/v": 0.5})

    def test_a_client_that_never_says_hello_is_dropped(self):
        saved, relay_mod.HELLO_TIMEOUT = relay_mod.HELLO_TIMEOUT, 0.2
        try:
            silent = Silent(self.port)
            self.assertTrue(closed(silent))
            silent.close()
        finally:
            relay_mod.HELLO_TIMEOUT = saved
        self.assertEqual(self.relay.clients, {})

    def test_stop_closes_every_connection(self):
        a, = self.session("A")
        silent = Silent(self.port)  # still hasn't said hello when the relay stops
        time.sleep(0.2)
        started = time.monotonic()
        self.server.stop()
        self.assertTrue(closed(a))
        self.assertTrue(closed(silent))
        silent.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.relay.clients, {})


if __name__ == "__main__":
    unittest.main()
