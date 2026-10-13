#!/usr/bin/env python3
"""FLSync relay (protocol v2, see docs/protocol_v2.md).

One session per relay. Clients send their changes in batches (`sets`). Every
accepted pair gets the next global sequence number and is stored as the latest
value for its key. The sender gets a short `ack`, everyone else the accepted
pairs. The `s` in ack, sets, welcome and state is the number of the newest
accepted change at that point, so pair i of an n-pair `sets` has s - n + 1 + i.

Values only flow between FLs whose projects have the same structure: the relay
keeps the session's structure fingerprint (`fp`) and refuses batches made for
another one. The structure changes with project snapshots: a bridge uploads
the saved .flp and its samples as blobs over a second connection (`app:
"files"`), the relay keeps them on disk and tells everyone about the new
version. It also keeps the newest piano-roll notes per pattern and channel.

A share may name the version it builds on (`parent`): the relay refuses it if
another one was shared since, so a save never drops someone else's (the bridge
merges and tries again, docs/phase4.md). With `persist` the relay keeps its
versions in the store folder (`session.json`), so a restarted relay goes on
where it was.

    python relay/relay.py                 # only this computer can join
    python relay/relay.py --host 0.0.0.0  # others on your network can join
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import itertools
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import time

PROTO = 2
DEFAULT_PORT = 31415
MAX_LINE = 2 << 20       # longest accepted message (a blob chunk, or 20000 notes)
MAX_OUTBOX = 8 << 20     # a client this far behind gets disconnected
HELLO_TIMEOUT = 10.0
MAX_BATCH = 256          # pairs in one batch
MAX_KEY = 256
MAX_TEXT = 4096
MAX_FP = 64              # a structure fingerprint (FL sends 12 hex digits)
MAX_NAME = 256           # a snapshot's name, a plugin's name
MAX_LIST = 4096          # hashes in a blob_has, samples and plugins in a snapshot
CHUNK = 512 << 10        # the largest blob chunk
DEFAULT_MAX_BLOB = 256 << 20
MAX_NOTES = 20000        # notes in one notes_put
MAX_STORED_NOTES = 60000  # notes kept for all patterns and channels together; the oldest go first
NOTE_FIELDS = 14
MAX_NOTE_NUMBER = 1 << 31
MAX_MERGED = 10          # names in a snapshot's `merged` (whose versions it includes)
MAX_MERGED_NAME = 40     # ...each as long as a participant's name can be
IDLE_TIMEOUT = 60.0      # a client that pings and then stays this quiet is gone
SESSION_FILE = "session.json"  # with persist: the versions, in the store folder
SESSION_FORMAT = 1
KEEP_VERSIONS = 20       # ...the older versions kept there besides the newest
APPS = ("fl", "monitor")
FILES = "files"          # the app of a bridge's files connection
NOTE_KINDS = ("unrelated",)  # what a `note` may tell the others
# Which part of FL's structure fingerprint a key depends on (docs/protocol_v2.md,
# "Structure"): its 12 hex digits are the channels' 6, then the mixer's 6. Keys
# of other prefixes (song, transport, playlist tracks, patterns) depend on neither.
KEY_AREAS = {"c": "c", "g": "c", "gp": "c", "pc": "c", "m": "m", "mx": "m", "mr": "m", "pm": "m"}
AREA_PARTS = {"c": slice(0, 6), "m": slice(6, 12)}
HASH = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()      # the value of a single set that has none


def log(msg):
    try:
        print(time.strftime("%H:%M:%S ") + msg, flush=True)
    except OSError:
        pass  # nobody reads the output any more (a closed pipe); the session goes on


def encode(msg):
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def parse(line):
    try:
        msg = json.loads(line)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or absurdly deeply nested
        return None
    return msg if isinstance(msg, dict) else None


def valid_key(key):
    return isinstance(key, str) and 0 < len(key) <= MAX_KEY


def valid_value(value):
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(value)
        except OverflowError:  # an integer too big for a float
            return False
    return isinstance(value, str) and len(value) <= MAX_TEXT


def valid_fp(fp):
    return isinstance(fp, str) and 0 < len(fp) <= MAX_FP


def key_area(key):
    """The part of the structure a key's thing is found by ("c" channels, "m"
    the mixer), or None: it means the same in every structure."""
    return KEY_AREAS.get(key.split("/", 1)[0]) if isinstance(key, str) else None


def parts_differ(a, b, area):
    """Whether structures `a` and `b` differ where keys of `area` point. Unknown
    on either side never counts as another; a fingerprint that isn't FL's two
    parts differs as a whole."""
    if a is None or b is None or a == b or area is None:
        return False
    if len(a) != 12 or len(b) != 12:
        return True
    part = AREA_PARTS[area]
    return a[part] != b[part]


def valid_hash(h):
    return isinstance(h, str) and HASH.match(h) is not None


def is_int(value, low=0, high=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def valid_note(note):
    """[time, length, number, velocity, pan, release, color, fcut, fres, pitchofs,
    slide, porta, muted, repeats]: numbers (booleans as 0/1)."""
    if not isinstance(note, list) or len(note) != NOTE_FIELDS:
        return False
    for field in note:
        if not isinstance(field, (int, float)) or not valid_value(field) or abs(field) > MAX_NOTE_NUMBER:
            return False
    return True


def pair_problem(pair):
    """Why one [key, value] pair of a batch can't be accepted, or None."""
    if not isinstance(pair, list) or len(pair) != 2:
        return "ett par ska vara [nyckel, värde]"
    if not valid_key(pair[0]):
        return "ogiltig nyckel"
    if not valid_value(pair[1]):
        return "ogiltigt värde"
    return None


def echo(value):
    """Something a client sent, if it is safe to send back (a JSON scalar), else None."""
    return value if valid_value(value) else None


