#!/usr/bin/env python3
"""FLSync: start or join a session, in a window of its own.

One program, one asyncio event loop, everything a person needs:
  * the relay, when you host ("Starta session"): on all network interfaces,
    with a password, the port opened in the router (UPnP) when it can be,
    and an invite code for the others;
  * the bridge between FL Studio (MIDI) and the relay, always;
  * a page on http://127.0.0.1:<port> that starts or joins sessions and
    shows what is going on, with concrete fixes when something is missing,
    shown in FLSync's own window: an app window of Edge (or Chrome), no tabs,
    no address bar. Closing the window ends FLSync;
  * the installer (setup/setup.py, app/installer.py) in that window first,
    when FLSync.exe starts from the download (--install), and for the Start
    menu's Reparera (--repair) and Avinstallera FLSync (--uninstall).

    FLSync.exe                       (FL's pythonw.exe app/flsync.py: no console)
    python app/flsync.py [--browser | --no-browser] [--name Anna] [--host | --join FLS1-...]

Settings (name, last invite, last mode) are kept in %LOCALAPPDATA%\\FLSync\\settings.json,
and without a console (pythonw) what would be printed goes to flsync.log there.
"""

import argparse
import asyncio
import collections
import errno
import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
import traceback
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
# FL's own Python doesn't put a script's folder on the path: do it for ours and our neighbours.
for _folder in (os.path.join(ROOT, "relay"), os.path.join(ROOT, "bridge"), APP_DIR):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import bridge as bridge_mod  # noqa: E402
import diagnose  # noqa: E402
import flpwatch  # noqa: E402
import installer  # noqa: E402
import invite as invites  # noqa: E402
import relay as relay_mod  # noqa: E402
import upnp  # noqa: E402
import webui  # noqa: E402
import winsys  # noqa: E402
from version import VERSION  # noqa: E402

MIDI_PORT = "FLSync"
DEFAULT_PORT = invites.DEFAULT_PORT
LOG_LINES = 400           # kept for the page
LOG_SHOWN = 200           # sent to the page
ACTIVITY_SHOWN = 8        # what happened in the session, newest first, on the page
NOTES_SHOWN = 8
LOOK_INTERVAL = 3.0       # how often the computer is looked at (programs, MIDI ports)
FL_STARTUP = 45.0         # FL may take this long after starting before it is heard
PROBE_TIMEOUT = 4.0       # for an address of an invite to answer
PROBE_GRACE = 1.0         # once an address answers, how long a preferred one may still take
OPEN_WATCH = 15.0         # how long to look for a second FL after opening a version
SECOND_FL_STAYS = 3.0     # ...that keeps running at least this long (not just handing the file over)
QUIT_DELAY = 0.3          # the page gets its answer before the program ends
BIND_TRIES = 15           # attempts to listen on a port nothing listens on (0.2 s apart)
ROUTER_WAIT = 12.0        # leaving waits this long for a router that is opening the port
WINDOW_POLL = 0.5         # how often FLSync looks whether its window is still open
WINDOW_HANDOFF = 5.0      # a browser that ends this soon handed the window to one that runs already
BYE_GRACE = 3.0           # after the page says it closes, a reload has this long to come back
STOP_WAIT = 30.0          # installing waits this long for an FLSync that runs to end (it leaves its session)
LOG_FILE = "flsync.log"   # without a console (pythonw), what would be printed
LOG_FILE_MAX = 1 << 20    # ... started over (the old one kept as .old) when it grows past this
LOBBY = "ingen session vald: välj Starta session eller Gå med i FLSync-fönstret"
LOBBY_REPEAT = 30.0       # FL hears that again at most this often (e.g. after a Reload script)
RESUME_RETRY = 10.0       # a guest resuming its session tries the host again this often
REPO = "https://github.com/YGAltberg/FLSync"
RELEASES_API = "https://api.github.com/repos/YGAltberg/FLSync/releases/latest"  # asked once per start
RELEASES_PAGE = REPO + "/releases/latest"
UPDATE_TIMEOUT = 8.0
AUTOSAVE_FROM = (26, 1, 7)  # the FL that may save by itself (flsync_content.SAVES_SINCE)
PAGE = os.path.join(APP_DIR, "ui.html")
ICON = os.path.join(APP_DIR, "flsync.ico")  # also the shortcut's icon


def megabytes(size):
    return "%.1f MB" % (size / 1e6)


def fl_label(major):
    """'FL Studio 2026' for 26 (the settings key's number), 'FL Studio 21' for 21."""
    return "FL Studio %d" % (2000 + major if major >= 24 else major)


def clean_name(text):
    """A name to show the others: printable, at most 40 characters (the relay's limit)."""
    text = "".join(ch for ch in str(text or "") if ch.isprintable()).strip()
    return text[:40].strip()


