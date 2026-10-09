"""Runs relay.py and monitor.py as real programs, the way you start them."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from fake_fl import ROOT, FakeFL
from harness import BridgeLink, FilesClient, LoopThread, bridge_mod, free_port, pump, relay_mod, sha256

RELAY = os.path.join(ROOT, "relay", "relay.py")
MONITOR = os.path.join(ROOT, "tools", "monitor.py")


def start(script, *args, **kwargs):
    return subprocess.Popen([sys.executable, script, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", **kwargs)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="flsync-store-")  # a killed relay can't remove its own

    def tearDown(self):
        shutil.rmtree(self.store, ignore_errors=True)

    def test_relay_monitor_and_fl_together(self):
        port = free_port()
        relay = start(RELAY, "--port", str(port), "--store", self.store)
        loop = LoopThread()
        fl = bridge = monitor = None
        try:
            self.assertIn("lyssnar", relay.stdout.readline())
            fl = FakeFL("A")
            bridge = BridgeLink(loop, port, fl)
            self.assertTrue(pump([fl], lambda: fl.client.joined and not fl.client.pending, 5))
            fl.set_volume(2, 0.25)
            self.assertTrue(pump([fl], lambda: not fl.client.pending, 3))

            monitor = start(MONITOR, "--port", str(port), stdin=subprocess.PIPE)
            monitor.stdin.write("vol 1 0.5\ntempo 128\nstate\n")
            monitor.stdin.flush()
            self.assertTrue(pump([fl], lambda: fl.volume(1) == 0.5 and fl.mixer.tempo == 128, 5),
                            "monitorn styrde inte FL")

            fl.set_volume(3, 0.75)
            pump([fl], lambda: False, 0.5)  # give the monitor time to print it
            monitor.stdin.write("quit\n")
            monitor.stdin.flush()
            out, _ = monitor.communicate(timeout=5)
        finally:
            if bridge is not None:
                bridge.stop()
            if fl is not None:
                fl.close()
            loop.stop()
            if monitor is not None and monitor.poll() is None:
                monitor.kill()
            relay.terminate()
            relay.wait(5)
            relay.stdout.close()

        self.assertEqual(monitor.returncode, 0)
        self.assertIn("Ansluten som monitor", out)
        self.assertIn("Insert 2 volym 0.250", out)  # from the session state at join
        self.assertIn("[A] Insert 3 volym 0.750", out)  # live change from FL

    def test_relay_keeps_blobs_in_its_store_up_to_its_limit(self):
        port = free_port()
        relay = start(RELAY, "--port", str(port), "--store", self.store, "--max-blob", "1K")
        try:
            self.assertIn("lyssnar", relay.stdout.readline())
            with FilesClient(port) as files:
                self.assertEqual(files.welcome["max_blob"], 1024)
                small, big = b"s" * 1024, b"b" * 1025
                self.assertEqual(files.upload(small), {"t": "blob_ok", "h": sha256(small)})
                answer = files.upload(big)
                self.assertEqual((answer["t"], answer["h"]), ("error", sha256(big)))
            self.assertEqual(os.listdir(self.store), [sha256(small)])
            with open(os.path.join(self.store, sha256(small)), "rb") as f:
                self.assertEqual(f.read(), small)
        finally:
            relay.terminate()
            relay.wait(5)
            relay.stdout.close()

    def test_programs_explain_their_options(self):
        # In this process: FL's own Python doesn't put a script's folder on the
        # path, so it can't run bridge.py (which imports its neighbours) as a program.
        for program, words in ((relay_mod, ("--store", "--max-blob")),
                               (bridge_mod, ("--project", "--projects-dir", "--auto-open"))):
            with self.subTest(program=program.__name__):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                    program.main(["--help"])
                for word in words:
                    self.assertIn(word, out.getvalue())


if __name__ == "__main__":
    unittest.main()