def shown(value):
    """A short text for logs and error messages about something a client sent."""
    return ("%r" % (value,))[:40] if valid_value(value) else type(value).__name__


def hello_name(hello, fallback):
    name = hello.get("name")
    return name[:40] if isinstance(name, str) and name else fallback


def megabytes(size):
    return "%.1f MB" % (size / 1e6)


class Client:
    def __init__(self, cid, name, app, writer, fp=None, flv=None, ver=None):
        self.id = cid
        self.name = name
        self.app = app
        self.writer = writer
        self.fp = fp          # its project's structure fingerprint (FLs say it)
        self.flv = flv        # its FL's version text (FLs say it)
        self.ver = ver        # its FLSync's version (the launcher's bridge says it)
        self.upload = None    # files connections: the blob being uploaded
        self.failed = None    # ...and (h, n) of one whose remaining chunks are ignored after an error
        self.heard = time.monotonic()  # when it last sent anything
        self.pinged = False   # it keeps its connection alive with `ping`, so silence means it's gone

    def info(self):
        info = {"id": self.id, "name": self.name, "app": self.app}
        if self.flv:
            info["flv"] = self.flv
        if self.ver:
            info["ver"] = self.ver
        return info


class Upload:
    """One blob arriving in chunks. It goes to a temporary file and only gets
    its name (the sha256) once the whole of it has the right hash."""

    def __init__(self, root, h, n):
        self.h, self.n = h, n
        self.next = 0
        self.size = 0
        self.hash = hashlib.sha256()
        fd, self.temp = tempfile.mkstemp(dir=root, suffix=".part")
        self.file = os.fdopen(fd, "wb")

    def add(self, data):
        self.file.write(data)
        self.hash.update(data)
        self.size += len(data)
        self.next += 1

    def discard(self):
        self.file.close()
        try:
            os.remove(self.temp)
        except OSError:
            pass

    def finish(self, path):
        """True if the blob checked out and is now at `path`."""
        self.file.close()
        if self.hash.hexdigest() != self.h or os.path.exists(path):  # wrong, or someone was faster
            self.discard()
            return self.hash.hexdigest() == self.h
        os.replace(self.temp, path)
        return True


class BlobStore:
    """Blobs on disk, each named by the sha256 of its content."""

    def __init__(self, root=None):
        self.owned = root is None  # a fresh temporary folder is removed again on close()
        self.root = tempfile.mkdtemp(prefix="flsync-relay-") if root is None else os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        for name in os.listdir(self.root):
            if name.endswith(".part"):  # an upload a stopped relay left behind
                try:
                    os.remove(os.path.join(self.root, name))
                except OSError:
                    pass

    def path(self, h):
        return os.path.join(self.root, h)

    def size(self, h):
        """The blob's size, or None if the store doesn't have it."""
        if not valid_hash(h):
            return None
        try:
            return os.path.getsize(self.path(h))
        except OSError:
            return None

    def close(self):
        if self.owned:
            shutil.rmtree(self.root, ignore_errors=True)