def version_tuple(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return None


def clock_text(when):
    return time.strftime("%H:%M", time.localtime(when))


def missing_text(entry):
    """'Serum (kanal 3, 7)' from {plugin, channels}; plain names as they are."""
    if not isinstance(entry, dict):
        return str(entry)
    channels = entry.get("channels") or []
    if channels:
        return "%s (kanal %s)" % (entry.get("plugin"), ", ".join(str(c) for c in channels))
    if entry.get("mixer"):
        return "%s (mixern)" % entry.get("plugin")
    return str(entry.get("plugin"))


def genitive(name):
    name = str(name or "någons")
    return name if name[-1:].lower() in ("s", "x", "z") else name + "s"


def names_text(names):
    names = [str(n) for n in names or () if n]
    if len(names) <= 1:
        return "".join(names) or "någon"
    return ", ".join(names[:-1]) + " och " + names[-1]


def saved_text(msg):
    """What the page says about the bridge's `saved` (docs/phase4.md)."""
    outcome, v = msg.get("outcome"), msg.get("v")
    others = names_text(msg.get("with"))
    version = " (version %s)" % v if v else ""
    opening = {"now": " FL öppnar den gemensamma versionen.",
               "later": " Den öppnas i FL när du tar en paus (eller nu med Ctrl+S).",
               "stop": " Den öppnas i FL när du stoppar uppspelningen.",
               "no": " Spara igen i FL (Ctrl+S) för att öppna den, eller klicka Öppna senaste versionen."
               }.get(msg.get("open"), "")
    if outcome == "shared":
        return "Din version är delad med de andra%s." % version
    if outcome == "merged":
        text = "Din version är sammanslagen med %s ändringar%s." % (genitive(others), version) + opening
        conflicts = msg.get("conflicts")
        if conflicts:
            text += (" %d %s hade ni båda ändrat: där behölls din ändring."
                     % (conflicts, "del" if conflicts == 1 else "delar"))
        return text
    if outcome == "pulled":
        return "Du hade inget nytt att dela, så du får %s version%s." % (genitive(others), version) + opening
    if outcome == "unrelated":
        return ("Projektet delades inte: det du har öppet i FL hör inte till sessionen. Spara igen i FL (Ctrl+S) "
                "inom %s sekunder, eller klicka Dela mitt projekt, om det ska bli allas projekt. Annars: öppna "
                "sessionens version." % (msg.get("confirm") or 15))
    if outcome == "switched":
        return "Ditt projekt är nu sessionens%s: de andra får det öppnat i sina FL." % version
    if outcome == "old":
        return ("%s bytte sessionens projekt, så din sparning delades inte (den finns kvar i din fil)."
                % capital(others)) + opening
    if outcome == "replaced":
        return ("Din version kunde inte slås ihop med %s version (%s), så den ersatte den. %s version finns kvar "
                "i mappen Versioner bredvid projektet." % (genitive(others), msg.get("why") or "okänt skäl",
                                                           capital(genitive(others))))
    if outcome == "failed":
        return "Projektet kunde inte delas: %s." % (msg.get("why") or "okänt fel")
    return str(outcome)


def saved_event(msg):
    """The bridge's `saved` as one short line of the page's Händelser."""
    outcome, v = msg.get("outcome"), msg.get("v")
    others = names_text(msg.get("with"))
    version = " %s" % v if v else ""
    if outcome == "shared":
        return "Du delade version%s" % version
    if outcome == "merged":
        return "Din version slogs ihop med %s ändringar (version%s)" % (genitive(others), version)
    if outcome == "pulled":
        return "Du fick %s ändringar (version%s)" % (genitive(others), version)
    if outcome == "switched":
        return "Ditt projekt blev sessionens (version%s)" % version
    if outcome == "old":
        return "%s bytte sessionens projekt, så din sparning delades inte" % capital(others)
    if outcome == "replaced":
        return "Din version ersatte %s: de gick inte att slå ihop" % genitive(others)
    if outcome == "unrelated":
        return "Det du sparade delades inte: projektet hör inte till sessionen"
    if outcome == "failed":
        return "Det du sparade kunde inte delas: %s" % (msg.get("why") or "okänt fel")
    return None


def capital(text):
    return text[:1].upper() + text[1:]


def os_reason(error):
    if isinstance(error, ConnectionRefusedError):
        return "ingen lyssnar på porten"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "inget svar"
    return getattr(error, "strerror", None) or str(error) or error.__class__.__name__


# --- settings ---------------------------------------------------------------------------

class Settings:
    """settings.json: name, last invite, last mode, the host's password and port.
    A missing or broken file gives the defaults; it is written whole or not at all."""

    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger or (lambda msg: None)
        self.data = {}

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            self.log("Inställningarna i %s gick inte att läsa, standardvärden används" % self.path)
            data = {}
        self.data = data if isinstance(data, dict) else {}
        return self

    def get(self, key, default=None):
        value = self.data.get(key)
        return default if value is None else value

    def set(self, **values):
        self.data.update(values)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        except OSError as e:
            self.log("Kunde inte spara inställningarna i %s: %s" % (self.path, e))


# --- trying an invite's addresses -------------------------------------------------------------

async def probe(host, port, password, name="FLSync", timeout=PROBE_TIMEOUT):
    """How the relay at host:port answers a files hello with this password:
    (outcome, text), outcome "ok", "password", "proto", "other" or "unreachable".
    A files connection isn't a peer, so nobody in the session notices."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return "unreachable", os_reason(e)
    try:
        hello = {"t": "hello", "app": "files", "proto": relay_mod.PROTO, "name": name, "pw": password}
        writer.write((json.dumps(hello) + "\n").encode("utf-8"))
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, ValueError, asyncio.TimeoutError):
        line = b""
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass
    try:
        msg = json.loads(line) if line else None
    except ValueError:
        msg = None
    if not isinstance(msg, dict):
        return "other", "svarar inte som en FLSync-session"
    text = msg.get("msg") if isinstance(msg.get("msg"), str) else None
    if msg.get("t") == "welcome":
        if msg.get("proto") not in (None, relay_mod.PROTO):
            return "proto", "relayn kör protokoll %s" % msg.get("proto")
        return "ok", None
    if msg.get("t") == "error":
        if msg.get("code") == "password":
            return "password", text
        if msg.get("code") == "proto" or "protokoll" in (text or ""):
            return "proto", text
    return "other", text or "svarar inte som en FLSync-session"


async def choose(addresses, port, password, name="FLSync", timeout=PROBE_TIMEOUT, grace=PROBE_GRACE,
                 probe_fn=probe):
    """Try every address at once and return (address, None) for the best one
    that answers right (invites.join_order: LAN, Tailscale, internet), or
    (None, {address: (outcome, text)}). Once one answers, a preferred one
    still gets `grace` seconds."""
    order = invites.join_order(addresses)
    loop = asyncio.get_running_loop()
    tasks = [asyncio.ensure_future(probe_fn(a, port, password, name, timeout)) for a in order]
    patience = None
    try:
        while True:
            answered = [(a, t.result()) for a, t in zip(order, tasks) if t.done()]
            for address, task in zip(order, tasks):
                if not task.done():
                    break
                if task.result()[0] == "ok":
                    return address, None  # every address before it has failed
            good = [a for a, (outcome, _) in answered if outcome == "ok"]
            pending = [t for t in tasks if not t.done()]
            if good and patience is None:
                patience = loop.time() + grace
            if good and (not pending or loop.time() >= patience):
                return good[0], None
            if not pending:
                return None, dict(answered)
            wait = None if patience is None else max(0.0, patience - loop.time())
            await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


def join_problem(results, port):
    """(code, title, text, fix) for an invite none of whose addresses answered right."""
    outcomes = {outcome for outcome, _ in results.values()}
    tried = ", ".join("%s (%s: %s)" % (a, invites.KIND_NAMES.get(invites.kind(a), "?"), text or outcome)
                      for a, (outcome, text) in results.items())
    if "password" in outcomes:
        return ("password", "Fel lösenord",
                "Värdens FLSync svarar, men inbjudan hör inte till den session som körs nu.",
                ["Värden har kanske gjort en ny kod eller startat om: be om den senaste inbjudan."])
    if "proto" in outcomes:
        text = next(t for o, t in results.values() if o == "proto")
        return ("proto", "Olika versioner av FLSync", text or "",
                ["Uppdatera FLSync hos er båda till samma version (kör Installera FLSync med den nya versionen)."])
    if "other" in outcomes:
        return ("other", "Adressen svarar inte som FLSync", "Provade " + tried + ".",
                ["Kontrollera att du har klistrat in rätt inbjudan."])
    return ("unreachable", "Ingen av adresserna i inbjudan svarar", "Provade " + tried + ".",
            ["Kontrollera att värden har FLSync igång och har valt Starta session.",
             "Är ni på samma nätverk: när Windows frågar om brandväggen hos värden ska den svara Tillåt "
             "(annars: Windows-säkerhet > Brandvägg > Tillåt en app, och tillåt Python).",
             "Via internet: värden behöver port %d öppen i routern (FLSync försöker själv med UPnP och "
             "säger om det inte gick), eller så använder ni Tailscale båda två." % port])


# --- a running launcher -------------------------------------------------------------------------

def instance_path(data_dir):
    return os.path.join(data_dir, "launcher.json")


def read_instance(data_dir):
    """launcher.json of the launcher running for this data folder: {pid, port,
    token, window} ({} if there is none)."""
    try:
        with open(instance_path(data_dir), encoding="utf-8") as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def ask_running(data_dir, name, body=None, timeout=1.5):
    """GET /api/<name> (POST with `body`) of the launcher running for this data
    folder: its answer, or None if none answers."""
    try:
        info = read_instance(data_dir)
        port, token = int(info["port"]), str(info["token"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {webui.TOKEN_HEADER: token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request("http://127.0.0.1:%d/api/%s" % (port, name), data=data, headers=headers)
        with opener.open(request, timeout=timeout) as answer:
            return json.load(answer)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fetch_json(url, timeout):
    """A JSON document from the internet (GitHub's API wants a User-Agent)."""
    request = urllib.request.Request(url, headers={"User-Agent": "FLSync/" + VERSION,
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read(1 << 20).decode("utf-8"))


def find_running(data_dir, timeout=1.5):
    """The page address of a launcher already running for this data folder, or None."""
    answer = ask_running(data_dir, "ping", timeout=timeout)
    if isinstance(answer, dict) and answer.get("app") == "FLSync":
        info = read_instance(data_dir)
        try:
            return "http://127.0.0.1:%d/?t=%s" % (int(info["port"]), str(info["token"]))
        except (KeyError, TypeError, ValueError):
            pass
    return None


# --- the launcher -----------------------------------------------------------------------------

class App:
    """The launcher. start() on a running event loop, close() at the end
    (run() does both and waits in between). Everything that touches the
    computer goes through `system` (winsys.System), and MIDI through
    `run_midi` (bridge.run_midi): the tests give fakes for both.

    `setup` (an installer.SetupRun): the page shows the installer first, and
    FLSync itself starts once it is done ("Starta FLSync"). `window`: the page
    is shown in FLSync's own window, and closing it ends FLSync."""

    def __init__(self, data_dir=None, system=None, name=None, relay_port=None, bind="0.0.0.0", ui_port=0,
                 midi_port=MIDI_PORT, bridge_options=None, run_midi=None, mapper=None, ip_lookup=True,
                 verbose=False, max_blob=None, echo=True, probe_timeout=PROBE_TIMEOUT, setup=None, window=False,
                 update_check=False):
        self.data_dir = data_dir or bridge_mod.local_data_dir()
        self.setup = setup
        self.window_wanted = window
        self.window = None           # FLSync's window: its browser process (system.open_window)
        self.window_opened_at = 0.0
        self.page_seen = 0.0         # when the page last asked for the status
        self.bye_at = None           # when the page said its window closes
        self.system = system or winsys.System()
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.echo = echo
        self.settings = Settings(os.path.join(self.data_dir, "settings.json"), self.log).load()
        self.name = clean_name(name) or clean_name(self.settings.get("name")) or clean_name(self.system.user_name())
        if clean_name(name):
            self.settings.set(name=self.name)
        saved = self.settings.get("relay_port")
        if not (isinstance(saved, int) and not isinstance(saved, bool) and 0 < saved < 65536):
            saved = DEFAULT_PORT
        self.relay_port = relay_port or saved
        self.bind = bind
        self.ui_port = ui_port
        self.midi_port = midi_port
        self.bridge_options = dict(bridge_options or {})
        self.run_midi = run_midi or bridge_mod.run_midi
        self.mapper = upnp.PortMapper() if mapper is None else mapper  # False: don't ask the router
        self.ip_lookup = ip_lookup
        self.update_check = update_check  # ask GitHub once whether there is a newer FLSync
        self.update = None           # {version, url} of a newer FLSync, once found
        self.verbose = verbose
        self.max_blob = max_blob or relay_mod.DEFAULT_MAX_BLOB
        self.probe_timeout = probe_timeout

        self.token = secrets.token_urlsafe(24)
        self.guard = webui.Guard(self.token)
        self.server = None
        self.url = None
        self.loop = None
        self.quit_event = None
        self.closed = threading.Event()
        self.closing = False
        self.tasks = set()
        self.bridge = None
        self.page = None
        self.icon = None

        # The session.
        self.mode = None             # None, "host" or "join"
        self.target = None           # (host, port, password) the bridge joins; None while no session is chosen
        self.generation = 0          # counts sessions: what's still on its way for an old one is dropped
        self.busy = None             # what is being done right now, for the page
        self.error = None            # the last action's problem: {id, code, title, text, fix}
        self.errors = 0
        self.relay = None
        self.relay_task = None
        self.invite = None           # host: the code to hand out
        self.invite_addresses = []
        self.invite_note = None      # ...and what limits it
        self.router = None           # host: what UPnP did, {state, title, detail, fix}
        self.mapping = None
        self.asking_router = False   # a UPnP request is on its way
        self.invite_task = None
        self.joined = None           # join: the address that answered
        self.resuming = None         # the invite code FLSync tries to go back into (_rejoin)
        self.open_warning = None
        self.open_poll = 0.5         # how often to look for a second FL after opening a version
        self.second_fl_stays = SECOND_FL_STAYS

        # What FL and the session look like, from what the bridge tells FL.
        self.fl_hello = {}           # FL's last hello (flv: its version)
        self.lobby_said = None       # when FL last heard that no session is chosen
        self.my_name = None
        self.peers = {}              # id -> peer info, while in the session
        self.pen = None
        self.link_problem = None     # why the bridge can't reach the relay
        self.session_problem = None  # why the relay refused FL (another version of the script)
        self.notices = {}            # (v, from) -> the bridge's snapshot notice to FL, merged
        self.seen_at = {}            # (v, from) -> when it was first heard of
        self.loaded = None           # the version FL has opened (`loaded`)
        self.saved = None            # how FL's last save went: {time, text, state}
        self.notes_seen = collections.deque(maxlen=NOTES_SHOWN)
        self.activity = collections.deque(maxlen=ACTIVITY_SHOWN)  # {time, text, kind}: newest first

        # What the computer looks like (winsys), refreshed on a worker thread.
        self.look_interval = LOOK_INTERVAL
        self.machine = {}
        self.midi_open = False
        self.midi_problem = None
        self.midi_opened_at = None
        self.fl_seen_at = None
        self.heard_at = None

    # --- running ------------------------------------------------------------------------

    async def start(self):
        """Start the page's server and, unless the installer comes first, the
        bridge and the watching; returns the page's address."""
        self.loop = asyncio.get_running_loop()
        self.quit_event = asyncio.Event()
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        try:
            with open(ICON, "rb") as f:
                self.icon = f.read()
        except OSError:
            self.icon = None  # the page works without it
        self.server = webui.Server(self.handle, "127.0.0.1", self.ui_port, logger=self.log)
        self.guard.port = await self.server.start()
        self.url = "http://127.0.0.1:%d/?t=%s" % (self.guard.port, self.token)
        if self.setup is None:
            self._begin()
        return self.url

    def _begin(self):
        """FLSync itself: the bridge, MIDI and the watching of the computer."""
        if self.bridge is not None:
            return
        self.bridge = bridge_mod.Bridge("127.0.0.1", self.relay_port, send_to_fl=None, logger=self._bridge_log,
                                        name=self.name, **self.bridge_options)
        self.bridge.app_version = VERSION
        if self.settings.get("auto_sync") is False:
            self.bridge.auto_sync = False  # turned off in the window
        self._hook(self.bridge)
        self._spawn(self._run_midi())
        self._spawn(self._look_around())
        if self.update_check:
            self._spawn(self._check_update())

    async def run(self, open_browser=True, action=None):
        await self.start()
        self._write_instance()
        self.say("FLSync är igång. Sidan finns på %s" % self.url)
        self.say("Stäng FLSync-fönstret (eller klicka Avsluta FLSync) för att avsluta.")
        if open_browser:
            await self._show()
        try:
            if action is None and self.setup is None:
                action = self._resume()
            if action is not None:
                self._spawn(action(self))  # quitting mustn't wait for it
            self._spawn(self._watch_window())
            await self.quit_event.wait()
        finally:
            await self.close()
            self._remove_instance()

    async def _show(self):
        """The page in FLSync's own window, or in the browser (no Edge or
        Chrome, or not wanted)."""
        if self.window_wanted:
            try:
                window = await asyncio.to_thread(self.system.open_window, self.url,
                                                 os.path.join(self.data_dir, "window"))
            except Exception as e:
                window = None
                self.log("FLSync-fönstret gick inte att öppna (%s), sidan öppnas i webbläsaren" % e)
            if window is not None:
                self.window, self.window_opened_at = window, time.monotonic()
                self._write_instance()  # with the window: a second start brings it forward
                return
        try:
            await asyncio.to_thread(self.system.open_browser, self.url)
        except Exception as e:  # no browser: the address is printed above
            self.say("Kunde inte öppna webbläsaren (%s). Öppna adressen ovan själv." % e)

    async def _watch_window(self):
        """FLSync ends with its window: when the window's browser ends, or when
        the page says it closes and no page comes back (a reload does). A
        browser that ends at once handed the window to one that ran already:
        then only the page tells."""
        if not self.window_wanted:
            return
        while True:
            await asyncio.sleep(WINDOW_POLL)
            now = time.monotonic()
            window = self.window
            if window is not None and window.poll() is not None:
                self.window = None
                if now - self.window_opened_at > WINDOW_HANDOFF:
                    self.log("FLSync-fönstret stängdes, så FLSync avslutas")
                    self.request_quit()
                    return
            if self.bye_at is not None and now - self.bye_at > BYE_GRACE and self.page_seen < self.bye_at:
                self.log("FLSync-fönstret stängdes, så FLSync avslutas")
                self.request_quit()
                return

    def _resume(self):
        """The session FLSync was in when it last ended without "Lämna sessionen"
        (the window closed, a crash, a restart of the computer): back into it."""
        resume = self.settings.get("resume")
        if resume == "host":
            self.log("Startar sessionen igen, som när FLSync avslutades")

            async def action(app):
                await app.host()
            return action
        code = self.settings.get("last_invite")
        if resume == "join" and isinstance(code, str) and code:
            try:
                found = invites.decode(code)
            except invites.InviteError:
                self.settings.set(resume=None)
                return None
            self.log("Går med i sessionen igen, som när FLSync avslutades")

            async def action(app):
                await app._rejoin(found)
            return action
        return None

    async def _rejoin(self, found):
        """Back into the session of invite `found` (resume): quietly, until its
        host answers. The page shows that it tries and can stop it; choosing
        Starta session or Gå med stops it too (_stop_resuming)."""
        self.resuming = found.code
        try:
            while self.resuming == found.code and self.mode is None:
                if not self.busy:
                    address, _ = await choose(found.addresses, found.port, found.password, self.name,
                                              self.probe_timeout)
                    if address is not None and self.resuming == found.code and self.mode is None and not self.busy:
                        self._joined_at(found, address)
                        return
                await asyncio.sleep(RESUME_RETRY)
        finally:
            if self.resuming == found.code:
                self.resuming = None

    def _stop_resuming(self):
        self.resuming = None

    def request_quit(self):
        if self.quit_event is not None:
            self.quit_event.set()

    async def close(self):
        if self.closing:
            return
        self.closing = True
        try:
            # Not "Lämna sessionen": started again, FLSync goes back into the session.
            await asyncio.wait_for(self.leave("FLSync avslutades", forget=False), ROUTER_WAIT + 8)
        except Exception:
            self.log("fel när sessionen avslutades:\n" + traceback.format_exc())
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.wait(list(self.tasks), timeout=5)
        if self.bridge is not None:
            self.bridge.close()  # run_midi did, unless it never ran
        if self.server is not None:
            await self.server.stop()
        if self.window is not None:
            try:
                await asyncio.to_thread(self.system.close_window, self.window)  # FLSync ended: its window goes
            except Exception:
                self.log("fel när FLSync-fönstret stängdes:\n" + traceback.format_exc())
        self.closed.set()

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            e = task.exception()
            self.log("fel:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))

    def _write_instance(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(instance_path(self.data_dir), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "port": self.guard.port, "token": self.token,
                           "window": getattr(self.window, "pid", None)}, f)
        except OSError:
            pass

    def _remove_instance(self):
        try:
            with open(instance_path(self.data_dir), encoding="utf-8") as f:
                mine = json.load(f).get("token") == self.token
            if mine:
                os.remove(instance_path(self.data_dir))
        except (OSError, ValueError, AttributeError):
            pass

    # --- the log -----------------------------------------------------------------------------

    def log(self, text, source="app"):
        stamp = time.strftime("%H:%M:%S")
        self.lines.append({"time": stamp, "source": source, "text": text})
        if self.echo:
            self.say(text, stamp)

    def say(self, text, stamp=None):
        """To the console window, if there is one."""
        if not self.echo:
            return
        try:
            print((stamp or time.strftime("%H:%M:%S")) + " " + text, flush=True)
        except (OSError, ValueError, AttributeError):
            pass  # no console (pythonw), or it went away

    def _bridge_log(self, text):
        if text.startswith("FL: "):  # the FL script's own log, through the bridge
            words = text[4:]
            try:
                words = json.loads(words) if words.startswith('"') else words
            except ValueError:
                pass
            self.log("FL: %s" % words, "fl")
        else:
            self.log(text, "bridge")

    def _midi_log(self, text):
        """bridge.run_midi's messages, which also say whether the port is open."""
        if text.startswith("MIDI-porten är öppen"):
            self.midi_open, self.midi_problem = True, None
            self.midi_opened_at = time.monotonic()
            text = "MIDI-porten %s är öppen, väntar på FL" % self.midi_port
        elif text.startswith("MIDI-porten slutade fungera"):
            self.midi_open = False
        elif text.endswith("Försöker igen ..."):
            self.midi_open = False
            self.midi_problem = text[:-len(". Försöker igen ...")]
        self.log(text, "midi")

    def _relay_log(self, text):
        self.log(text, "relay")

    # --- the bridge ----------------------------------------------------------------------------

    def _hook(self, bridge):
        """See everything the bridge tells FL, and keep FL out of any relay
        while no session is chosen (the bridge joins the relay when FL says hello)."""
        to_fl = bridge.to_fl
        start_session = bridge._start_session

        def spy(msg):
            try:
                self._heard(msg)
            except Exception:  # the page's view must never cut FL off
                self.log("fel:\n" + traceback.format_exc())
            to_fl(msg)

        def hello(msg):
            self.fl_hello = dict(msg)
            if self.target is None:
                now = time.monotonic()
                if self.lobby_said is None or now - self.lobby_said > LOBBY_REPEAT:
                    self.lobby_said = now
                    to_fl({"t": "link", "up": False, "msg": LOBBY})
                return  # FL says hello again every few seconds, until a session is chosen
            start_session(msg)

        bridge.to_fl = spy
        bridge._start_session = hello
        self._to_fl = to_fl

    async def _run_midi(self):
        try:
            await self.run_midi(self.bridge, self.midi_port, logger=self._midi_log)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.midi_open = False
            self.log("fel: MIDI-delen stannade:\n" + traceback.format_exc())

    def _use_relay(self, host, port, password):
        """Point the bridge at a relay. It joins when FL next says hello."""
        bridge = self.bridge
        self.target = (host, port, password)
        bridge.relay_host, bridge.relay_port, bridge.password = host, port, password
        bridge.name = self.name
        self.link_problem = None

    def _drop_session(self, reason):
        """Disconnect the bridge from the relay and tell FL."""
        bridge = self.bridge
        bridge.close_session()
        bridge._drop_files()
        if bridge.fl_last_heard is not None:
            self._to_fl({"t": "link", "up": False, "msg": reason})
            self.lobby_said = time.monotonic()
        self.peers, self.pen, self.my_name, self.link_problem = {}, None, None, None
        self.session_problem = self.saved = None

    def _happened(self, text, kind=None):
        """A line of the page's Händelser. One of the same `kind` as the newest
        replaces it (a version shared every few seconds is one line, the latest)."""
        entry = {"time": clock_text(time.time()), "text": text, "kind": kind}
        if self.activity and kind is not None and self.activity[0]["kind"] == kind:
            self.activity[0] = entry
        elif not self.activity or self.activity[0]["text"] != text:
            self.activity.appendleft(entry)

    def _heard(self, msg):
        """Something the bridge tells FL: keep the page's view of the session."""
        kind = msg.get("t")
        if kind == "welcome":
            self.my_name = msg.get("name")
            self.peers = {p["id"]: p for p in msg.get("peers") or [] if isinstance(p, dict)
                          and isinstance(p.get("id"), str)}
            self.pen = msg.get("pen") if isinstance(msg.get("pen"), dict) and msg["pen"].get("id") else None
            self.link_problem = self.session_problem = None
            self.lobby_said = None
            others = ", ".join(str(p.get("name")) for p in self.peers.values())
            self.log("Med i sessionen som %s%s" % (self.my_name, " tillsammans med " + others if others
                                                    else " (ensam än så länge)"))
            names = [p.get("name") for p in self.peers.values()]
            self._happened("Du är med i sessionen" + (" med %s" % names_text(names) if names else ""))
        elif kind == "peer_join" and isinstance(msg.get("id"), str):
            self.peers[msg["id"]] = msg
            self._happened("%s anslöt" % msg.get("name"))
        elif kind == "peer_leave":
            gone = self.peers.pop(msg.get("id"), None) or msg
            if self.pen and self.pen.get("id") == msg.get("id"):
                self.pen = None
            if gone.get("name"):
                self._happened("%s lämnade sessionen" % gone.get("name"))
        elif kind == "pen":
            self.pen = dict(msg) if msg.get("id") else None
        elif kind == "snapshot":
            key = (msg.get("v"), msg.get("from"))
            what = [str(w) for w in msg.get("what") or [] if isinstance(w, str) and w][:4]  # once it's fetched
            if (key not in self.seen_at or what) and msg.get("from") != self.bridge.my_id and msg.get("by"):
                self._happened("%s delade version %s%s" % (msg.get("by"), msg.get("v"),
                                                          " (%s)" % ", ".join(what) if what else ""),
                               "shared:%s" % msg.get("by"))
            self.seen_at.setdefault(key, time.time())
            self.notices[key] = dict(self.notices.get(key, {}), **msg)
            for old in list(self.notices)[:-10]:
                self.notices.pop(old, None)
        elif kind == "loaded":
            self.loaded = msg
            self.log("FL har öppnat version %s" % msg.get("v"))
            self._happened("FL öppnade version %s" % msg.get("v"))
        elif kind == "saved" and msg.get("outcome") != "unchanged":
            self.saved = {"time": clock_text(time.time()), "text": saved_text(msg),
                          "state": "bad" if msg.get("outcome") in ("failed", "replaced", "unrelated") else "ok"}
            line = saved_event(msg)
            if line:
                self._happened(line, "saved:%s" % msg.get("outcome"))
        elif kind == "added":
            what = [str(w) for w in msg.get("what") or [] if w][:5]
            self._happened("%s lade till %s, som kom in live" % (msg.get("by") or "Någon",
                                                                names_text(what) if what else "kanaler"))
        elif kind == "pull" and not msg.get("cancel"):
            self._happened("%s ändringar hämtas in när du har stoppat och inte rör något"
                           % capital(genitive(msg.get("by"))), "pull")
        elif kind == "notes":
            mine = msg.get("from") == self.bridge.my_id
            try:
                channel = int(msg.get("c")) + 1
            except (TypeError, ValueError):
                channel = msg.get("c")
            text = "%s skickade %s noter till kanal %s i mönster %s" % (
                "Du" if mine else msg.get("by"), msg.get("n"), channel, msg.get("p"))
            self.notes_seen.appendleft({"time": clock_text(time.time()), "text": text, "mine": mine})
            self._happened(text)
        elif kind == "link" and self.target is not None:
            if msg.get("up") is False and self.peers:
                self._happened("Tappade kontakten med sessionen, försöker igen", "link")
            self.link_problem = msg.get("msg")
            self.peers, self.pen = {}, None
        elif kind == "error":
            self._relay_error(msg)

    def _relay_error(self, msg):
        text = msg.get("msg") if isinstance(msg.get("msg"), str) else ""
        if msg.get("code") == "password":
            problem = ("password", "Fel lösenord för sessionen",
                       "Värden har gjort en ny kod eller startat en ny session.",
                       ["Be om den senaste inbjudan och välj Gå med igen."])
        elif msg.get("code") == "proto" or "protokoll" in text:
            problem = ("proto", "Olika versioner av FLSync", text,
                       ["Uppdatera FLSync hos er båda till samma version (kör Installera FLSync) och starta om "
                        "FL Studio, eller klicka Reload script under View > Script output i FL."])
        else:
            return
        self.session_problem = {"state": "bad", "title": problem[1], "detail": problem[2], "fix": problem[3]}
        if self.mode == "join":
            # Joining again would only be refused again: leave, and say why.
            self._fail(*problem)
            self._spawn(self.leave("sessionen tog inte emot FL: " + problem[1].lower(), keep_error=True))

    # --- actions ------------------------------------------------------------------------------

    def _fail(self, code, title, text="", fix=None):
        self.errors += 1
        self.error = {"id": self.errors, "code": code, "title": title, "text": text,
                      "fix": fix if isinstance(fix, list) else ([fix] if fix else [])}
        self.log("%s. %s" % (title, text) if text else title)
        return {"ok": False, "error": self.error}

    def _done(self):
        self.error = None
        return {"ok": True}

    async def host(self, port=None):
        """Start a session here: the relay, the router, the invite."""
        if self.mode or self.busy:
            return self._fail("busy", "Du är redan med i en session", "Lämna den först.")
        self._stop_resuming()
        port = port or self.relay_port
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            return self._fail("port", "Ogiltig port", "Porten ska vara ett tal mellan 1 och 65535.")
        self.busy = "Startar sessionen ..."
        try:
            return await self._host(port)
        finally:
            self.busy = None

    async def _host(self, port):
        taken = self._port_taken_problem(port)
        if await self._port_taken(port):
            return self._fail(*taken)
        password = self.settings.get("host_password")
        if not isinstance(password, str) or len(password) != 2 * invites.PASSWORD_BYTES:
            password = invites.new_password()
            self.settings.set(host_password=password)
        relay_mod.log = self._relay_log
        # The versions stay on disk, so a host who starts again (or comes back
        # after a crash) still has the project for those who join.
        relay = relay_mod.Relay(verbose=self.verbose, max_blob=self.max_blob, password=password,
                                store=os.path.join(self.data_dir, "relay-store"), persist=True)
        for attempt in range(BIND_TRIES):
            try:
                await relay.start(self.bind, port)
                break
            except OSError as e:
                in_use = e.errno in (errno.EADDRINUSE, 10048) or getattr(e, "winerror", None) == 10048
                if in_use and attempt < BIND_TRIES - 1:
                    # Nothing listens there (checked above): Windows lets a port go a moment after
                    # it was last used, e.g. when you leave and start a session again at once.
                    await asyncio.sleep(0.2)
                    continue
                relay.close_store()
                if in_use:
                    return self._fail(*taken)
                return self._fail("relay", "Kunde inte starta sessionen", os_reason(e))
        self.generation += 1
        self.relay = relay
        self.relay_task = self._spawn(self._serve(relay))
        self.mode = "host"
        self.relay_port = port
        self.invite, self.invite_addresses, self.mapping = None, [], None
        self._use_relay("127.0.0.1", port, password)
        self.settings.set(mode="host", relay_port=port, resume="host")
        self.log("Sessionen är startad (port %d)" % port)
        self.invite_task = self._spawn(self._prepare_invite(self.generation, port, password))
        return self._done()

    def _port_taken_problem(self, port):
        return ("port", "Port %d används redan" % port,
                "Ett annat program på den här datorn lyssnar redan på port %d, kanske relay.py eller ett "
                "annat FLSync-fönster." % port,
                ["Stäng det andra programmet och försök igen, eller välj en annan port under Avancerat "
                 "(den hamnar i inbjudan)."])

    async def _port_taken(self, port):
        """Something on this computer already answers on the port. On Windows our
        bind to all interfaces would succeed anyway, and our own bridge would
        then reach the other program on 127.0.0.1."""
        hosts = ["127.0.0.1"]
        if self.bind not in ("127.0.0.1", "localhost"):
            try:
                local = await asyncio.to_thread(self.system.local_addresses)
            except Exception:
                local = []
            hosts += [a for a in local if invites.kind(a) in ("lan", "tailscale")]

        async def answers(host):
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 0.5)
            except (OSError, asyncio.TimeoutError):
                return False
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, asyncio.TimeoutError):
                pass
            return True

        return any(await asyncio.gather(*(answers(h) for h in hosts)))

    async def _serve(self, relay):
        try:
            await relay.serve_forever()
        except asyncio.CancelledError:
            pass

    async def _prepare_invite(self, generation, port, password):
        """Ask the router to open the port, then make the invite: LAN,
        Tailscale and public addresses. Whatever happens, the session runs."""
        local = await asyncio.to_thread(self.system.local_addresses)
        default = await asyncio.to_thread(self.system.default_address)
        if generation != self.generation:
            return  # the session ended meanwhile
        lan = [a for a in invites.host_addresses(local, default) if invites.kind(a) == "lan"]
        where = lan[0] if lan else "den här datorn"
        if isinstance(self.mapper, upnp.PortMapper) and tuple(self.mapper.sources) == ("",):
            # Ask on every network, not only the one Windows picks for multicast
            # (a VPN or virtual adapter would get the search and no router answers).
            self.mapper.sources = ("",) + tuple(lan)
        public, mapping, lookup = None, None, self.ip_lookup
        if self.mapper:
            self.router = {"state": "wait", "title": "Frågar routern om port %d (UPnP) ..." % port}
            problem = None
            self.asking_router = True
            try:
                mapping = await asyncio.to_thread(self.mapper.open, port)
            except upnp.UpnpError as e:
                problem = str(e)
            except Exception as e:  # a router answering nonsense must not stop the session
                problem = "oväntat fel (%s)" % e
            finally:
                self.asking_router = False
            if generation != self.generation:
                if mapping is not None:
                    await self._close_mapping(mapping)
                return
            if mapping is not None:
                self.mapping = mapping
                if mapping.lease:
                    self._spawn(self._renew(generation, mapping))
                if mapping.external_ip and invites.is_public(mapping.external_ip):
                    public = mapping.external_ip
                    self.router = {"state": "ok", "title": "Routern skickar port %d hit (UPnP), så andra når dig "
                                                            "via internet på %s" % (port, public)}
                    lookup = False
                else:
                    lookup = False  # what a lookup finds is someone else's address
                    self.router = {
                        "state": "bad", "title": "Routern har ingen egen publik adress",
                        "detail": "Routern öppnade porten, men dess adress utåt är %s. Din internetleverantör delar "
                                  "alltså en publik adress mellan flera kunder (CGNAT), eller så sitter routern "
                                  "bakom en router till." % (mapping.external_ip or "okänd"),
                        "fix": ["Använd Tailscale (gratis) hos er båda: dess adress kommer med i inbjudan.",
                                "Eller låt någon annan vara värd. På samma nätverk fungerar det ändå."]}
            else:
                self.router = {
                    "state": "bad", "title": "Routern öppnade inte porten automatiskt",
                    "detail": problem,
                    "fix": ["På samma nätverk fungerar det ändå.",
                            "Via internet: öppna port %d (TCP) i routern mot %s, eller använd Tailscale hos "
                            "er båda." % (port, where)]}
        else:
            self.router = {"state": "off", "title": "Routern tillfrågas inte (UPnP är avstängt)",
                           "fix": ["Via internet: öppna port %d (TCP) i routern mot %s, eller använd Tailscale."
                                   % (port, where)]}
        if public is None and lookup:
            try:
                public = await asyncio.to_thread(self.system.public_address)
            except Exception:
                public = None
        if generation != self.generation:
            return
        addresses = invites.host_addresses(local, default, public)
        self.invite_note = None
        if not addresses:
            addresses = ["127.0.0.1"]  # no network at all: still good for a second FL on this computer
            self.invite_note = ("Datorn har ingen nätverksadress, så bara den här datorn kan gå med. Anslut "
                                "datorn till ett nätverk och starta sessionen igen.")
        self.invite_addresses = addresses
        self.invite = invites.encode(addresses, port, bytes.fromhex(password))
        self.log("Inbjudan är klar: %s" % self.invite)

    async def _renew(self, generation, mapping):
        while generation == self.generation and self.mapping is mapping:
            await asyncio.sleep(max(60, mapping.lease / 2))
            if generation != self.generation or self.mapping is not mapping:
                return
            try:
                await asyncio.to_thread(self.mapper.renew, mapping)
            except upnp.UpnpError as e:
                self.log("Routern förnyade inte portöppningen: %s" % e)

    async def _close_mapping(self, mapping):
        try:
            await asyncio.to_thread(self.mapper.close, mapping)
            self.log("Port %d är stängd i routern igen" % mapping.port)
        except Exception as e:
            self.log("Kunde inte stänga port %d i routern: %s" % (mapping.port, e))

    async def join(self, code):
        """Join the session of an invite: the first of its addresses that answers right."""
        if self.mode or self.busy:
            return self._fail("busy", "Du är redan med i en session", "Lämna den först.")
        self._stop_resuming()  # your choice wins over going back into the last session
        try:
            found = invites.decode(code)
        except invites.InviteError as e:
            return self._fail("code", "Inbjudan går inte att läsa", str(e))
        self.busy = "Ansluter till sessionen ..."
        try:
            address, results = await choose(found.addresses, found.port, found.password, self.name,
                                            self.probe_timeout)
        finally:
            self.busy = None
        if self.mode:
            return self._fail("busy", "Du är redan med i en session", "Lämna den först.")
        if address is None:
            return self._fail(*join_problem(results, found.port))
        return self._joined_at(found, address)

    def _joined_at(self, found, address):
        """The host of invite `found` answers at `address`: the bridge joins it."""
        self.generation += 1
        self.mode = "join"
        self.joined = address
        self._use_relay(address, found.port, found.password)
        self.settings.set(mode="join", last_invite=found.code, resume="join")
        self.log("Värdens FLSync svarar på %s:%d (%s)" % (address, found.port,
                                                          invites.KIND_NAMES.get(invites.kind(address))))
        return self._done()

    async def leave(self, reason="du lämnade sessionen i FLSync", keep_error=False, forget=True):
        """End our part of the session: the bridge leaves the relay, a hosted
        relay stops and the router's port is closed again. `forget`: FLSync
        doesn't go back into it by itself when it starts again."""
        if forget:
            self.settings.set(resume=None)
            self._stop_resuming()
        if self.mode is None:
            return self._done()
        self.generation += 1
        mode, relay, relay_task, mapping = self.mode, self.relay, self.relay_task, self.mapping
        self.mode = self.target = self.relay = self.relay_task = self.mapping = None
        self.invite, self.invite_addresses, self.invite_note, self.router, self.joined = None, [], None, None, None
        self.open_warning = None
        self._drop_session(reason)
        if relay is not None:
            try:
                await relay.stop()  # the others are disconnected
            except Exception:
                self.log("fel när relayn stoppades:\n" + traceback.format_exc())
        if relay_task is not None:
            relay_task.cancel()
        if mapping is not None:
            await self._close_mapping(mapping)
        elif self.asking_router and self.invite_task is not None:
            # The router may be opening the port right now: once it has, the
            # invite task sees the session is over and closes it again.
            try:
                await asyncio.wait_for(asyncio.shield(self.invite_task), ROUTER_WAIT)
            except Exception:
                pass
        self.log("Sessionen är avslutad" if mode == "host" else "Du lämnade sessionen")
        return {"ok": True} if keep_error else self._done()

    async def new_code(self):
        """A new password for the hosted session: new connections need the new
        invite, those already in stay."""
        if self.mode != "host" or self.relay is None:
            return self._fail("busy", "Du är inte värd för någon session")
        password = invites.new_password()
        self.settings.set(host_password=password)
        self.relay.password = password
        host, port, _ = self.target
        self._use_relay(host, port, password)
        if self.invite_addresses:
            self.invite = invites.encode(self.invite_addresses, port, bytes.fromhex(password))
            self.log("Ny inbjudan: %s" % self.invite)
        return self._done()

    async def set_name(self, name):
        name = clean_name(name) or clean_name(self.system.user_name())
        changed = name != self.name
        self.name = name
        self.settings.set(name=name)
        if changed and self.bridge is not None:
            self.bridge.name = name
            if self.bridge.in_session():
                # The others learn names when someone joins: join again under the new one.
                self._drop_session("byter namn till %s" % name)
            self.log("Ditt namn är nu %s" % name)
        return self._done()

    def share_mine(self):
        """"Dela mitt projekt": the project FL has open becomes the session's,
        though it isn't (it replaces the session's newest version)."""
        if not self.bridge.in_session():
            return self._fail("share", "FL är inte med i någon session än")
        if not self.bridge.share_mine():
            return self._fail("share", "FL har inget sparat projekt att dela",
                              "Spara projektet i FL (Ctrl+S) först.")
        self.log("Delar projektet du har öppet med sessionen, i stället för sessionens")
        return self._done()

    async def open_version(self, folder=False):
        """Open the newest version someone else shared in FL (or its folder)."""
        newest, bridge = self.bridge.newest, self.bridge
        if not newest:
            return self._fail("version", "Ingen version har delats än",
                              "Den som har projektet sparar det i FL (Ctrl+S), så delas det med sessionen.")
        if newest["flp"] in bridge.shared and not newest.get("merged"):
            return self._fail("version", "Det här är din egen version", "Den har du redan öppen i FL.")
        written = bridge.received.get(newest["flp"])
        if folder and written and os.path.isdir(os.path.dirname(written)):
            await asyncio.to_thread(self.system.open_file, os.path.dirname(written))
            return self._done()
        path = await bridge.version_file(newest)
        if not path:
            return self._fail("version", "Versionen är inte hämtad än",
                              "Den hämtas medan FL är med i sessionen. Vänta lite (se loggen) och försök igen.")
        if folder:
            await asyncio.to_thread(self.system.open_file, os.path.dirname(path))
            return self._done()
        before = await asyncio.to_thread(self.system.processes, winsys.FL_PROGRAMS)
        try:
            await asyncio.to_thread(self.system.open_file, path)
        except OSError as e:
            return self._fail("open", "Kunde inte öppna versionen", "%s: %s" % (path, os_reason(e)),
                              ["Öppna den i FL med File > Open."])
        self.open_warning = None
        self.log("Öppnar %s" % path)
        if before:
            self._spawn(self._watch_new_window({p.pid for p in before}, path))
        return self._done()

    async def _watch_new_window(self, before, path):
        """If FL started a second time to open the file, say how to open it in
        the FL that runs. An FL that only hands the file to the running one and
        ends at once doesn't count."""
        deadline = time.monotonic() + OPEN_WATCH
        appeared = {}  # pid -> when first seen
        while time.monotonic() < deadline:
            await asyncio.sleep(self.open_poll)
            now = await asyncio.to_thread(self.system.processes, winsys.FL_PROGRAMS)
            if now is None:
                return
            new = {p.pid for p in now} - before
            appeared = {pid: appeared.get(pid, time.monotonic()) for pid in new}
            if any(time.monotonic() - seen >= self.second_fl_stays for seen in appeared.values()):
                self.open_warning = {
                    "title": "FL Studio öppnade versionen i ett nytt fönster",
                    "text": "Stäng det nya FL-fönstret (spara inte). Öppna sedan versionen i det FL du redan har "
                            "igång: File > Open och välj %s. Du kan också dra filen från mappen in i FL." % path}
                self.log("FL startade ett nytt fönster för versionen")
                return

    # --- the page --------------------------------------------------------------------------------

    async def handle(self, request):
        if request.path == "/api/bye":
            return self._bye(request)
        api = request.path.startswith("/api/")
        problem = self.guard.problem(request, api=api)
        if problem:
            return webui.text_response(403, "FLSync: %s" % problem)
        if request.path in ("/", "/index.html"):
            if request.method != "GET":
                return webui.text_response(405, "bara GET")
            return self._page()
        if request.path == "/favicon.ico" and request.method == "GET" and self.icon:
            return webui.Response(200, self.icon, "image/x-icon")
        if not api:
            return webui.text_response(404, "finns inte")
        name = request.path[len("/api/"):]
        if request.method == "GET":
            if name == "status":
                self.page_seen = time.monotonic()
                return webui.json_response(self.status())
            if name == "ping":
                return webui.json_response({"ok": True, "app": "FLSync", "version": VERSION})
            return webui.text_response(404, "finns inte")
        if request.method != "POST":
            return webui.text_response(405, "bara GET och POST")
        if not request.headers.get("content-type", "").startswith("application/json"):
            return webui.text_response(400, "skicka JSON")
        try:
            body = request.json()
        except (ValueError, UnicodeDecodeError):
            return webui.text_response(400, "trasig JSON")
        if self.setup is not None and name not in ("setup", "quit", "window"):
            return webui.json_response({"ok": False}, 400)  # the installer first: no FLSync to act on yet
        if name == "host":
            port = body.get("port")
            result = await self.host(port if isinstance(port, int) and not isinstance(port, bool) else None)
        elif name == "join":
            result = await self.join(body.get("code") if isinstance(body.get("code"), str) else "")
        elif name == "leave":
            result = await self.leave()
        elif name == "open":
            result = await self.open_version(folder=bool(body.get("folder")))
        elif name == "share":
            result = self.share_mine()
        elif name == "name":
            result = await self.set_name(body.get("name"))
        elif name == "newcode":
            result = await self.new_code()
        elif name == "quit":
            self.loop.call_later(QUIT_DELAY, self.request_quit)
            result = {"ok": True}
        elif name == "setup":
            result = self._setup_action(body)
        elif name == "window":
            result = await self._window_action()
        elif name == "autosync":
            result = self.set_auto_sync(body.get("on") is True)
        elif name == "update":
            result = self._open_update()
        else:
            return webui.text_response(404, "finns inte")
        return webui.json_response(result, 200 if result.get("ok") else 400)

    def _bye(self, request):
        """The page's last words as its window closes (navigator.sendBeacon: a
        POST without our header, so the token comes in the address)."""
        given = (request.query.get("t") or [""])[0].encode("utf-8", "replace")
        if (request.method == "POST" and self.guard.problem(request, api=False) is None
                and hmac.compare_digest(given, self.token.encode("utf-8"))):
            self.bye_at = time.monotonic()
            return webui.json_response({"ok": True})
        return webui.text_response(403, "FLSync: fel nyckel")

    def _setup_action(self, body):
        """The installer's buttons: begin, answer (yes), cancel, and launch
        (FLSync itself, in this window, once installing is done)."""
        run, action = self.setup, body.get("action")
        if run is None:
            return {"ok": False}
        if action == "begin":
            ok = run.begin()
        elif action == "answer":
            ok = run.answer(bool(body.get("yes")))
        elif action == "cancel":
            run.cancel()
            ok = True
        elif action == "launch" and run.kind == "install" and run.state in ("ready", "done"):
            self.setup = None
            self._begin()
            self.log("FLSync är igång")
            ok = True
        else:
            ok = False
        return {"ok": bool(ok)}

    def set_auto_sync(self, on):
        """The window's "Spara och hämta automatiskt" (docs/phase5.md)."""
        if self.bridge is None:
            return {"ok": False}
        self.bridge.set_auto_sync(on)
        self.settings.set(auto_sync=on)
        self.log("Automatisk sparning och hämtning är %s" % ("på" if on else "av"))
        return {"ok": True}

    def _open_update(self):
        """The newer FLSync's page, in the browser: its zip is there."""
        if self.update is None:
            return {"ok": False}
        try:
            self.system.open_browser(self.update["url"])
        except Exception as e:
            self.log("Kunde inte öppna %s: %s" % (self.update["url"], e))
            return {"ok": False}
        return {"ok": True}

    async def _check_update(self):
        """Whether GitHub has a newer FLSync than this one (asked once; nothing
        is downloaded). Quietly nothing if it can't be told."""
        try:
            info = await asyncio.to_thread(fetch_json, RELEASES_API, UPDATE_TIMEOUT)
        except (OSError, ValueError):
            return
        tag = info.get("tag_name") if isinstance(info, dict) else None
        newest = version_tuple(tag[1:] if isinstance(tag, str) and tag[:1] == "v" else tag)
        if newest and newest > (version_tuple(VERSION) or ()):
            url = info.get("html_url")
            if not (isinstance(url, str) and url.startswith(REPO + "/")):
                url = RELEASES_PAGE
            self.update = {"version": ".".join(str(n) for n in newest), "url": url}
            self.log("FLSync %s finns (du har %s): ladda ner den i FLSync-fönstret" % (self.update["version"], VERSION))

    async def _window_action(self):
        """FLSync started again while it runs: show the window (a new one if it
        was handed to another browser or is gone; the second start brings an
        open one forward itself)."""
        if self.window is None or self.window.poll() is not None:
            await self._show()
        return {"ok": True}

    def _page(self):
        nonce = secrets.token_urlsafe(16)
        policy = ("default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'self'; "
                  "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % nonce)
        return webui.Response(200, self.page.replace("__NONCE__", nonce), "text/html; charset=utf-8",
                              {"Content-Security-Policy": policy})

    # --- what the page shows -----------------------------------------------------------------------

    async def _look_around(self):
        while True:
            try:
                self.machine = await asyncio.to_thread(self._look)
                for major, port in self.machine.get("configured") or ():
                    self.log("%s är nu inställt för FLSync (MIDI-porten %s på port %d). Starta det, så hörs det."
                             % (fl_label(major), self.midi_port, port))
            except Exception:
                self.log("fel:\n" + traceback.format_exc())
            await asyncio.sleep(self.look_interval)

    def _look(self):
        """On a worker thread: programs, MIDI ports, FL's MIDI settings (of the
        FL version that runs). An FL version whose settings lack FLSync (one
        installed after FLSync) gets it as soon as it isn't running: FL writes
        its own settings back when it closes."""
        processes = self.system.processes()
        majors = {p.major for p in processes or () if p.name in winsys.FL_PROGRAMS}
        needs = self.system.fl_setup_needed(self.midi_port) if processes is not None else {}
        configured = []
        if processes is not None and None not in majors:  # the version of every FL that runs is known
            for major, need in sorted(needs.items()):
                if need and major not in majors:
                    port = self.system.configure_fl(major)
                    if port is not None:
                        configured.append((major, port))
                        needs[major] = False
        running = sorted(m for m in majors if m is not None)
        settings = self.system.fl_midi_settings(self.midi_port, running[0] if len(running) == 1 else None)
        return {"processes": processes, "ports": self.system.midi_ports(), "settings": settings,
                "needs": needs, "configured": configured, "fl_majors": running}

    def facts(self):
        """What diagnose.checks() needs, from the bridge and the last look around."""
        now = time.monotonic()
        bridge = self.bridge
        processes = self.machine.get("processes")
        fls = None if processes is None else [p for p in processes if p.name in winsys.FL_PROGRAMS]
        loopmidi = None if processes is None else [p for p in processes if p.name == winsys.LOOPMIDI]
        heard = bridge is not None and bridge.fl_last_heard is not None
        if heard:
            self.heard_at = now
        if fls:
            self.fl_seen_at = self.fl_seen_at or now
        else:
            self.fl_seen_at = None
        fl_started = min((p.started for p in fls or [] if p.started), default=None)
        silent = None
        if fls and self.midi_open and not heard:
            since = [self.midi_opened_at or now, self.heard_at or 0]
            if fl_started is None:  # then FL counts from when we first saw it
                since.append(self.fl_seen_at or now)
            silent = now - max(since)
        age = time.time() - fl_started if fl_started else (now - self.fl_seen_at if self.fl_seen_at else None)
        needs = self.machine.get("needs") or {}
        unset = [fl_label(m) for m in self.machine.get("fl_majors") or () if needs.get(m)]
        majors = self.machine.get("fl_majors") or []
        return {
            "loopmidi": None if loopmidi is None else bool(loopmidi),
            "loopmidi_started": min((p.started for p in loopmidi or [] if p.started), default=None),
            "ports": self.machine.get("ports"),
            "midi_open": self.midi_open,
            "midi_problem": self.midi_problem,
            "fl": None if fls is None else bool(fls),
            "fl_started": fl_started,
            "fl_age": age,
            "fl_heard": heard,
            "fl_version": self.fl_hello.get("flv") if heard else None,
            "fl_label": fl_label(majors[0]) if len(majors) == 1 else None,  # which FL runs
            "fl_unset": unset[0] if unset and not heard else None,  # ... and lacks FLSync in its settings
            "silent_for": silent,
            "settings": self.machine.get("settings"),
            "mode": self.mode,
            "relay": self._relay_check(),
            "session": self._session_check(),
        }

    def _relay_check(self):
        if self.mode == "host":
            return {"state": "ok", "title": "Sessionen körs här på port %d, skyddad med lösenord" % self.relay_port}
        if self.mode == "join":
            host, port, _ = self.target
            where = "%s:%d (%s)" % (host, port, invites.KIND_NAMES.get(invites.kind(host), "?"))
            if self.link_problem and not self.bridge.in_session():
                return {"state": "bad", "title": "Värdens FLSync svarar inte på %s" % where,
                        "detail": self.link_problem,
                        "fix": ["Kontrollera att värden fortfarande har FLSync igång med sessionen startad. "
                                "FLSync försöker igen av sig självt.",
                                "Har värden startat om datorn eller bytt nätverk: be om en ny inbjudan."]}
            return {"state": "ok", "title": "Värdens FLSync på %s" % where}
        return None

    def _session_check(self):
        if self.mode is None:
            return {"state": "off", "title": "Ingen session än", "fix": ["Välj Starta session eller Gå med."]}
        bridge = self.bridge
        if bridge.in_session():
            others = len(self.peers)
            return {"state": "ok", "title": "Med i sessionen som %s%s" % (
                self.my_name or self.name, ", %d till med" % others if others else ", ensam än så länge")}
        if self.session_problem:
            return self.session_problem
        if bridge.fl_last_heard is None:
            return {"state": "wait", "title": "Ansluter när FL Studio hörs på MIDI"}
        return {"state": "wait", "title": "Ansluter FL till sessionen ..."}

    def _people(self):
        bridge = self.bridge
        my_id = bridge.my_id if bridge.in_session() else None
        if self.mode == "host" and self.relay is not None:
            people = [c.info() for c in self.relay.clients.values()]
        elif my_id:
            me = {"id": my_id, "name": self.my_name or self.name, "app": "fl", "flv": self.fl_hello.get("flv")}
            people = [me] + list(self.peers.values())
        else:
            return []
        pen = self._pen_holder()
        return [{"name": str(p.get("name")), "app": p.get("app") or "fl", "flv": p.get("flv"),
                 "ver": VERSION if p.get("id") == my_id else p.get("ver"),
                 "me": p.get("id") == my_id, "pen": pen is not None and p.get("id") == pen.get("id")}
                for p in people]

    def _pen_holder(self):
        if self.pen and self.bridge.in_session():
            return self.pen
        if self.mode == "host" and self.relay is not None and self.relay.pen in self.relay.clients:
            holder = self.relay.clients[self.relay.pen]
            return {"id": holder.id, "name": holder.name}
        return None

    def _pen_status(self):
        pen = self._pen_holder()
        if not pen:
            return None
        name = pen.get("name")
        if pen.get("id") == self.bridge.my_id and self.bridge.in_session():
            return {"text": "Du ändrar projektets struktur (kanaler eller plugins). Spara (Ctrl+S) i FL för att "
                            "dela ändringen med de andra."}
        if pen.get("busy"):
            return {"text": "%s ändrar redan projektets struktur. Din ändring kan skrivas över när %s sparar."
                            % (name, name)}
        return {"text": "%s ändrar projektets struktur. När %s har sparat: öppna den nya versionen."
                        % (name, name)}

    def _people_note(self, people):
        """A note when the FL versions differ (builds of one release don't count):
        it works, each one's saves are merged in their own FL's version, but
        what only the newer FL has may be missing in the older. First, though,
        another FLSync than ours: that one is worth updating at once."""
        others = [p for p in people if not p["me"] and p["app"] == "fl" and p.get("ver") != VERSION]
        if others:
            p = others[0]
            theirs = "FLSync %s" % p["ver"] if p.get("ver") else "en äldre FLSync"
            return ("%s har %s och du %s. Uppdatera båda till den senaste versionen (%s), annars kan delar av synken "
                    "inte fungera." % (p["name"], theirs, VERSION, RELEASES_PAGE))
        mine = version_tuple(self.fl_hello.get("flv")) if self.bridge.in_session() else None
        if not mine:
            return None
        release = lambda p: version_tuple(p["flv"])[:3] if version_tuple(p["flv"]) else None  # noqa: E731
        newer = [p for p in people if not p["me"] and release(p) and release(p) > mine[:3]]
        if newer:
            return ("%s har en nyare FL (%s) än du (%s). Det går bra att jobba ihop: när du sparar (Ctrl+S) slås "
                    "ändringarna ihop i din FL-version. Plugins och funktioner som bara finns i den nyare FL kan "
                    "saknas hos dig." % (newer[0]["name"], newer[0]["flv"], self.fl_hello.get("flv")))
        older = [p for p in people if not p["me"] and release(p) and release(p) < mine[:3]]
        if older:
            return ("%s har en äldre FL (%s) än du (%s). Det går bra att jobba ihop: när hen sparar slås "
                    "ändringarna ihop i hens FL-version. Plugins och funktioner som bara finns i din FL kan saknas "
                    "hos hen." % (older[0]["name"], older[0]["flv"], self.fl_hello.get("flv")))
        return None

    def _sync_status(self):
        """A warning about the project FL has: {text, share (offer Dela mitt projekt)}, or None."""
        bridge = self.bridge
        if not bridge.in_session():
            return None
        if bridge.unrelated:
            return {"text": "Projektet du har öppet i FL hör inte till sessionen, så det du sparar delas inte. Öppna "
                            "sessionens version nedan, eller spara två gånger i rad i FL (Ctrl+S) eller klicka Dela "
                            "mitt projekt om ditt projekt ska bli sessionens (de andra får då öppna det).",
                    "share": True}
        if not bridge.fl_fp or not bridge.session_fp or bridge.fl_fp == bridge.session_fp:
            return None
        pen = self._pen_holder()
        if pen and pen.get("id") == bridge.my_id:
            return None  # the pen's text says it
        newest = bridge.newest
        if newest and newest.get("fp") == bridge.session_fp and newest["flp"] not in bridge.shared:
            if bridge.base is not None:
                return {"text": "Synken är pausad: %s version har andra kanaler eller plugins. Tryck Ctrl+S i FL, så "
                                "slås dina ändringar ihop med den och den öppnas." % genitive(newest.get("by")),
                        "share": False}
            return {"text": "Synken är pausad tills du öppnar %s version av projektet (Öppna senaste versionen "
                            "nedan)." % genitive(newest.get("by")), "share": False}
        return {"text": "Synken är pausad: ditt projekt har andra kanaler eller plugins än sessionens. Öppna samma "
                        "projekt som de andra, eller spara ditt (Ctrl+S) för att dela det.", "share": False}

    def _autosync_status(self):
        """What FL saving by itself and getting others' versions in does now
        (docs/phase5.md), for the window's checkbox."""
        bridge = self.bridge
        if not self.mode or bridge is None:
            return None
        flv = version_tuple(self.fl_hello.get("flv"))
        newest = bridge.newest if bridge.in_session() else None
        if not bridge.auto_sync:
            text = ("Av: det som inte synkas live delas när du sparar (Ctrl+S), och andras versioner får du med Ctrl+S "
                    "eller Öppna senaste versionen.")
        elif bridge.autosave_failures >= bridge_mod.AUTOSAVE_TRIES:
            text = "FL sparade inte när FLSync bad om det, så det görs inte nu. Spara med Ctrl+S för att dela."
        elif bridge.autosave_failures:
            text = "FL sparade inte senast FLSync bad om det (en dialog var kanske öppen). FLSync försöker igen om en stund."
        elif flv and flv[:3] < AUTOSAVE_FROM:
            text = ("Din FL (%s) kan inte spara av sig själv: spara med Ctrl+S för att dela. FL 2026.1.7 eller senare "
                    "gör det automatiskt." % self.fl_hello.get("flv"))
        elif newest and bridge.pull_sent == newest["flp"] and newest["flp"] != bridge.base:
            text = "%s ändringar hämtas in när du har stoppat uppspelningen och inte rör något." % capital(
                genitive(newest.get("by")))
        elif bridge.autosave_sent:
            text = ("På: FL sparar själv det som inte synkas live (klipp, noter, nya plugins, samples) när du slutat "
                    "ändra, och de andras ändringar laddas in när du har stoppat uppspelningen.")
        elif bridge.in_session() and not bridge._other_fls():
            text = "På när någon mer är med i sessionen."
        else:
            text = ("På när ditt projekt är sessionens och sparat som en fil (spara en gång med Ctrl+S, eller öppna "
                    "sessionens version).")
        return {"on": bridge.auto_sync, "text": text}

    def _update_status(self):
        if self.update is None:
            return None
        return {"text": "FLSync %s finns (du har %s). Ladda ner den och kör nya FLSync.exe, så uppdateras det du har. "
                        "Be de andra i sessionen göra samma sak." % (self.update["version"], VERSION)}

    def _version_status(self):
        bridge = self.bridge
        newest = bridge.newest if bridge.in_session() else None
        if newest is None and self.mode == "host" and self.relay is not None:
            newest = self.relay.snapshot
        if not newest:
            return None
        key = (newest.get("v"), newest.get("from"))
        notice = self.notices.get(key, {})
        samples = newest.get("samples") or []
        size = (newest.get("size") or 0) + sum(s.get("size") or 0 for s in samples)
        details = "%d %s, %s" % (len(samples), "sample" if len(samples) == 1 else "samples", megabytes(size))
        plugins = newest.get("plugins") or []
        if plugins:
            details += ", %d %s" % (len(plugins), "plugin" if len(plugins) == 1 else "plugins")
        mine = newest.get("flp") in bridge.shared or (newest.get("from") == bridge.my_id and bridge.my_id)
        merged = mine and newest.get("merged")  # ours, but with the others' changes: FL has yet to open it
        path = bridge.received.get(newest.get("flp"))
        written = bool(path) and os.path.isfile(path)
        ready = written or newest.get("flp") in bridge.downloaded  # downloaded; written when it is opened
        newer = notice.get("newer")
        ours = version_tuple(self.fl_hello.get("flv"))
        theirs = version_tuple(notice.get("flv"))
        if not newer and ours and theirs and theirs[:3] > ours[:3]:  # a later build of the same FL opens it
            newer = notice.get("flv")
        open_now = bridge.base == newest.get("flp") or (self.loaded and self.loaded.get("v") == newest.get("v"))
        if merged:
            state = "Din version sammanslagen med %s ändringar. " % genitive(names_text(newest.get("merged")))
            if open_now:
                state += "Du har den öppen i FL."
            elif bridge.to_launch and bridge.to_launch.get("now"):
                state += "Den öppnas i FL när du stoppar uppspelningen."
            elif bridge.to_launch:
                state += "Den öppnas i FL när du tar en paus (eller nu med Ctrl+S)."
            else:
                state += "Öppna den med knappen nedan, eller spara igen i FL (Ctrl+S)."
        elif mine:
            state = "Det är din version. Den har du redan öppen."
        elif open_now:
            state = "Du har den öppen i FL."
        elif ready and bridge.base is not None:
            state = "Tryck Ctrl+S i FL för att få med den: dina ändringar slås ihop med den."
            if written:
                state += " (Hämtad till %s)" % path
        elif written:
            state = "Hämtad till %s" % path
        elif ready:
            state = "Hämtad. Öppna den med knappen nedan."
        elif bridge.in_session():
            state = "Hämtas ..."
        else:
            state = "Hämtas när din FL är med i sessionen."
        return {"v": newest.get("v"), "name": newest.get("name"), "by": newest.get("by"),
                "time": clock_text(self.seen_at[key]) if key in self.seen_at else None,
                "details": details, "missing": [missing_text(m) for m in notice.get("missing") or []],
                "newer": ("Sparad i en nyare FL (%s) än din (%s). Tryck Ctrl+S i FL, så får du den i din "
                          "FL-version. Öppnar du den direkt varnar FL, och det som bara finns i den nyare FL kan "
                          "saknas." % (newer, self.fl_hello.get("flv") or "en äldre")) if newer else None,
                "state_text": state, "can_open": ready and (not mine or bool(merged)),
                "path": path if written else None}

    def _summary(self, people):
        if self.busy:
            return {"text": self.busy, "state": "wait"}
        if self.mode is None:
            return {"text": "Ingen session", "state": ""}
        in_session = self.bridge.in_session()
        others = [p["name"] for p in people if not p["me"]]
        live = "live med %s" % (names_text(others) if len(others) <= 3 else "%d andra" % len(others))
        if self.mode == "host":
            text = "Du är värd" + (" · " + live if others else ", väntar på de andra")
            return {"text": text, "state": "ok" if in_session else "wait"}
        if in_session:
            return {"text": capital(live) if others else "Med i sessionen, ensam än så länge", "state": "ok"}
        if self.link_problem:
            return {"text": "Värden svarar inte", "state": "bad"}
        return {"text": "Ansluter ...", "state": "wait"}

    def status(self):
        if self.setup is not None:
            view = self.setup.view()
            return {"setup": view, "name": self.name, "log": list(self.lines)[-LOG_SHOWN:], "app_version": VERSION,
                    "summary": {"text": "Avinstallation" if view["kind"] == "uninstall" else "Installation",
                                "state": "ok" if view["state"] == "done" and view["code"] == 0 else "wait"}}
        people = self._people()
        joined_text = None
        if self.mode == "join":
            host, port, _ = self.target
            joined_text = "Ansluten till värdens FLSync på %s:%d (%s)." % (
                host, port, invites.KIND_NAMES.get(invites.kind(host), "?"))
        return {
            "app_version": VERSION,
            "name": self.name,
            "mode": self.mode,
            "busy": self.busy,
            "summary": self._summary(people),
            "error": self.error,
            "open_warning": self.open_warning,
            "invite": None if not self.invite else {
                "code": self.invite, "port": self.relay_port, "note": self.invite_note,
                "addresses": [{"ip": a, "kind": invites.KIND_NAMES.get(invites.kind(a), "?")}
                              for a in self.invite_addresses]},
            "router": self.router,
            "joined_text": joined_text,
            "checks": diagnose.checks(self.facts(), self.midi_port),
            "people": people,
            "people_note": self._people_note(people),
            "pen": self._pen_status(),
            "sync": self._sync_status(),
            "autosync": self._autosync_status(),
            "update": self._update_status(),
            "version": self._version_status(),
            "saved": self.saved if self.mode else None,
            "resuming": {"text": "Försöker gå med i sessionen igen, som när FLSync avslutades. Värden svarar inte "
                                 "än; FLSync försöker igen av sig självt."} if self.resuming and not self.mode else None,
            "notes": list(self.notes_seen),
            "activity": [{"time": a["time"], "text": a["text"]} for a in self.activity],
            "log": list(self.lines)[-LOG_SHOWN:],
            "settings": {"last_invite": self.settings.get("last_invite", ""),
                         "last_mode": self.settings.get("mode"), "relay_port": self.relay_port},
        }


# --- the program ------------------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="FLSync %s: starta eller gå med i en session, i ett eget fönster"
                                                 % VERSION)
    parser.add_argument("--version", action="version", version="FLSync %s" % VERSION)
    shown = parser.add_mutually_exclusive_group()
    shown.add_argument("--browser", action="store_true", help="visa FLSync i webbläsaren i stället för i ett eget fönster")
    shown.add_argument("--no-browser", action="store_true", help="öppna inget fönster (adressen skrivs ut)")
    setup = parser.add_mutually_exclusive_group()
    setup.add_argument("--install", action="store_true",
                       help="installera eller uppdatera FLSync först (FLSync.exe gör det när den startas från "
                            "nedladdningen)")
    setup.add_argument("--repair", action="store_true", help="installera om FLSync (Start-menyn > Reparera FLSync)")
    setup.add_argument("--uninstall", action="store_true",
                       help="ta bort FLSync från datorn (Start-menyn > Avinstallera FLSync)")
    parser.add_argument("--dry-run", action="store_true",
                        help="med --install, --repair eller --uninstall: visa vad som skulle göras, ändra ingenting")
    parser.add_argument("--name", help="namnet de andra ser (sparas; standard: ditt Windows-namn)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--host", action="store_true", help="starta en session direkt")
    action.add_argument("--join", metavar="KOD", help="gå med i sessionen för en inbjudan direkt")
    parser.add_argument("--relay-port", type=int, help="porten för sessionen du är värd för (standard 31415)")
    parser.add_argument("--bind", default="0.0.0.0", help="adressen relayn lyssnar på (standard: alla)")
    parser.add_argument("--ui-port", type=int, default=0, help="sidans port på 127.0.0.1 (standard: en ledig)")
    parser.add_argument("--midi-port", default=MIDI_PORT, help="loopMIDI-porten som FL och FLSync delar")
    parser.add_argument("--no-upnp", action="store_true", help="be inte routern öppna porten")
    parser.add_argument("--no-ip-lookup", action="store_true",
                        help="fråga inte en tjänst på internet om din publika adress när UPnP inte gav någon")
    parser.add_argument("--projects-dir", help="mappen där andras versioner sparas")
    parser.add_argument("--data-dir", help="mapp för inställningar och bryggans filer (standard: "
                                           "%%LOCALAPPDATA%%\\FLSync; ett andra FLSync på samma dator behöver en "
                                           "egen, och en egen --midi-port; pianorullens noter går då bara i det "
                                           "första)")
    parser.add_argument("-v", "--verbose", action="store_true", help="logga varje ändring i sessionen")
    return parser.parse_args(argv)


def without_console(data_dir):
    """FLSync started without a console (pythonw.exe, as FLSync.exe starts it):
    what would be printed goes to flsync.log in the data folder. True then."""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, LOG_FILE)
        if os.path.isfile(path) and os.path.getsize(path) > LOG_FILE_MAX:
            os.replace(path, path + ".old")
        log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log
    return True


def main(argv=None):
    args = parse_args(argv)  # (argparse says nothing without a console: it ignores a missing stderr)
    data_dir = os.path.abspath(args.data_dir) if args.data_dir else bridge_mod.local_data_dir()
    quiet = without_console(data_dir)
    try:
        # Names and paths can have any characters: print them even into a pipe.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass
    system = winsys.System()
    try:
        return run_program(args, data_dir, system)
    except Exception:
        traceback.print_exc()
        if quiet:  # nobody sees a console: say it in a box
            system.message("FLSync stannade av ett fel.\n\n%s\n\nMer står i %s"
                           % (traceback.format_exc().strip().splitlines()[-1], os.path.join(data_dir, LOG_FILE)))
        return 1


def wait_for(lock, seconds):
    """Claim the launcher's lock as soon as the FLSync holding it has ended."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if winsys.claim(lock) is not False:
            return True
        time.sleep(0.3)
    return False


def run_program(args, data_dir, system):
    kind = "uninstall" if args.uninstall else "install" if args.install or args.repair else None
    lock = "Local\\FLSync-launcher-" + hashlib.sha256(os.path.normcase(data_dir).encode("utf-8")).hexdigest()[:16]
    if winsys.claim(lock) is False:
        if kind is not None:
            # Installing or removing: the FLSync that runs (maybe an older one) ends first.
            print("FLSync körs: det avslutas först")
            ask_running(data_dir, "quit", {})
            if not wait_for(lock, STOP_WAIT):
                system.message("FLSync körs och gick inte att avsluta. Stäng FLSync-fönstret och försök igen.")
                return 1
        else:
            url = None
            for _ in range(20):  # the other one may still be starting
                url = find_running(data_dir)
                if url:
                    break
                time.sleep(0.25)
            if not url:
                print("FLSync körs redan men svarar inte. Stäng det andra FLSync-fönstret och försök igen.")
                system.message("FLSync körs redan men svarar inte. Stäng det andra FLSync-fönstret och försök igen.")
                return 1
            print("FLSync körs redan: %s" % url)
            if args.browser:
                system.open_browser(url)
            elif not args.no_browser and not system.focus_window(read_instance(data_dir).get("window")):
                ask_running(data_dir, "window", {}, timeout=10)
            return 0
    setup = None
    if kind is not None:
        setup = installer.SetupRun(kind, ROOT, dry_run=args.dry_run)
        setup.look()
        if kind == "uninstall":
            setup.begin()  # it asks first: Ta bort FLSync ...?
    options = {}
    if args.projects_dir:
        options["projects_dir"] = args.projects_dir
    if args.data_dir:
        # (FL's piano roll scripts look in one folder only: notes are the first FLSync's.)
        options.update(cache_dir=os.path.join(data_dir, "cache"), notes_dir=os.path.join(data_dir, "notes"))
    # Where FL saves: FL tells nobody (its recent-files list reaches the registry
    # only when it closes), so the bridge watches the .flp files being written.
    watcher = flpwatch.FlpWatcher().start()
    options["flp_writes"] = watcher.recent
    app = App(data_dir=data_dir, system=system, name=args.name, relay_port=args.relay_port, bind=args.bind,
              ui_port=args.ui_port, midi_port=args.midi_port, bridge_options=options,
              mapper=False if args.no_upnp else None, ip_lookup=not args.no_ip_lookup,
              update_check=not args.no_ip_lookup, verbose=args.verbose,
              setup=setup, window=not args.browser and not args.no_browser)
    action = None
    if args.host:
        async def action(app):
            await app.host()
    elif args.join:
        async def action(app):
            await app.join(args.join)

    async def run():
        loop = asyncio.get_running_loop()

        def stop(*_):
            loop.call_soon_threadsafe(app.request_quit)

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), stop)
                except (OSError, ValueError):
                    pass
        winsys.on_console_close(stop, lambda: app.closed.wait(8))
        await app.run(open_browser=not args.no_browser, action=action)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
