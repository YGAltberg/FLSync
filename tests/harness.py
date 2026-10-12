"""Test plumbing: a background event loop for the relay and the bridges, raw
protocol clients (session and files connections), and a loop that drives the
simulated FL instances."""

import asyncio
import base64
import collections
import hashlib
import json
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time

from fake_fl import ROOT, load_module

sys.path.insert(0, os.path.join(ROOT, "bridge"))  # bridge.py imports its siblings

relay_mod = load_module("relay_under_test", os.path.join(ROOT, "relay", "relay.py"))
relay_mod.log = lambda msg: None  # keep the test output readable
bridge_mod = load_module("bridge_under_test", os.path.join(ROOT, "bridge", "bridge.py"))

PROTO = 2
# Bridges in tests look at their files often, so tests don't wait for seconds.
FAST = {"watch_interval": 0.02, "project_interval": 0.02, "stable_time": 0.1, "share_retry": 0.3}


class LoopThread:
    """An asyncio event loop on a background thread, like a separate process."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        async def cancel_rest():
            rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in rest:
                task.cancel()
            await asyncio.gather(*rest, return_exceptions=True)
            # Windows reports cancelled socket operations a moment later; let it,
            # so no socket is left half-closed when the loop goes (Python 3.10).
            await asyncio.sleep(0.05)

        self.run(cancel_rest())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


class RelayServer:
    def __init__(self, loop_thread, port=0, **options):
        self.loop_thread = loop_thread
        self.port = port
        self.relay = relay_mod.Relay(**options)

    def start(self):
        self.port = self.loop_thread.run(self._bind())
        return self.port

    async def _bind(self):
        # Rebinding the same port right after a stop can fail briefly on Windows.
        for attempt in range(50):
            try:
                return await self.relay.start("127.0.0.1", self.port)
            except OSError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.1)

    def stop(self):
        self.loop_thread.run(self.relay.stop())


class BridgeLink:
    """A real Bridge on the background loop, wired to a FakeFL like one shared
    loopMIDI port: everything either side sends reaches both of them.

    With doubled=True FL hears every event twice, the second copy up to two
    events late, the way FL 2025 behaved with its input port opened twice.

    Everything the bridge keeps on disk goes to a temporary folder (`folder`),
    and it watches no project unless `mru` or `project` says so: the tests
    never touch the real %LOCALAPPDATA%, Documents or FL's recent-files list.
    `sent` collects the messages the bridge sends FL; `opened` what it opened.
    """

    def __init__(self, loop_thread, relay_port, fl, doubled=True, **options):
        self.loop_thread = loop_thread
        self.fl = fl
        self.logs = []
        self.sent = []
        self.opened = []
        self.folder = tempfile.mkdtemp(prefix="flsync-bridge-")
        self.places = {
            "mru": lambda: None,
            "mru_all": lambda: [],    # never the real FL's recent files ...
            "fl_title": lambda: None,  # ... or window title (a real FL may run meanwhile)
            "fl_open": lambda: getattr(fl, "window_open", False),  # FL's window: closed unless a test opens it
            "cache_dir": os.path.join(self.folder, "cache"),
            "projects_dir": os.path.join(self.folder, "Projects", "FLSync"),
            "notes_dir": os.path.join(self.folder, "notes"),
            "plugin_db": os.path.join(self.folder, "Plugin database", "Installed"),
            "fl_dirs": {"factory_dir": os.path.join(self.folder, "FL Studio 2025"),
                        "user_dir": os.path.join(self.folder, "Image-Line"),
                        "downloads_dir": os.path.join(self.folder, "Image-Line", "Downloads"),
                        "home_dir": os.path.join(self.folder, "home")},
            "opener": self.opened.append,
        }
        timing = {key: options.pop(key) for key in list(options) if key in FAST}
        self.places.update(options)
        lock = threading.Lock()
        late_copies = collections.deque()
        rng = random.Random(7)

        def port(message):  # called from the bridge's loop thread and from FL's
            with lock:
                fl.from_bridge.put(message)
                if doubled:
                    late_copies.append(message)
                    while len(late_copies) > rng.randint(0, 2):
                        fl.from_bridge.put(late_copies.popleft())
                self.bridge.incoming.append(message)

        async def create():
            self.bridge = bridge_mod.Bridge("127.0.0.1", relay_port, port, logger=self.logs.append,
                                            **self.places)
            for key, value in dict(FAST, **timing).items():
                setattr(self.bridge, key, value)
            to_fl = self.bridge.to_fl

            def spy(msg):
                self.sent.append(msg)
                to_fl(msg)

            self.bridge.to_fl = spy
            self.bridge.start()
            return asyncio.ensure_future(self.bridge.pump())

        self.task = loop_thread.run(create())
        fl.to_bridge = port

    def call(self, function, *args):
        """Run function(*args) on the bridge's loop and return its result."""
        async def run():
            return function(*args)

        return self.loop_thread.run(run())

    def stop(self):
        self.fl.to_bridge = None

        async def shutdown():
            self.task.cancel()
            transfers = list(self.bridge.tasks)
            self.bridge.close()
            if transfers:  # a cancelled download removes its temporary file first
                await asyncio.wait(transfers, timeout=5)
            await asyncio.sleep(0)

        self.loop_thread.run(shutdown())
        shutil.rmtree(self.folder, ignore_errors=True)


class RawClient:
    """Speaks the relay protocol directly over a blocking socket."""

    def __init__(self, port, name="raw", app="monitor", proto=PROTO, **hello):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rb")
        self.send(dict({"t": "hello", "proto": proto, "name": name, "app": app}, **hello))
        self.welcome = self.recv()

    @property
    def id(self):
        return self.welcome["id"]

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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FilesClient(RawClient):
    """A raw files connection: blob uploads and downloads."""

    def __init__(self, port, name="filer", proto=PROTO):
        super().__init__(port, name, app="files", proto=proto)

    def missing(self, hashes):
        self.send({"t": "blob_has", "h": list(hashes)})
        return self.recv()

    def upload(self, data, h=None, chunk=512 << 10):
        """Send `data` as blob `h` (its sha256 unless given) and return the answer."""
        h = h or sha256(data)
        count = max(1, -(-len(data) // chunk))
        for i in range(count):
            self.send({"t": "blob", "h": h, "i": i, "n": count,
                       "d": base64.b64encode(data[i * chunk:(i + 1) * chunk]).decode("ascii")})
        return self.recv()

    def download(self, h):
        """The blob's bytes, or the error message the relay answered with."""
        self.send({"t": "blob_get", "h": h})
        parts = []
        while True:
            msg = self.recv()
            if msg["t"] != "blob":
                return msg
            parts.append(base64.b64decode(msg["d"]))
            if msg["i"] == msg["n"] - 1:
                return b"".join(parts)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pump(fls, until, timeout=3.0):
    """Keep driving every FL until `until()` is true or time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        for fl in fls:
            fl.idle()
        if until():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.002)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