class Relay:
    def __init__(self, verbose=False, store=None, max_blob=DEFAULT_MAX_BLOB, password=None, persist=False):
        if persist and not store:
            # A temporary folder goes when the relay stops: there would be nothing to read back.
            raise ValueError("persist kräver en egen mapp att spara versionerna i (store)")
        self.verbose = verbose
        self.password = password or None  # with one, a hello must bring it (`pw`)
        self.pen = None       # the client id of the FL changing the structure (docs/phase3.md)
        self.clients = {}     # the session's peers: FLs and monitors
        self.files = {}       # files connections, which aren't peers
        self.state = {}
        self.seq = 0          # number of the newest accepted change (one per pair)
        self.seeder = None    # the FL asked to share its project as starting values
        self.seeded = False   # has it done so? (changes from a monitor don't count)
        self.touched = set()  # keys someone has really changed; starting values never overwrite these
        self.fp = None        # the session's structure fingerprint
        self.snapshot = None  # the newest snapshot (as in a `snapshot` message, without "t")
        self.version = 0      # its number
        self.notes = {}       # (pattern, channel) -> the newest notes for it, oldest first
        self.notes_version = 0
        self.max_blob = max_blob
        self.store_dir = store
        self.store = None
        self.persist = persist  # keep the versions in the store folder across restarts
        self.kept = []        # with persist: the newest accepted snapshots, newest first (session.json)
        self.idle_timeout = IDLE_TIMEOUT
        self._ids = itertools.count(1)
        self._file_ids = itertools.count(1)
        self._server = None
        self._tasks = set()
        self._watcher = None  # the task that drops clients gone quiet

    async def start(self, host="127.0.0.1", port=DEFAULT_PORT):
        if self.store is None:
            self.store = BlobStore(self.store_dir)
            if self.persist:
                self._load_session()  # before anyone can join, so no blob goes while someone needs it
        self._server = await asyncio.start_server(self._serve, host, port, limit=MAX_LINE)
        self._watcher = asyncio.get_running_loop().create_task(self._watch_idle())
        return self._server.sockets[0].getsockname()[1]

    async def serve_forever(self):
        await self._server.serve_forever()

    async def stop(self):
        if self._server is None:
            return
        self._server.close()
        waiting = list(self._tasks)
        if self._watcher is not None:
            self._watcher.cancel()
            waiting.append(self._watcher)
            self._watcher = None
        for client in list(self.clients.values()) + list(self.files.values()):
            client.writer.close()  # each handler then sees EOF and finishes by itself
        if waiting:
            _, stuck = await asyncio.wait(waiting, timeout=2)
            for task in stuck:
                task.cancel()
        try:
            await asyncio.wait_for(self._server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass
        self._server = None
        self.close_store()

    def close_store(self):
        if self.store is not None:
            self.store.close()

    # --- one connection -------------------------------------------------

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        client = None
        try:
            hello = parse(await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT))
            if not hello or hello.get("t") != "hello":
                writer.write(encode({"t": "error", "msg": "första meddelandet måste vara hello"}))
                return
            proto = hello.get("proto")
            if proto != PROTO or isinstance(proto, bool):
                theirs = "anger inget protokoll" if proto is None else "kör protokoll %s" % shown(proto)
                writer.write(encode({"t": "error", "code": "proto",
                                     "msg": "relayn kör protokoll %d men klienten %s. "
                                     "Uppdatera så att de matchar." % (PROTO, theirs)}))
                log("%s nekades: klienten %s, relayn kör %d" % (hello_name(hello, "En klient"),
                                                               theirs, PROTO))
                return
            if not self._let_in(hello):
                writer.write(encode({"t": "error", "code": "password",
                                     "msg": "fel lösenord för sessionen: be om en ny inbjudan"}))
                log("%s nekades: fel lösenord" % hello_name(hello, "En klient"))
                return
            if hello.get("app") == FILES:
                client = self._open_files(hello, writer)
                await self._serve_files(client, reader)
                return
            client = self._join(hello, writer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                client.heard = time.monotonic()  # anything at all shows it's still there
                msg = parse(line)
                if msg is None:
                    self._send(client, {"t": "error", "msg": "ogiltig JSON"})
                else:
                    self._dispatch(client, msg)
        except (asyncio.TimeoutError, OSError, ValueError):
            pass  # silent client, lost connection or an over-long line: drop it
        except asyncio.CancelledError:
            pass  # shutting down; ending normally keeps Python 3.12.1's stream callback quiet
        finally:
            if client is not None:
                if client.app == FILES:
                    self._close_files(client)
                else:
                    self._leave(client)
            writer.close()
            try:
                # Gone for good before the handler ends: stop() then leaves no half-closed sockets.
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (asyncio.CancelledError, asyncio.TimeoutError, OSError):
                pass
            self._tasks.discard(task)

    async def _watch_idle(self):
        """Drop session clients that ping and then fall silent. A computer that
        slept or lost its network leaves a connection nobody reads, and its
        client would keep its place, its name and maybe the pen. Clients that
        never ping (monitors, older bridges) may be quiet as long as they like."""
        while True:
            await asyncio.sleep(max(0.01, min(self.idle_timeout / 4, 5.0)))
            now = time.monotonic()
            for client in list(self.clients.values()):
                if client.pinged and now - client.heard > self.idle_timeout and not client.writer.is_closing():
                    log("%s har inte hörts av på länge och kopplas från" % client.name)
                    client.writer.transport.abort()  # close() would wait for a buffer nobody reads

    def _let_in(self, hello):
        """Whether a hello may join: it brings the session's password, if there is one."""
        if self.password is None:
            return True
        given = hello.get("pw")
        return isinstance(given, str) and hmac.compare_digest(given.encode("utf-8"),
                                                             self.password.encode("utf-8"))

    def _join(self, hello, writer):
        cid = "c%d" % next(self._ids)
        app = hello.get("app") if hello.get("app") in APPS else "fl"
        name = self._unique_name(hello_name(hello, cid))
        fp = hello.get("fp") if valid_fp(hello.get("fp")) else None
        flv = hello.get("flv") if isinstance(hello.get("flv"), str) and 0 < len(hello.get("flv")) <= 40 else None
        ver = hello.get("ver") if isinstance(hello.get("ver"), str) and 0 < len(hello.get("ver")) <= 20 else None
        client = Client(cid, name, app, writer, fp, flv, ver)
        if app == "fl" and self._differs(fp) and not self._other_fls(client):
            # Alone with another structure (it opened another project while
            # nobody else was here): that structure becomes the session's.
            self._restructure(fp, "%s anslöt ensam med ett annat projekt" % name)
        seed = app == "fl" and not self.seeded and self.seeder is None and not self._differs(fp)
        if seed:
            self.seeder = cid
            if self.fp is None:
                self.fp = fp  # the first FL to share its project sets the session's structure
        peers = [c.info() for c in self.clients.values()]
        self.clients[cid] = client
        self._send(client, {"t": "welcome", "proto": PROTO, "id": cid, "name": name, "seed": seed,
                            "state": self.state, "peers": peers, "s": self.seq, "fp": self.fp,
                            "snapshot": self.snapshot, "notes": list(self.notes.values()),
                            "pen": self._pen_info()})
        self._broadcast(dict(client.info(), t="peer_join"), skip=cid)
        details = app if app != "fl" else "FL, API %s" % shown(hello.get("api"))
        log("%s anslöt (%s), %d i sessionen" % (name, details, len(self.clients)))
        if seed:
            log("Sessionen har inga värden än, så %s delar sina inställningar" % name)
        elif app == "fl" and self._differs(fp):
            log("%s har ett projekt med en annan struktur än sessionens" % name)
        return client

    def _leave(self, client):
        self.clients.pop(client.id, None)
        self._broadcast(dict(client.info(), t="peer_leave"))
        log("%s lämnade, %d kvar" % (client.name, len(self.clients)))
        if self.pen == client.id:
            self._free_pen()
        if self.seeder == client.id:
            self.seeder = None
            if not self.seeded:
                self._hand_over()  # they left before sharing their project: ask the next FL instead
                return
        if client.app == "fl":
            self._adopt_lone_fl()

    def _adopt_lone_fl(self):
        """An FL left alone with another structure (the others had moved on to a
        version it hasn't opened): nobody else's values are at stake, so its
        structure becomes the session's, as for an FL that is alone from the start."""
        fls = [c for c in self.clients.values() if c.app == "fl"]
        if len(fls) == 1 and self._differs(fls[0].fp):
            other = fls[0]
            self._restructure(other.fp, "%s är ensam kvar med ett annat projekt" % other.name)
            self.seeder = other.id
            self._send(other, {"t": "reshare"})
            self._check_pen()

    def _hand_over(self):
        fls = [c for c in self.clients.values() if c.app == "fl"]
        same = [c for c in fls if not self._differs(c.fp)]
        if same:
            other = same[0]
            self.seeder = other.id
            self._send(other, {"t": "seed"})
            log("%s delar sina inställningar i stället" % other.name)
        elif len(fls) == 1:
            # The only FL left has another structure: it becomes the session's.
            other = fls[0]
            self._restructure(other.fp, "%s är ensam kvar med ett annat projekt" % other.name)
            self.seeder = other.id
            self._send(other, {"t": "reshare"})
        elif fls:
            log("Ingen av de kvarvarande har sessionens projektstruktur; den som sparar först bestämmer")

    def _dispatch(self, client, msg):
        kind = msg.get("t")
        if kind == "sets":
            self._batch(client, msg.get("l"), msg.get("kv"), msg.get("seed"), msg.get("fp"))
        elif kind == "set":
            # The single change of simple tools: a batch of one pair.
            self._batch(client, msg.get("l"), [[msg.get("k"), msg.get("v", _MISSING)]], msg.get("seed"),
                        msg.get("fp"))
        elif kind == "sync":
            self._send(client, {"t": "state", "state": self.state, "s": self.seq})
        elif kind == "fp":
            self._new_fp(client, msg.get("fp", _MISSING), msg.get("load") is True)
        elif kind == "snap_put":
            self._snap_put(client, msg)
        elif kind == "notes_put":
            self._notes_put(client, msg)
        elif kind == "note":
            self._note(client, msg)
        elif kind == "ping":
            client.pinged = True  # from now on, a long silence means it's gone (_watch_idle)
            self._send(client, {"t": "pong"})
        else:
            self._send(client, {"t": "error", "msg": "okänd meddelandetyp: %s" % shown(kind)})

    def _batch(self, client, number, pairs, seed, fp=None):
        """Apply one batch, then answer in the protocol's order: rejects, keep,
        ack to the sender, and the accepted pairs to everyone else."""
        if not isinstance(pairs, list) or not pairs:
            self._send(client, {"t": "error", "msg": "kv ska vara en lista med 1-%d par" % MAX_BATCH})
            return
        if len(pairs) > MAX_BATCH:
            self._send(client, {"t": "error", "msg": "för många par i en sats: %d (högst %d)"
                                % (len(pairs), MAX_BATCH)})
            return
        number = echo(number)
        stale = []
        if self._differs(fp):
            # Made for another structure: keys of the part that differs would land
            # on the wrong things. The others (tempo, or the mixer when only the
            # channels differ) mean the same, and are taken.
            stale = [pair for pair in pairs if isinstance(pair, list) and pair
                     and parts_differ(fp, self.fp, key_area(pair[0]))]
            if len(stale) == len(pairs):
                self._send(client, {"t": "ack", "l": number, "s": self.seq, "stale": True})
                if self.verbose:
                    log("%s: en sats för en annan projektstruktur togs inte emot" % client.name)
                return
            pairs = [pair for pair in pairs if pair not in stale]
        rejects, kept, accepted = [], [], []
        for pair in pairs:
            problem = pair_problem(pair)
            if problem:
                key = pair[0] if isinstance(pair, list) and pair else None
                rejects.append({"t": "reject", "l": number, "k": echo(key), "msg": problem})
            elif seed and pair[0] in self.touched:
                # A starting value that arrived after someone changed the key:
                # the change wins, and the sharer gets the session's value back.
                kept.append([pair[0], self.state[pair[0]]])
            else:
                key, value = pair
                if not seed:
                    self.touched.add(key)
                self.state[key] = value
                self.seq += 1
                accepted.append(pair)
        if client.id == self.seeder and (accepted or kept):
            self.seeded = True
        for reject in rejects:
            self._send(client, reject)
        if kept:
            self._send(client, {"t": "keep", "l": number, "kv": kept})
        ack = {"t": "ack", "l": number, "s": self.seq}
        if stale:
            ack["stale"] = [echo(pair[0]) for pair in stale]  # these keys were refused, the rest taken
        self._send(client, ack)
        if accepted:
            self._broadcast({"t": "sets", "from": client.id, "kv": accepted, "s": self.seq},
                            skip=client.id)
        if self.verbose:
            for key, value in accepted:
                log("%s: %s = %s" % (client.name, key, shown(value)))
            for reject in rejects:
                log("%s: avvisade %s (%s)" % (client.name, shown(reject["k"]), reject["msg"]))
            if kept:
                log("%s: %d startvärden ersattes av sessionens" % (client.name, len(kept)))

    # --- structure ------------------------------------------------------------

    def _differs(self, fp):
        """Is `fp` (a client's structure, or the one a batch was made for) another
        than the session's? Unknown on either side never counts as another."""
        return fp is not None and self.fp is not None and fp != self.fp

    def _other_fls(self, client):
        return any(c.app == "fl" and c.id != client.id for c in self.clients.values())

    def _new_fp(self, client, fp, load=False):
        if fp is not None and not valid_fp(fp):
            self._send(client, {"t": "error", "msg": "fp ska vara en text på högst %d tecken" % MAX_FP})
            return
        client.fp = fp
        if self.verbose:
            log("%s: projektets struktur är nu %s" % (client.name, fp))
        if client.app == "fl" and self._differs(fp) and not self._other_fls(client):
            # Alone: it changed its structure or loaded another project, and
            # nobody else's values are at stake.
            self._restructure(fp, "%s ändrade sitt projekts struktur och är ensam" % client.name)
            self.seeder = client.id
            self._send(client, {"t": "reshare"})
            self._check_pen()
        elif client.app == "fl":
            self._update_pen(client, load)

    def _restructure(self, fp, why, keep=True):
        """The session gets another structure. Values and notes for the part
        that changed would land on the wrong things, so they go (all of them
        when not `keep`: another project); whoever brought the new structure
        shares its values again."""
        old, self.fp = self.fp, fp
        keep = keep and old is not None  # values made for an unknown structure can't be told apart
        if keep:
            self.state = {k: v for k, v in self.state.items() if not parts_differ(old, fp, key_area(k))}
            self.touched = {k for k in self.touched if k in self.state}
        else:
            self.state, self.touched = {}, set()
        if not keep or parts_differ(old, fp, "c"):
            self.notes = {}  # they are a channel's
        self.seeded, self.seeder = False, None
        if self.snapshot is not None and self.snapshot.get("fp") != fp:
            self.snapshot = None  # a version of another structure: not one to hand to newcomers
        log("%s: sessionens värden börjar om med den strukturen" % why)

    # --- the pen: one structure change at a time ----------------------------------

    def _pen_info(self):
        holder = self.clients.get(self.pen)
        return {"id": holder.id, "name": holder.name} if holder else None

    def _update_pen(self, client, load=False):
        """An FL among others reported its structure: it takes the pen when it
        starts changing the structure and nobody has it, and gives it back when
        its structure is the session's again. A structure that comes from
        opening a project (`load`) isn't a change in the making: no pen."""
        if self.pen is not None and self.pen not in self.clients:
            self.pen = None  # its holder left without giving it back
        if self._differs(client.fp):
            if load:
                return
            if self.pen is None:
                self.pen = client.id
                self._broadcast({"t": "pen", "id": client.id, "name": client.name})
                log("%s ändrar projektets struktur" % client.name)
            elif self.pen != client.id:
                holder = self.clients[self.pen]
                self._send(client, {"t": "pen", "id": holder.id, "name": holder.name, "busy": True})
        elif self.pen == client.id:
            self._free_pen()

    def _free_pen(self):
        self.pen = None
        self._broadcast({"t": "pen", "id": None})

    def _check_pen(self):
        """Free the pen when its holder is gone or its structure is the session's now."""
        holder = self.clients.get(self.pen)
        if self.pen is not None and (holder is None or not self._differs(holder.fp)):
            self._free_pen()

    # --- snapshots and notes ----------------------------------------------------

    def _snap_put(self, client, msg):
        snapshot, problem = self._check_snapshot(msg)
        if problem:
            self._send(client, {"t": "error", "msg": "versionen togs inte emot: %s" % problem})
            return
        # The newest version is the one newcomers get: a version dropped for another
        # structure (_restructure) counts as none, so nobody is told to merge with
        # a version the session has left behind.
        newest = self.snapshot["v"] if self.snapshot is not None else 0
        if "parent" in snapshot and snapshot["parent"] != newest:
            # Built on an older version: taking it would drop what was shared since.
            # The sharer merges with the newest and tries again.
            self._send(client, {"t": "error", "code": "outdated", "v": newest,
                                "msg": "en nyare version har delats, slå ihop med den först"})
            built_on = "version %d" % snapshot["parent"] if snapshot["parent"] else "ingen tidigare version"
            log("%s delade en version som bygger på %s, men sessionen har %s: den togs inte emot"
                % (client.name, built_on, "version %d" % newest if newest else "ingen version"))
            return
        self.version += 1
        snapshot = dict(v=self.version, **snapshot)
        snapshot.update({"by": client.name, "from": client.id})
        self.snapshot = snapshot
        if self.persist:
            # On disk before anyone hears of it: a restarted relay never hands out its number again.
            self.kept = [snapshot] + self.kept[:KEEP_VERSIONS]
            self._write_session()
        changed = snapshot["fp"] != self.fp or snapshot.get("switch")
        if changed:
            what = "bytte sessionens projekt" if snapshot.get("switch") else "delade ett projekt med en ny struktur"
            self._restructure(snapshot["fp"], "%s %s" % (client.name, what), keep=not snapshot.get("switch"))
            if client.app == "fl":
                self.seeder = client.id  # its `reshare` brings the values
        self._broadcast(dict({"t": "snapshot"}, **snapshot))
        if changed:
            self._send(client, {"t": "reshare"})
        self._check_pen()
        size = snapshot["size"] + sum(s["size"] for s in snapshot["samples"])
        count = len(snapshot["samples"])
        merged = snapshot.get("merged")
        with_whom = ", sammanslagen med %s" % ", ".join(merged) if merged else ""
        log("%s delade version %d av '%s' (%d %s, %s)%s" % (client.name, self.version, snapshot["name"], count,
                                                          "sample" if count == 1 else "samples", megabytes(size),
                                                          with_whom))

    def _check_snapshot(self, msg):
        """A clean copy of a snap_put's snapshot, or why it can't be taken."""
        name, fp, flp, size = msg.get("name"), msg.get("fp"), msg.get("flp"), msg.get("size")
        samples, plugins = msg.get("samples"), msg.get("plugins")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
            return None, "name ska vara en text på högst %d tecken" % MAX_NAME
        if not valid_fp(fp):
            return None, "fp saknas"
        if not valid_hash(flp) or not is_int(size):
            return None, "flp ska vara projektfilens sha256 och size dess storlek"
        if self.store.size(flp) != size:
            return None, "relayn har inte projektfilen (ladda upp den först)"
        if not isinstance(samples, list) or len(samples) > MAX_LIST:
            return None, "samples ska vara en lista med högst %d samples" % MAX_LIST
        clean = []
        for sample in samples:
            if not isinstance(sample, dict):
                return None, "en sample ska vara {h, size, path}"
            h, sample_size, path = sample.get("h"), sample.get("size"), sample.get("path")
            if not valid_hash(h) or not is_int(sample_size) or not isinstance(path, str) \
                    or not 0 < len(path) <= MAX_TEXT:
                return None, "en sample ska vara {h, size, path}"
            if self.store.size(h) != sample_size:
                return None, "relayn har inte samplen %s (ladda upp den först)" % shown(path)
            clean.append({"h": h, "size": sample_size, "path": path})
            if sample.get("plugin") is True:
                clean[-1]["plugin"] = True  # a plugin's sample: it goes next to the project, by its name
        if not isinstance(plugins, list) or len(plugins) > MAX_LIST or not all(
                isinstance(p, str) and 0 < len(p) <= MAX_NAME for p in plugins):
            return None, "plugins ska vara en lista med pluginnamn"
        # Optional (docs/phase4.md); null counts as not given, as for a batch's fp.
        parent, merged = msg.get("parent"), msg.get("merged")
        if parent is not None and not is_int(parent):
            return None, "parent ska vara ett versionsnummer"
        if merged is not None and (not isinstance(merged, list) or len(merged) > MAX_MERGED or not all(
                isinstance(m, str) and 0 < len(m) <= MAX_MERGED_NAME for m in merged)):
            return None, "merged ska vara en lista med högst %d namn på högst %d tecken" % (MAX_MERGED,
                                                                                         MAX_MERGED_NAME)
        snapshot = {"name": name, "fp": fp, "flp": flp, "size": size, "samples": clean, "plugins": list(plugins)}
        flv = msg.get("flv")
        if isinstance(flv, str) and 0 < len(flv) <= 40:
            snapshot["flv"] = flv  # the FL version that saved it (optional, like in hello)
        if parent is not None:
            snapshot["parent"] = parent  # the newest version it includes, 0 for none
        if merged is not None:
            snapshot["merged"] = list(merged)  # whose versions it includes besides the sharer's own
        if msg.get("switch") is True:
            snapshot["switch"] = True  # another project than the session's: nobody merges it with theirs
        return snapshot, None

    def _note(self, client, msg):
        """Something the others should hear about an FL (docs/protocol_v2.md):
        passed on, never kept. `unrelated`: it saved a project that isn't the
        session's, which isn't shared unless it saves again soon."""
        kind, name = msg.get("kind"), msg.get("name")
        if kind not in NOTE_KINDS or not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
            self._send(client, {"t": "error", "msg": "note ska ha kind (%s) och name" % ", ".join(NOTE_KINDS)})
            return
        self._broadcast({"t": "note", "kind": kind, "name": name, "from": client.id, "by": client.name},
                        skip=client.id)
        log("%s sparade '%s', som inte är sessionens projekt: det delas om hen sparar igen inom kort"
            % (client.name, name))

    def _notes_put(self, client, msg):
        pattern, channel, ppq, notes = msg.get("p"), msg.get("c"), msg.get("ppq"), msg.get("notes")
        if not is_int(pattern, 1, 1 << 20) or not is_int(channel, 0, 1 << 20) or not is_int(ppq, 1, 1 << 20):
            self._send(client, {"t": "error", "msg": "notes_put ska ha mönster (p), kanal (c) och ppq"})
            return
        if not isinstance(notes, list) or len(notes) > MAX_NOTES or not all(valid_note(n) for n in notes):
            self._send(client, {"t": "error", "msg": "notes ska vara en lista med högst %d noter" % MAX_NOTES})
            return
        if self._differs(msg.get("fp")):
            self._send(client, {"t": "error", "stale": True,
                                "msg": "noterna gjordes för en annan projektstruktur och skickades inte vidare"})
            return
        self.notes_version += 1
        entry = {"p": pattern, "c": channel, "v": self.notes_version, "by": client.name, "from": client.id,
                 "ppq": ppq, "notes": notes}
        self.notes.pop((pattern, channel), None)
        self.notes[(pattern, channel)] = entry  # the newest last
        total = sum(len(e["notes"]) for e in self.notes.values())
        while total > MAX_STORED_NOTES and len(self.notes) > 1:
            oldest = next(iter(self.notes))
            total -= len(self.notes.pop(oldest)["notes"])
        self._broadcast(dict({"t": "notes"}, **entry))
        log("%s skickade %d noter till kanal %d i mönster %d" % (client.name, len(notes), channel + 1,
                                                                pattern))

    # --- versions on disk (persist) ----------------------------------------------------

    def _load_session(self):
        """Read back what session.json kept: the version counter, so numbers go
        on, and the newest version whose files are all still here, with its
        structure as the session's. Values and notes aren't kept: the first FL
        to join shares its own, as at any start. Then the blobs no kept version
        uses go."""
        path = os.path.join(self.store.root, SESSION_FILE)
        saved, problem = self._read_session(path)
        if problem:
            # Which blobs are still needed can't be told, so they all stay; the
            # next version shared writes a new file.
            log("%s gick inte att läsa (%s): sessionen börjar utan sparade versioner" % (path, problem))
            return
        listed = [data for data in [saved.get("snapshot")] + saved["history"] if data is not None]
        found = {}
        for data in listed:
            snapshot = self._stored_snapshot(data)
            if snapshot is not None:
                found.setdefault(snapshot["v"], snapshot)
        self.kept = sorted(found.values(), key=lambda s: s["v"], reverse=True)[:KEEP_VERSIONS + 1]
        self.version = max([saved["version"]] + [s["v"] for s in self.kept])
        if self.kept:
            self.snapshot = self.kept[0]
            self.fp = self.snapshot["fp"]
        # Client ids start over with the relay, and a client takes a version whose
        # `from` is its own id for its own: newcomers get ids no kept version names.
        taken = [int(s["from"][1:]) for s in self.kept if re.match(r"c\d{1,9}\Z", s["from"])]
        self._ids = itertools.count(max(taken, default=0) + 1)
        if len(listed) > len(self.kept):
            log("%d av de sparade versionerna saknar filer eller är trasiga och hoppas över"
                % (len(listed) - len(self.kept)))
        if self.snapshot is not None:
            log("Sessionen fortsätter från version %d av '%s' (%d äldre sparade)"
                % (self.snapshot["v"], self.snapshot["name"], len(self.kept) - 1))
        elif self.version:
            log("Ingen sparad version finns kvar; nästa blir version %d" % (self.version + 1))
        self._prune()

    @staticmethod
    def _read_session(path):
        """(session.json's content, None), or (None, why it can't be used)."""
        try:
            with open(path, "rb") as f:
                saved = json.loads(f.read().decode("utf-8"))
        except FileNotFoundError:
            return {"format": SESSION_FORMAT, "version": 0, "snapshot": None, "history": []}, None  # a new store
        except (OSError, ValueError, RecursionError) as e:  # unreadable, not UTF-8 or not JSON
            return None, str(e) or e.__class__.__name__
        if not (isinstance(saved, dict) and is_int(saved.get("format"), SESSION_FORMAT, SESSION_FORMAT)
                and is_int(saved.get("version")) and isinstance(saved.get("history"), list)):
            return None, "okänt format"  # not ours, or from a newer relay
        return saved, None

    def _stored_snapshot(self, data):
        """A snapshot from session.json as it was handed out, if it is sound and
        its .flp and samples are all in the store with the right sizes."""
        if not isinstance(data, dict) or not is_int(data.get("v"), 1):
            return None
        clean, problem = self._check_snapshot(data)
        by, sender = data.get("by"), data.get("from")
        if problem or not all(isinstance(x, str) and 0 < len(x) <= MAX_NAME for x in (by, sender)):
            return None
        snapshot = dict(v=data["v"], **clean)
        snapshot.update({"by": by, "from": sender})
        return snapshot

    def _prune(self):
        """Remove the blobs no kept version uses: those of versions that fell out
        of the history, or of shares that never happened. Only files named like
        a blob: anything else in the folder isn't ours."""
        used = set()
        for snapshot in self.kept:
            used.add(snapshot["flp"])
            used.update(s["h"] for s in snapshot["samples"])
        try:
            names = os.listdir(self.store.root)
        except OSError:
            return
        count = freed = 0
        for name in names:
            path = self.store.path(name)
            if name in used or not valid_hash(name) or not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue  # in use, say: the next start tries again
            count += 1
            freed += size
        if count:
            log("Tog bort %d %s som ingen sparad version använder (%s)"
                % (count, "fil" if count == 1 else "filer", megabytes(freed)))

    def _write_session(self):
        """Write session.json, taking the old one's place in one step: a crash
        leaves one of them whole, never half of one."""
        data = {"format": SESSION_FORMAT, "version": self.version,
                "snapshot": self.kept[0] if self.kept else None, "history": self.kept[1:]}
        path = os.path.join(self.store.root, SESSION_FILE)
        temp = None
        try:
            # .part: a leftover goes at the next start, like an unfinished upload's.
            fd, temp = tempfile.mkstemp(dir=self.store.root, prefix="session-", suffix=".part")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)  # all ASCII: no text from the network can fail to encode
                f.flush()
                os.fsync(f.fileno())  # on disk before it replaces the old one
            os.replace(temp, path)
        except OSError as e:
            # The session goes on; the next version shared tries again.
            log("Kunde inte spara versionerna i %s (%s)" % (path, e))
            if temp is not None:
                try:
                    os.remove(temp)
                except OSError:
                    pass

    # --- the files connection -------------------------------------------------

    def _open_files(self, hello, writer):
        client = Client("f%d" % next(self._file_ids), hello_name(hello, "filer"), FILES, writer)
        self.files[client.id] = client
        self._send(client, {"t": "welcome", "proto": PROTO, "id": client.id, "name": client.name,
                            "max_blob": self.max_blob})
        if self.verbose:
            log("%s öppnade en filanslutning" % client.name)
        return client

    def _close_files(self, client):
        self.files.pop(client.id, None)
        if client.upload is not None:
            client.upload.discard()
            client.upload = None

    async def _serve_files(self, client, reader):
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = parse(line)
            kind = msg.get("t") if msg is not None else None
            if msg is None:
                self._send(client, {"t": "error", "msg": "ogiltig JSON"})
            elif kind == "blob":
                self._blob(client, msg)
            elif kind == "blob_has":
                self._blob_has(client, msg.get("h"))
            elif kind == "blob_get":
                await self._blob_get(client, msg.get("h"))
            elif kind == "ping":
                # Answered, but never dropped for silence: it says nothing while it downloads.
                self._send(client, {"t": "pong"})
            else:
                self._send(client, {"t": "error", "msg": "okänd meddelandetyp på en filanslutning: %s"
                                    % shown(kind)})

    def _blob_has(self, client, hashes):
        if not isinstance(hashes, list) or len(hashes) > MAX_LIST or not all(valid_hash(h) for h in hashes):
            self._send(client, {"t": "error", "msg": "h ska vara en lista med högst %d sha256-namn" % MAX_LIST})
            return
        missing = []
        for h in hashes:
            if self.store.size(h) is None and h not in missing:
                missing.append(h)
        self._send(client, {"t": "blob_missing", "h": missing})

    def _blob(self, client, msg):
        """One chunk of an upload. Chunks come in order; the last one is answered
        with blob_ok, and anything wrong with one `error` for the whole blob."""
        h, i, n, d = msg.get("h"), msg.get("i"), msg.get("n"), msg.get("d")
        if client.failed is not None and client.failed == (h, n) and is_int(i, 1):
            return  # the rest of an upload that already failed: one error was enough
        client.failed = None
        if not valid_hash(h):
            self._upload_failed(client, h, n, "h ska vara blobens sha256 i hex")
            return
        if not is_int(n, 1, self.max_blob // CHUNK + 1) or not is_int(i, 0, n - 1):
            self._upload_failed(client, h, n, "ogiltigt bitnummer")
            return
        try:
            data = base64.b64decode(d, validate=True) if isinstance(d, str) else None
        except (binascii.Error, ValueError):
            data = None
        if data is None or len(data) > CHUNK:
            self._upload_failed(client, h, n, "d ska vara base64 av högst %d byte" % CHUNK)
            return
        upload = client.upload
        if i != 0 and (upload is None or (upload.h, upload.n, upload.next) != (h, n, i)):
            self._upload_failed(client, h, n, "bitarna kom i fel ordning")
            return
        try:
            if i == 0:
                if upload is not None:
                    upload.discard()
                upload = client.upload = Upload(self.store.root, h, n)
            if upload.size + len(data) > self.max_blob:
                self._upload_failed(client, h, n, "filen är större än relayns gräns på %s"
                                    % megabytes(self.max_blob))
                return
            upload.add(data)
            if i < n - 1:
                return
            client.upload = None
            stored = upload.finish(self.store.path(h))
        except OSError as e:  # the disk is full, say
            if upload is not None:
                upload.discard()
            client.upload = None
            self._upload_failed(client, h, n, "relayn kunde inte spara filen (%s)" % e)
            return
        if stored:
            self._send(client, {"t": "blob_ok", "h": h})
            if self.verbose:
                log("%s laddade upp %s (%s)" % (client.name, h[:12], megabytes(upload.size)))
        else:
            self._send(client, {"t": "error", "h": h, "msg": "kontrollsumman stämmer inte, filen sparades inte"})

    def _upload_failed(self, client, h, n, why):
        if client.upload is not None:
            client.upload.discard()
            client.upload = None
        client.failed = (h, n) if valid_hash(h) else None
        self._send(client, {"t": "error", "h": h if valid_hash(h) else None, "msg": why})

    async def _blob_get(self, client, h):
        size = self.store.size(h)
        try:
            blob = open(self.store.path(h), "rb") if size is not None else None
        except OSError:
            blob = None
        if blob is None:
            self._send(client, {"t": "error", "h": h if valid_hash(h) else None, "msg": "relayn har ingen sådan fil"})
            return
        writer = client.writer
        count = max(1, -(-size // CHUNK))
        with blob:
            for i in range(count):
                data = base64.b64encode(blob.read(CHUNK)).decode("ascii")
                writer.write(encode({"t": "blob", "h": h, "i": i, "n": count, "d": data}))
                # At the client's pace: a big download never piles up in memory
                # (nor trips MAX_OUTBOX), and the session's messages go on meanwhile.
                await writer.drain()

    # --- sending ----------------------------------------------------------

    def _send(self, client, msg):
        self._write(client, encode(msg))

    def _broadcast(self, msg, skip=None):
        data = encode(msg)
        for client in list(self.clients.values()):
            if client.id != skip:
                self._write(client, data)

    def _write(self, client, data):
        writer = client.writer
        if writer.is_closing():
            return
        writer.write(data)
        if writer.transport.get_write_buffer_size() > MAX_OUTBOX:
            log("%s ligger för långt efter och kopplas från" % client.name)
            writer.transport.abort()  # close() would wait for a buffer the client never reads

    def _unique_name(self, name):
        taken = {c.name for c in self.clients.values()}
        candidate, n = name, 2
        while candidate in taken:
            candidate = "%s (%d)" % (name, n)
            n += 1
        return candidate


def lan_addresses():
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []
    return [a for a in addresses if not a.startswith("127.")]


async def run(args):
    relay = Relay(verbose=args.verbose, store=args.store, max_blob=args.max_blob, password=args.password)
    try:
        port = await relay.start(args.host, args.port)
        log("FLSync-relay lyssnar på %s:%d" % (args.host, port))
        if args.host == "0.0.0.0":
            for address in lan_addresses():
                log("  andra datorer startar bryggan med --relay %s:%d" % (address, port))
        log("Delade projektversioner sparas i %s (högst %s per fil)" % (relay.store.root,
                                                                      megabytes(relay.max_blob)))
        log("Avsluta med Ctrl+C")
        await relay.serve_forever()
    finally:
        relay.close_store()  # a temporary folder of our own goes away again


def size_arg(text):
    """A byte count, optionally with K, M or G: 256M."""
    match = re.fullmatch(r"(\d+)\s*([KMG]?)i?B?", text.strip(), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("ange en storlek, t.ex. 256M")
    return int(match.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30}[match.group(2).upper()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="FLSync-relay (protokoll %d)" % PROTO)
    parser.add_argument("--host", default="127.0.0.1",
                        help="adress att lyssna på; 0.0.0.0 låter andra datorer i nätverket ansluta")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store", help="mapp för delade projekt och samples (standard: en ny tillfällig mapp)")
    parser.add_argument("--max-blob", type=size_arg, default=DEFAULT_MAX_BLOB,
                        help="största fil som tas emot, t.ex. 256M (standard)")
    parser.add_argument("-v", "--verbose", action="store_true", help="logga varje ändring")
    parser.add_argument("--password", help="lösenord som alla måste ange för att gå med")
    args = parser.parse_args(argv)
    try:
        # Names come from the network: log them even when the output goes to a pipe or a file.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
