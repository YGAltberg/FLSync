#!/usr/bin/env python3
"""Terminal client for an FLSync session (protocol v2, see docs/protocol_v2.md).

Shows other people's changes live and lets you make changes yourself, so the
whole chain can be tested with a single FL Studio. It also says when someone
shares a new version of the project or sends piano-roll notes.

    python tools/monitor.py [--host 127.0.0.1] [--port 31415] [--name monitor]
"""

import argparse
import asyncio
import json
import math
import re
import sys
import threading

PROTO = 2
PRINT_INTERVAL = 0.15  # a fader drag is many changes; show the latest per key this often
MAX_PRINTED = 40       # changes shown per interval; more (a whole project being shared) are counted
MAX_LINE = 16 << 20    # the session's state arrives as one line
TEMPO_RANGE = (10.0, 522.0)       # what FL Studio accepts
TRANSIENT = ("s/play", "s/pos")   # actions rather than state, so `state` leaves them out

HELP = """Kommandon:
  vol <spår> <0-1>        mixerspårets volym (0.8 = 0 dB), t.ex. "vol 1 0.5"
  pan <spår> <-1..1>      mixerspårets panorering, -1 = helt vänster
  mute <spår> [on|off]    mute på mixerspåret (växlar om du inte skriver on/off)
  chvol <kanal> <0-1>     kanalens volym, t.ex. "chvol 1 0.5" för den första kanalen
  chpan <kanal> <-1..1>   kanalens panorering
  tempo <bpm>             tempo, t.ex. "tempo 128"
  play, stop              starta och stoppa uppspelningen
  demo [spår]             låter volymen på mixerspåret svepa upp och ner i 5 sekunder
  set <nyckel> <värde>    skicka vilken nyckel som helst, t.ex. "set s/play true" eller
                          'set pt/1/n "Vers"' (värdet läses som JSON om det går, annars som text)
  get [början]            visa sessionens värden vars nycklar börjar så, t.ex. "get m/3/"
  state                   visa det i sessionen som inte har standardvärden
  peers                   visa vilka som är anslutna
  help                    visa den här listan
  quit                    avsluta
Spår 0 är master och 1 och uppåt är inserts. Kanaler, slots och EQ-band räknas från 1, som i FL.
set och get tar nycklarna som de är (se docs/protocol_v2.md), och där räknas kanaler från 0."""

AREAS = ("s", "m", "mx", "mr", "c", "g", "gp", "pt", "pl", "pc", "pm")  # the order state and get list them in
MAX_STEPS_SHOWN = 8  # step parameters: list this many steps, count the rest
SONG = ("tempo", "num", "den", "loop", "play", "pos")
EQ_PROPS = {"g": "gain", "f": "frekvens", "w": "bredd"}
NEUTRAL_EQ_GAIN = 0.5  # 0 dB: the band does nothing, whatever its frequency and width
DEFAULT_NAME = re.compile(r"(Master|(Insert|Track|Pattern) \d+)?$")  # what FL calls things nobody named
DEFAULTS = {  # (area, prop) -> value in a new FL project
    ("m", "v"): 0.8, ("m", "p"): 0, ("m", "mu"): False, ("m", "s"): 0, ("m", "fx"): True,
    ("mx", "m"): 1.0, ("mx", "on"): True,
    ("c", "v"): 0.78125, ("c", "p"): 0, ("c", "pi"): 0, ("c", "mu"): False,
    ("pl", "mu"): False,
}
_MISSING = object()


# --- describing values ----------------------------------------------------------

def track_name(index):
    return "Master" if index == 0 else "Insert %d" % index


def raw(value):
    return json.dumps(value, ensure_ascii=False)


def num(value, decimals):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "%.*f" % (decimals, value)
    return raw(value)


def whole(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == int(value):
        return "%d" % value
    return raw(value)


def on_off(value):
    return "på" if value else "av"


def text(value):
    return '"%s"' % value if isinstance(value, str) else raw(value)


def color(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return "#%06x" % (value & 0xFFFFFF)  # FL colors are 0xAARRGGBB, often negative
    return raw(value)


def routing(value):
    if not isinstance(value, str):
        return raw(value)
    return ", ".join(part for part in value.split(",") if part) or "ingenstans"


def step_params(value):
    """Which steps a gp/<pattern>/<channel> value has parameters for ("steg 1, 5"),
    counted from 1; None if the value isn't in the protocol's form."""
    if not isinstance(value, str):
        return None
    if not value:
        return "inga steg på"
    steps = []
    for item in value.split(";"):
        step, colon, params = item.partition(":")
        fields = params.split(",")
        if not colon or not step.isdecimal() or len(fields) != 8 \
                or not all(re.fullmatch(r"-?\d+", f) for f in fields):
            return None
        steps.append(int(step) + 1)
    listed = ", ".join("%d" % step for step in steps[:MAX_STEPS_SHOWN])
    if len(steps) > MAX_STEPS_SHOWN:
        listed += " och %d till" % (len(steps) - MAX_STEPS_SHOWN)
    return "steg " + listed


def plugin_owner(parts):
    """'Plugin Serum (kanal 4)' for pc/3/Serum/<param>, 'Plugin X (Insert 3 slot 2)' for pm/3/1/X/<param>."""
    if parts[0] == "pc" and len(parts) == 4:
        return "Plugin %s (kanal %d)" % (parts[2].replace("|", "/"), int(parts[1]) + 1)
    if parts[0] == "pm" and len(parts) == 5:
        return "Plugin %s (%s slot %d)" % (parts[3].replace("|", "/"), track_name(int(parts[1])),
                                           int(parts[2]) + 1)
    return None


def describe(key, value, state=None):
    """A Swedish description of one value, e.g. "Insert 3 volym 0.640", numbered
    the way FL and the FLSync script show things (channels and slots from 1).
    `state` fills in what one key alone doesn't say (the other half of the time signature)."""
    try:
        line = _describe(key.split("/"), value, state or {})
    except Exception:  # an index or a value of an unexpected kind: show it as it is
        line = None
    return line or "%s = %s" % (key, raw(value))


def _describe(parts, value, state):
    area, args = parts[0], parts[1:]
    if area == "m" and len(args) == 2:
        return _mixer(track_name(int(args[0])), args[1], value)
    if area == "mx" and len(args) == 3:
        slot = "%s slot %d" % (track_name(int(args[0])), int(args[1]) + 1)
        if args[2] == "m":
            return "%s mix %s" % (slot, num(value, 3))
        if args[2] == "on":
            return "%s %s" % (slot, on_off(value))
    if area == "mr" and len(args) == 1:
        return "%s routing → %s" % (track_name(int(args[0])), routing(value))
    if area == "c" and len(args) == 2:
        return _channel("Kanal %d" % (int(args[0]) + 1), args[1], value)
    if area == "g" and len(args) == 2:
        steps = value if isinstance(value, str) else raw(value)
        return "Mönster %d steg %s (kanal %d)" % (int(args[0]), steps, int(args[1]) + 1)
    if area == "gp" and len(args) == 2:
        steps = step_params(value)
        if steps is not None:
            return "Mönster %d stegparametrar (kanal %d): %s" % (int(args[0]), int(args[1]) + 1, steps)
    if area in ("pc", "pm"):
        owner = plugin_owner(parts)
        if owner:
            return "%s param %d = %s" % (owner, int(parts[-1]), num(value, 3))
    if area == "s" and len(args) == 1:
        return _song(args[0], value, state)
    if area == "pt" and len(args) == 2:
        return _common("Mönster %d" % int(args[0]), args[1], value)
    if area == "pl" and len(args) == 2:
        return _common("Spellistespår %d" % int(args[0]), args[1], value)
    return None


def _mixer(name, prop, value):
    if prop == "v":
        return "%s volym %s" % (name, num(value, 3))
    if prop == "p":
        return "%s pan %s" % (name, num(value, 2))
    if prop == "s":
        return "%s stereobredd %s" % (name, num(value, 2))
    if prop == "fx":
        return "%s effekter %s" % (name, on_off(value))
    band = re.fullmatch(r"e(\d+)([gfw])", prop)
    if band:
        return "%s EQ band %d %s %s" % (name, int(band.group(1)) + 1, EQ_PROPS[band.group(2)],
                                        num(value, 3))
    return _common(name, prop, value)


def _channel(name, prop, value):
    if prop == "v":
        return "%s volym %s" % (name, num(value, 3))
    if prop == "p":
        return "%s pan %s" % (name, num(value, 2))
    if prop == "pi":
        return "%s tonhöjd %s cent" % (name, whole(value))
    if prop == "t":
        return "%s kopplad till %s" % (name, track_name(int(value)))
    return _common(name, prop, value)


def _common(name, prop, value):
    """Properties that several areas have."""
    if prop == "n":
        return "%s namn %s" % (name, text(value))
    if prop == "c":
        return "%s färg %s" % (name, color(value))
    if prop == "mu":
        return "%s mute %s" % (name, on_off(value))
    return None


def _song(prop, value, state):
    if prop == "tempo":
        return "Tempo %s BPM" % num(value, 1)
    if prop in ("num", "den"):
        signature = {"num": state.get("s/num", 4), "den": state.get("s/den", 4), prop: value}
        return "Taktart %s/%s" % (whole(signature["num"]), whole(signature["den"]))
    if prop == "loop":
        return {0: "Mönsterläge", 1: "Låtläge"}.get(value)
    if prop == "play":
        return "Uppspelning startad" if value else "Uppspelning stoppad"
    if prop == "pos":  # in quarter notes, -1 while stopped
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            return "Ingen startposition (stoppad)"
        return "Startposition %s fjärdedelar" % num(value, 2)
    return None


def is_default(key, value, state):
    """True for what a new FL project has anyway (as far as known); `state` leaves it out."""
    parts = key.split("/")
    area, prop = parts[0], parts[-1]
    if area == "g":
        return isinstance(value, str) and "1" not in value
    if area == "gp":
        return value == ""
    if area == "mr":
        return isinstance(value, str) and re.fullmatch(r"(0:[^,]*)?", value) is not None  # only to master
    if prop == "n":
        return isinstance(value, str) and DEFAULT_NAME.match(value) is not None
    if area == "m" and len(parts) == 3:
        band = re.fullmatch(r"(e\d+)[gfw]", prop)
        if band:
            gain = state.get("m/%s/%sg" % (parts[1], band.group(1)), NEUTRAL_EQ_GAIN)
            return gain == NEUTRAL_EQ_GAIN
    return DEFAULTS.get((area, prop), _MISSING) == value


def key_order(key):
    """By area (the song's tempo first), then by the indices as numbers: m/2/v before m/10/v."""
    parts = key.split("/")
    area = AREAS.index(parts[0]) if parts[0] in AREAS else len(AREAS)
    song = SONG.index(parts[-1]) if parts[0] == "s" and parts[-1] in SONG else len(SONG)
    return area, parts[0], song, [(0, int(p), "") if p.isdecimal() else (1, 0, p) for p in parts[1:]]


def amount(items, one, many):
    n = len(items) if isinstance(items, list) else 0
    return "%d %s" % (n, one if n == 1 else many)


def version_text(snapshot):
    """"3 av 'Beat' från Anna (4 samples)" for a snapshot."""
    return "%s av '%s' från %s (%s)" % (snapshot.get("v"), snapshot.get("name"), snapshot.get("by"),
                                        amount(snapshot.get("samples"), "sample", "samples"))


def snapshot_line(snapshot):
    """"Anna delade version 3 av 'Beat' (4 samples)"."""
    return "%s delade version %s av '%s' (%s)" % (snapshot.get("by"), snapshot.get("v"), snapshot.get("name"),
                                                 amount(snapshot.get("samples"), "sample", "samples"))


def notes_line(notes):
    """"Anna skickade 32 noter till kanal 5 i mönster 2" (channels counted from 1)."""
    channel = notes.get("c")
    if isinstance(channel, int) and not isinstance(channel, bool):
        channel += 1
    return "%s skickade %s till kanal %s i mönster %s" % (
        notes.get("by"), amount(notes.get("notes"), "not", "noter"), channel, notes.get("p"))


def pairs(kv):
    """The well-formed [key, value] pairs of a message."""
    for pair in kv if isinstance(kv, list) else ():
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
            yield pair[0], pair[1]


# --- what the user types ---------------------------------------------------------

def index(text, first=0):
    """A track or channel number typed by the user, as the index in the keys:
    tracks are typed as they are (0 = master), channels from 1 (first=1)."""
    value = int(text)
    if value < first:
        raise ValueError(text)
    return value - first


def number(text, low, high):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return min(high, max(low, value))


def parse_value(text):
    """The value after `set`: JSON if it is JSON (0.5, true, "Vers"), otherwise the text itself."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return text


def peer_kind(peer):
    """'fl, FL 25.2.4.4960' or 'monitor': what someone in the session runs."""
    flv = peer.get("flv")
    return "%s, FL %s" % (peer.get("app"), flv) if isinstance(flv, str) and flv else str(peer.get("app"))


class Monitor:
    def __init__(self, host, port, name, password=None):
        self.host, self.port, self.name = host, port, name
        self.password = password
        self.state = {}
        self.peers = {}
        self.pending = {}    # key -> our newest batch that changes it, until the relay has handled it
        self.batch = 0
        self.unprinted = {}  # key -> (who, value), flushed every PRINT_INTERVAL
        self.writer = None
        self.loop = None
        self.reading = False
        self.quitting = False
        self.failed = False
        self.session_fp = None  # the session's project structure; a new one starts its values over

    async def run(self):
        self.loop = asyncio.get_running_loop()
        try:
            reader, self.writer = await asyncio.open_connection(self.host, self.port, limit=MAX_LINE)
        except OSError as e:
            print("Når inte relayn på %s:%d (%s). Är relay.py igång?" % (self.host, self.port, e))
            return 1
        hello = {"t": "hello", "proto": PROTO, "app": "monitor", "name": self.name}
        if self.password:
            hello["pw"] = self.password
        self.send(hello)
        printer = asyncio.ensure_future(self._print_changes())
        try:
            while True:
                try:
                    line = await reader.readline()
                except (OSError, ValueError):  # connection lost, or a line longer than MAX_LINE
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(msg, dict):
                    self._handle(msg)
        finally:
            printer.cancel()
            self._flush_changes()
            self.writer.close()
        if not self.quitting:
            print("Relayn stängde anslutningen.")
        return 1 if self.failed else 0

    def send(self, msg):
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))

    def change(self, key, value):
        """Send one change of our own. Until the relay has handled it, other
        people's values for the key are older than ours and get ignored."""
        self.batch += 1
        self.state[key] = value
        self.pending[key] = self.batch
        self.send({"t": "sets", "l": self.batch, "kv": [[key, value]]})

    # --- incoming ---------------------------------------------------------

    def _handle(self, msg):
        kind = msg.get("t")
        if kind == "sets":
            who = self._name(msg.get("from"))
            for key, value in pairs(msg.get("kv")):
                if key in self.pending:
                    continue  # our own change to it comes later in the relay's order and wins
                self.state[key] = value
                self.unprinted[key] = (who, value)
        elif kind == "ack":
            self._settle(msg.get("l"))
        elif kind == "keep":
            # Only sent for starting values, which a monitor never shares; take the values anyway.
            kept = dict(pairs(msg.get("kv")))
            self.state.update(kept)
            self._settle(msg.get("l"), kept)
        elif kind == "reject":
            self._flush_changes()
            print("! relayn avvisade %s: %s" % (msg.get("k"), msg.get("msg")))
            key = msg.get("k")
            self._settle(msg.get("l"), [key] if isinstance(key, str) else [])
            self.send({"t": "sync"})  # take the session's value instead of the rejected one
        elif kind == "welcome":
            self._welcome(msg)
        elif kind == "state":
            state = msg.get("state")
            if isinstance(state, dict):
                ours = {key: self.state[key] for key in self.pending if key in self.state}
                self.state = dict(state)
                self.state.update(ours)  # still on the way, and newer than the snapshot
        elif kind == "peer_join":
            if isinstance(msg.get("id"), str):
                self.peers[msg["id"]] = msg
            self._flush_changes()
            print("+ %s anslöt (%s)" % (msg.get("name"), peer_kind(msg)))
        elif kind == "peer_leave":
            if isinstance(msg.get("id"), str):
                self.peers.pop(msg["id"], None)
            self._flush_changes()
            print("- %s lämnade" % msg.get("name"))
        elif kind == "snapshot":
            self._flush_changes()
            print("* %s" % snapshot_line(msg))
            if msg.get("fp") != self.session_fp:
                # Another structure: the relay starts the session's values over.
                self.session_fp = msg.get("fp")
                self.send({"t": "sync"})
        elif kind == "notes":
            self._flush_changes()
            print("* %s" % notes_line(msg))
        elif kind == "pen":
            self._flush_changes()
            if msg.get("id") is None:
                print("* Ingen ändrar projektets struktur längre")
            else:
                print("* %s ändrar projektets struktur" % msg.get("name"))
        elif kind == "error":
            self._flush_changes()
            if msg.get("code") == "password":
                print("! Fel lösenord för sessionen. Starta monitorn med --password.")
                self.failed = True
            else:
                print("! relayn: %s" % msg.get("msg"))

    def _welcome(self, msg):
        if msg.get("proto") != PROTO:
            print("Relayn kör protokoll %s men monitorn %d. Uppdatera så att de matchar."
                  % (msg.get("proto"), PROTO))
            self.failed = True
            self._quit()
            return
        state = msg.get("state")
        peers = msg.get("peers") if isinstance(msg.get("peers"), list) else []
        self.state = dict(state) if isinstance(state, dict) else {}
        self.peers = {p["id"]: p for p in peers if isinstance(p, dict) and isinstance(p.get("id"), str)}
        self.session_fp = msg.get("fp")
        print("Ansluten som %s. %d värden i sessionen. Skriv help för kommandon."
              % (msg.get("name"), len(self.state)))
        self._print_peers()
        snapshot = msg.get("snapshot")
        if isinstance(snapshot, dict):
            print("Senaste versionen av projektet: %s" % version_text(snapshot))
        notes = msg.get("notes") if isinstance(msg.get("notes"), list) else []
        if notes:
            print("Sessionen har noter för %d %s" % (len(notes), "kanal" if len(notes) == 1 else "kanaler"))
        pen = msg.get("pen")
        if isinstance(pen, dict):
            print("%s ändrar projektets struktur just nu" % pen.get("name"))
        if not self.reading:
            # Only take commands once we know the session, so "state" shows real data.
            self.reading = True
            threading.Thread(target=self._read_commands, daemon=True).start()

    def _settle(self, batch, keys=None):
        """The relay has handled our batch (or these keys of it)."""
        for key in list(self.pending if keys is None else keys):
            if self.pending.get(key) == batch:
                del self.pending[key]

    def _name(self, cid):
        peer = self.peers.get(cid) if isinstance(cid, str) else None
        return str(peer.get("name", cid) if peer else cid)

    async def _print_changes(self):
        while True:
            await asyncio.sleep(PRINT_INTERVAL)
            self._flush_changes()

    def _flush_changes(self):
        changes, self.unprinted = self.unprinted, {}
        for count, (key, (who, value)) in enumerate(changes.items()):
            if count == MAX_PRINTED:
                print("... och %d ändringar till" % (len(changes) - count))
                break
            print("[%s] %s" % (who, describe(key, value, self.state)))

    def _print_peers(self):
        names = ["%s (%s)" % (p.get("name"), peer_kind(p)) for p in self.peers.values()]
        print("I sessionen: %s" % (", ".join(names) or "ingen annan"))

    def _print_state(self):
        if not self.state:
            print("Sessionen är tom.")
            return
        rows, seen, plugins, hidden = [], set(), {}, 0
        for key in sorted(self.state, key=key_order):
            value, parts = self.state[key], key.split("/")
            if key in TRANSIENT or (len(parts) == 3 and parts[2] == "c") or is_default(key, value, self.state):
                hidden += 1  # colors too: "#5f7581" says little in a terminal
                continue
            try:
                owner = plugin_owner(parts)
            except ValueError:
                owner = None
            if owner:  # a plugin can have thousands of parameters: just count them
                plugins[owner] = plugins.get(owner, 0) + 1
                continue
            row = "  " + describe(key, value, self.state)
            if row not in seen:  # the time signature's two keys give the same line
                seen.add(row)
                rows.append(row)
        rows += ["  %s: %d %s" % (owner, n, "parameter" if n == 1 else "parametrar")
                 for owner, n in plugins.items()]
        print("\n".join(rows) if rows else "Allt i sessionen har standardvärden.")
        if hidden:
            print("(%d värden till, som standardvärden och färger, visas med get)" % hidden)

    def _print_values(self, prefix):
        keys = sorted((key for key in self.state if key.startswith(prefix)), key=key_order)
        for key in keys:
            print("  %s = %s" % (key, raw(self.state[key])))
        if not keys:
            print('Inga värden i sessionen börjar med "%s".' % prefix if prefix else "Sessionen är tom.")

    # --- commands -----------------------------------------------------------

    def _read_commands(self):
        try:
            for line in sys.stdin:
                self.loop.call_soon_threadsafe(self._command, line.strip())
            self.loop.call_soon_threadsafe(self._quit)
        except RuntimeError:
            pass  # the event loop already ended

    def _quit(self):
        self.quitting = True
        if self.writer is not None:
            self.writer.close()

    def _command(self, line):
        words = line.split()
        if not words:
            return
        cmd, args = words[0].lower(), words[1:]
        try:
            if cmd == "vol" and len(args) == 2:
                self.change("m/%d/v" % index(args[0]), number(args[1], 0.0, 1.0))
            elif cmd == "pan" and len(args) == 2:
                self.change("m/%d/p" % index(args[0]), number(args[1], -1.0, 1.0))
            elif cmd == "mute" and len(args) in (1, 2):
                key = "m/%d/mu" % index(args[0])
                self.change(key, args[1].lower() in ("on", "1", "på") if len(args) == 2
                            else not self.state.get(key))
            elif cmd == "chvol" and len(args) == 2:
                self.change("c/%d/v" % index(args[0], first=1), number(args[1], 0.0, 1.0))
            elif cmd == "chpan" and len(args) == 2:
                self.change("c/%d/p" % index(args[0], first=1), number(args[1], -1.0, 1.0))
            elif cmd == "tempo" and len(args) == 1:
                self.change("s/tempo", round(number(args[0], *TEMPO_RANGE), 3))
            elif cmd in ("play", "stop") and not args:
                self.change("s/play", cmd == "play")
            elif cmd == "set" and len(args) >= 2:
                _, key, value = line.split(None, 2)  # the value may contain spaces: 'set pt/1/n "Min vers"'
                self.change(key, parse_value(value))
            elif cmd == "get" and len(args) <= 1:
                self._print_values(args[0] if args else "")
            elif cmd == "demo" and len(args) <= 1:
                asyncio.ensure_future(self._demo(index(args[0]) if args else 1))
            elif cmd == "state":
                self._print_state()
            elif cmd == "peers":
                self._print_peers()
            elif cmd in ("quit", "exit"):
                self._quit()
            else:
                print(HELP)
        except ValueError:
            print(HELP)

    async def _demo(self, track):
        print("Demo: %s svepar i 5 sekunder" % track_name(track))
        key = "m/%d/v" % track
        start = self.state.get(key, 0.8)
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            start = 0.8
        steps = 125  # 25 updates per second
        for step in range(steps + 1):
            phase = step / steps
            self.change(key, round(0.5 + 0.3 * math.sin(2 * math.pi * 2 * phase), 4))
            await asyncio.sleep(0.04)
        self.change(key, start)
        print("Demo klar, volymen återställd till %.3f" % start)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Följ och styr en FLSync-session från terminalen")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=31415)
    parser.add_argument("--name", default="monitor")
    parser.add_argument("--password", help="sessionens lösenord, om relayn har ett")
    args = parser.parse_args(argv)
    try:
        # Names come from the network and descriptions use "→": print them even
        # when the output goes to a pipe or a file, a line at a time. Read what
        # is typed into a pipe the same way.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        return asyncio.run(Monitor(args.host, args.port, args.name, args.password).run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
