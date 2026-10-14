"""The sync client: keeps the bindings' keys in step with the session.

Sync model:
  * The relay puts all changes in one global order. The sender of a batch gets
    an `ack` once the relay has handled it.
  * Until a key's newest change is acknowledged, other people's values for it
    are ignored: our change comes later in the relay's order and wins
    everywhere anyway.
  * After writing someone else's value into FL we keep what FL actually holds
    as the baseline, so the write is never mistaken for a local change. For
    SETTLE_TIME afterwards we also leave the key alone, in case FL shows the
    new value a moment late (mute does).
  * The first FL in a session shares all its values as starting values
    (`seed`). The relay never lets them overwrite a change someone made in the
    meantime; those come back in `keep` and we take the session's value.
  * Presence: what others change shows up in FL's hint bar.
  * Structure: keys address channels and plugins by number, so values only
    flow between FLs whose projects have the same structure (fingerprint `fp`,
    flsync_structure.py). It has two parts, the channels' and the mixer's:
    where ours differs from the session's, those keys pause (the rest, and
    song keys like tempo and play, go on) and we say why: save to share our
    structure, or open the version someone else shared (a snapshot, which
    the bridge downloads for us). When FL
    reports channels or mixer tracks added, deleted or moved, the structure
    is checked before anything else is sent: values that moved to other
    numbers must not go out as changes.
  * Offline edits (docs/phase4.md): what changes while we're out of the
    session (bridge or relay gone), and our changes the relay hadn't
    acknowledged when it went, is remembered with the value it had before.
    Back in a session with the same structure, a change the others haven't
    made meanwhile is sent; where they changed it too, the session's value wins.
  * Saving (docs/phase4.md): the bridge hears whether FL has unsaved changes
    and plays (`st`) and when it loaded a project (`opened`). It merges a save
    with the others' versions and may open the result, so from a save until
    the bridge says how it went (`saved`) others' values wait in the inbox:
    FL stays unchanged for that.

Everything runs on FL's UI thread, driven by the bridge's heartbeats (and by
OnIdle when FL calls it), so each tick does a small, bounded amount of work.
"""

import math
import sys
import time

import general
import midi
import ui

try:
    import transport
except ImportError:  # every FL with scripting has it; without it the bridge just doesn't hear if FL plays
    transport = None

import flsync_content
import flsync_fetch
import flsync_view
from flsync_binding import ALL
from flsync_log import log

PROTO = 2
HEARTBEAT_INTERVAL = 1.0        # how often we tell the bridge FL is still here
WELCOME_TIMEOUT = 3.0           # send hello again if no welcome arrives
RETRY_DELAYS = (1.0, 2.0, 5.0)  # seconds before asking the bridge to try the relay again
FAILURE_LOG_INTERVAL = 30.0     # don't repeat "relay unreachable" more often than this
MIN_TICK_INTERVAL = 0.01        # OnIdle and the heartbeats both drive ticks; cap the rate
DETECT_INTERVAL = 0.03          # look for local changes at most this often: each look costs FL calls
PENDING_TIMEOUT = 10.0          # stop waiting for the ack of a change that got lost on the way
SETTLE_TIME = 0.5               # leave a key alone this long after writing someone else's value
BATCH_SIZE = 64                 # changes per message (the protocol allows 256)
HINT_INTERVAL = 0.3             # show at most this many presence hints per second
FP_INTERVAL = 1.0               # how often the structure fingerprint is recomputed
FP_REPORT_INTERVAL = 0.2        # ...and at most this often when FL reports channels or mixer tracks
                                # added, deleted or moved (computing it costs FL calls per channel and slot)
CONTEXT_INTERVAL = 0.25         # how often we check which pattern/channel the piano roll shows
PAUSE_HINT_INTERVAL = 20.0      # while paused, repeat why in the hint bar this often
STATUS_INTERVAL = 0.25          # how often FL's unsaved-changes flag and play state are read for the bridge
VIEW_INTERVAL = 1.0             # how often where the user is in FL is noted (flsync_view)...
RESTORE_DELAY = 0.5             # ...to put back this long after FL opened a version the bridge had it open,
RESTORE_WAIT = 120.0            # if that opening comes within this
HOLD_TIMEOUT = 10.0             # after a save, others' values wait at most this long for the bridge's `saved`
HOLD_OPEN_TIMEOUT = 30.0        # ...and this long once it says it opens the merged version now
LOAD_SETTLE = 3.0               # FL's changed flag settles this long after a project load: no save meanwhile
STALL_TIME = 2.0                # this long between ticks: FL itself was busy (a plugin or project loading)
RESYNC_INTERVAL = 30.0          # after MIDI from the bridge got lost, ask for the session's values at most this often
ERROR_LOG_INTERVAL = 30.0       # an area that keeps failing (a plugin still loading) says so at most this often
# Content (flsync_content.py): what live sync can't carry goes out as versions FL saves by itself.
CONTENT_WINDOW = 1.0            # an undo step this close to a live change is that change, not content
AUTOSAVE_QUIET = 2.0            # FL saves by itself once nothing changed here for this long...
AUTOSAVE_INTERVAL = 5.0         # ...and at most this often
SLOW_SAVE = 0.5                 # a save that held FL up this long: then never while it plays (it would stutter)
PULL_IDLE = 2.0                 # someone else's version is loaded once nothing changed here for this long,
PULL_COUNTDOWN = 3.0            # ...and FL said so this long before
FULL_REFRESH = 8                # an OnRefresh with this many flags is FL refreshing everything, not an edit
STRUCTURE_SAVE = 1.0            # a new channel or plugin is saved this soon, whatever else goes on
REMOTE_ADD_TIME = 20.0          # someone's channels the bridge brings in (`adding`) take at most this long...
REMOTE_SETTLE = 2.0             # ...and their last undo steps come this soon after `added`
DIRTY_QUIET = 4.0               # FL with unsaved changes also saves once stopped and nothing changed for this
DIRTY_INTERVAL = 20.0           # long, at most this often: content we didn't see goes out too (the bridge
                                # shares such a save only if it has something live sync didn't carry)
# The part of the structure fingerprint (flsync_structure.py) each key prefix
# depends on: "c" the channels, "m" the mixer. Others (song, transport,
# playlist tracks, patterns) mean the same in every structure.
KEY_AREAS = {"c": "c", "g": "c", "gp": "c", "pc": "c", "m": "m", "mx": "m", "mr": "m", "pm": "m"}
AREAS = (("c", "kanalerna"), ("m", "mixern"))
_MISSING = object()
# OnDirtyChannel flags of channels added, deleted or replaced, and of a selection (FL's midi.py values).
_RESTRUCTURE = tuple(getattr(midi, name, value) for name, value in
                     (("CE_New", 0), ("CE_Delete", 1), ("CE_Replace", 2)))
_SELECT = getattr(midi, "CE_Select", 4)

# `saved` from the bridge: what happens with the version the save gave (its `open`).
OPEN_HINTS = {"now": ", öppnar den gemensamma versionen",
              "later": ", öppnas när du tar en paus (Ctrl+S nu)",
              "stop": ", öppnas när uppspelningen stoppas",
              "no": ", spara igen (Ctrl+S) för att öppna den"}
OPEN_LOGS = {"now": "FL öppnar den gemensamma versionen nu",
             "later": "den gemensamma versionen öppnas när du tar en paus: uppspelningen stoppad och ingen tangent "
                      "eller mus en stund (eller nu med Ctrl+S)",
             "stop": "den gemensamma versionen öppnas när uppspelningen stoppas",
             "no": "du har ändrat mer sedan du sparade, så den gemensamma versionen öppnas inte än: "
                   "spara igen (Ctrl+S) för att få den"}


def api_version():
    try:
        return general.getVersion()
    except Exception:
        return None


def fl_version():
    """FL's own version, e.g. '25.2.4.4960' (ui.getVersion modes 0-3), or None."""
    try:
        parts = [ui.getVersion(mode) for mode in range(4)]
    except Exception:
        return None
    if all(isinstance(part, int) and not isinstance(part, bool) for part in parts):
        return "%d.%d.%d.%d" % tuple(parts)
    return None


def _version_tuple(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return None


def key_area(key):
    return KEY_AREAS.get(key.split("/", 1)[0]) if isinstance(key, str) else None


def parts_differ(a, b, area):
    """Whether structures `a` and `b` differ where keys of `area` point (the
    relay checks the same). Unknown never differs; a fingerprint that isn't
    the two 6-digit parts differs as a whole."""
    if a is None or b is None or a == b or area is None:
        return False
    if len(a) != 12 or len(b) != 12:
        return True
    return a[:6] != b[:6] if area == "c" else a[6:] != b[6:]


def capabilities():
    """What this FL offers scripts, for the bridge's log: FL and API version,
    and whether newer functions exist (swing, globalTransport, Gopher's
    `ilmcp` module). Only looked at, never called."""
    parts = ["FL %s, API %s" % (fl_version() or "?", api_version())]
    try:
        import ilmcp
        names = sorted(name for name in dir(ilmcp) if not name.startswith("_"))
        parts.append("ilmcp finns: %s" % (", ".join(names)[:400] or "tom"))
    except Exception as e:
        parts.append("ilmcp finns inte (%s)" % e.__class__.__name__)
    try:
        import channels
        found = [name for name in ("getSwing", "getShuffle") if hasattr(channels, name)]
        parts.append("swing: %s" % (", ".join(found) or "nej"))
    except Exception:
        pass
    parts.append("globalTransport: %s" % ("ja" if hasattr(transport, "globalTransport") else "nej"))
    return "funktioner i FL: " + "; ".join(parts)


def _safe_to_edit():
    check = getattr(general, "safeToEdit", None)  # API 29+
    return True if check is None else bool(check())


def changed_flag():
    """general.getChangedFlag(): 0 saved, 1 changed since the save, 2 changed
    but autosaved since. None if this FL can't say."""
    get = getattr(general, "getChangedFlag", None)
    if get is None:
        return None
    try:
        flag = get()
    except Exception:  # e.g. "Operation unsafe at current time": try again next time
        return None
    if isinstance(flag, bool):
        return int(flag)
    return flag if isinstance(flag, int) else None


def is_playing():
    """transport.isPlaying() as a bool, or None if this FL can't say."""
    get = getattr(transport, "isPlaying", None)
    if get is None:
        return None
    try:
        return bool(get())
    except Exception:
        return None


class SyncClient:
    def __init__(self, name, bindings, link, hint=None, debug=False, fingerprint=None, context=None):
        self.name = name
        self.bindings = list(bindings)
        self.by_prefix = {b.prefix: b for b in self.bindings}
        self.link = link
        self.hint = hint or (lambda text: None)
        self.debug = debug
        self.fingerprint = fingerprint or (lambda: None)
        self.context = context or (lambda: None)  # (pattern, channel, name) the piano roll shows
        self.sent_context = None
        self.next_context = 0.0
        self.my_fp = None        # our structure as we tell the session (None: unknown, never pauses)
        self.local_fp = None     # the fingerprint FL's API gives us here
        self.alias = None        # (local fp, version's fp): we opened that version (`loaded`), see _effective_fp
        self.load_fp = None      # the local fp right after the last project load (or script start)
        self.pen = None          # who changes the session's structure now: {id, name} or None
        self.flv = fl_version()
        self.session_fp = None   # the session's, from welcome / snapshot / reshare
        self.snapshot = None     # the newest snapshot notice: {v, name, fp, by, from, path?, missing?}
        self.pause_reason = None
        self.next_fp = 0.0
        self.next_fp_report = 0.0
        self.fp_due = False      # FL reported what may be a change of structure: check before sending
        self.next_pause_hint = 0.0
        self.units = {b: [] for b in self.bindings}
        self.dirty = {b: set() for b in self.bindings}
        self.all_dirty = set()
        self.sweep = {b: 0 for b in self.bindings}
        self.shadow = {}         # key -> value FL had the last time we looked
        self.pending = {}        # key -> number of our newest batch with it, not yet acknowledged
        self.pending_since = {}  # key -> when that batch was sent
        self.batches = {}        # batch number -> its (key, value) pairs
        self.unacked = {}        # key -> [value before our oldest change of it the relay hasn't acknowledged,
                                 #         number of our newest batch with it]
        self.inbox = {}          # key -> (value, sender id), waiting to be written into FL
        self.settling = {}       # key -> time before which local reads of it are ignored
        self.seq = 0
        self.my_id = None
        self.joined = False
        self.peers = {}
        self.need_seed = False
        self.seed_initial = True
        self.loading = False
        self.bridge_seen = False
        self.warned_no_output = False
        self.last_tick = float("-inf")
        self.next_detect = 0.0
        self.busy_at = float("-inf")  # the last tick that ended with our messages still queued
        self.next_hello = 0.0
        self.next_heartbeat = 0.0
        self.retries = 0
        self.last_failure_log = None
        self.next_hint = 0.0
        self.waiting_hint = None
        # Phase 4: FL's state for the bridge, holding others' values during a save, offline edits.
        self.status = None       # (changed flag, playing) as last read; None: not read since the start or a load
        self.sent_status = None  # what the bridge last heard (None: tell it again)
        self.next_status = 0.0
        self.last_flag = None    # the changed flag at the last look: going from set to 0 is a save
        self.loaded = False      # a project load happened since the last tick (saves_from is set then)
        self.saves_from = 0.0    # a 0 before this is FL settling after a load, not a save
        self.undo_from = 0.0     # undo steps before this are FL refreshing after a start or load (on_refresh)
        self.hold_until = None   # while set, others' values wait in the inbox (a save is being merged)
        self.held_back = False   # the inbox has values that waited: old news, written without presence hints
        self.offline = False     # we left a session in sync: remember what changes until we're back
        self.offline_fp = None   # our structure when we left
        self.offline_base = {}   # key -> its value before the first change made since we left
        self.gaps = 0            # the link's count of lost events at the last look
        self.next_resync = 0.0
        self.failing = {}        # binding -> when its next failure may be reported
        self.told_capabilities = False
        # Content: edits live sync can't carry, saved by FL itself (flsync_content.py).
        self.content = 0         # such edits since the last save or load
        self.undo_steps = []     # when undo steps came that may be content: decided CONTENT_WINDOW later
        self.live_times = []     # when live values changed here lately, ours or someone else's
        self.local_at = float("-inf")  # the last edit here of any kind
        self.marks = None        # flsync_content.marks() at the last look
        self.can_save = flsync_content.can_save(_version_tuple(self.flv))
        self.autosave_ok = False  # the bridge's say: FL's project is a file of the session's
        self.next_autosave = 0.0
        self.pull = None         # {by, v}: the bridge wants someone's newer version in FL
        self.pull_at = None      # when the countdown to that ends
        self.from_idle = True    # this tick came from OnIdle (FL may be asked to save only then)
        self.next_check = 0.0    # FL with unsaved changes saves to be compared (DIRTY_INTERVAL)
        self.structure_at = None  # when channels or plugins changed here (not by a load): saved soon
        self.expect_fp = None    # the structure FL gets from someone's channels the bridge brings in (`adding`)
        self.remote_until = float("-inf")  # until then, structure and undo steps are those channels coming in
        self.saved_by_itself = float("-inf")  # when FL last saved by itself
        self.slow_save = False   # a save by itself held FL up SLOW_SAVE: none while it plays from then on
        self.load_started = None  # when FL began loading a project (time.monotonic): how long it takes
        self.view, self.next_view = {}, 0.0  # where the user is in FL (flsync_view), noted every VIEW_INTERVAL
        self.restore_view = None  # (view, by when, loads then): put back once FL opened what the bridge opens
        self.loads = 0           # projects FL has loaded since the script started
        self.loaded_at = None    # when the last load was seen done
        self.fetch = None        # someone's notes coming into the piano rolls (flsync_fetch, the bridge's `notes_in`)

    # --- driven by FL's callbacks -----------------------------------------

    def start(self):
        self._refresh_units()
        self._snapshot()
        self.local_fp = self.load_fp = self.fingerprint()
        self.my_fp = self._effective_fp()
        self.fp_due = False

    def on_midi(self, status, data1, data2, now):
        if self.link.receive(status, data1, data2, now):
            self.idle(now, from_idle=False)

    def idle(self, now, from_idle=True):
        if now - self.last_tick >= MIN_TICK_INTERVAL:
            self.from_idle = from_idle
            self.tick(now)
            self.from_idle = True

    def tick(self, now):
        since, self.last_tick = now - self.last_tick, now
        if since == float("inf"):
            self.undo_from = now + LOAD_SETTLE  # the script just started: FL refreshes everything soon
        if STALL_TIME <= since < float("inf"):
            # FL itself was busy (a plugin or project loading, a dialog): the
            # bridge only seemed silent because we didn't listen, and it keeps
            # us in the session meanwhile. It says so if it left after all.
            self.link.forgive(now)
            self._tell("FL var upptaget i %.1f s" % since)
        if self.loaded:
            self.loaded, self.saves_from = False, now + LOAD_SETTLE
            self.undo_from = self.saves_from
            self.loaded_at = now
        self._refresh_units()
        _each(self.link.poll(), self._handle, now)
        gaps = self.link.stats["gaps"]
        if gaps != self.gaps:
            self.gaps = gaps
            if self.joined and now >= self.next_resync:
                # Events from the bridge got lost: a change of someone else's
                # may be missing here. The session's values bring it back.
                self.next_resync = now + RESYNC_INTERVAL
                self.link.send({"t": "sync"})
        if self.hold_until is not None and now >= self.hold_until:
            self._end_hold()
            if self.debug:
                log("bryggan har inte sagt hur sparningen gick, så de andras värden skrivs in igen")
        if not self.loading and now >= self.next_status:
            self.next_status = now + STATUS_INTERVAL
            self._read_status(now)
            self._look_for_content(now)
        if not self.loading and self.from_idle:
            self._keep_view(now)
            if self.fetch is not None:
                may_edit = (not is_playing() and not flsync_content.recording() and _safe_to_edit()
                            and not flsync_content.in_menu())
                if self.fetch.tick(now, self.link.send, may_edit):
                    self._fetch_over()
        if self.link.alive(now):
            self._bridge_present(now)
        elif self.bridge_seen:
            self.bridge_seen = False
            log("tappade kontakten med bryggan (bridge.py), väntar på att den kommer tillbaka")
            self._reset()
            self.next_hello = 0.0
        if not self.loading:
            if self.fp_due and now >= self.next_fp_report:
                self.next_fp_report = now + FP_REPORT_INTERVAL
                self._check_fp(now)
            elif now >= self.next_fp:
                self._check_fp(now)
            # Local changes first: anything the user just did becomes pending
            # before remote values for the same keys are considered. Not while
            # the link is still sending earlier ones (a preset change, the
            # project being shared): new changes would only queue up behind
            # them, and what changes meanwhile goes out later as its newest value.
            # Nor while a structure change FL reported waits to be checked: the
            # values of channels or inserts that got other numbers mustn't go out.
            if now >= self.next_detect and not self.fp_due and not self.link.busy():
                self.next_detect = now + DETECT_INTERVAL
                self._detect_local_changes(now)
            if self.joined:
                if self.need_seed:
                    self._seed(now)
                self._expire_pending(now)
                self._apply_inbox(now)
                if self.pause_reason and now >= self.next_pause_hint:
                    self.next_pause_hint = now + PAUSE_HINT_INTERVAL
                    self.waiting_hint = self.pause_reason
                self._save_by_itself(now)
        self._show_hint(now)
        self.link.flush()
        if self.link.busy():
            self.busy_at = now

    def on_dirty_mixer_track(self, index):
        if index < 0:
            self.fp_due = True  # all tracks changed: inserts may have been added, deleted or moved
        for b in self.bindings:
            self._mark(b, b.on_dirty_mixer_track(index))

    def on_dirty_channel(self, index, flag):
        if flag in _RESTRUCTURE or (index < 0 and flag != _SELECT):
            self.fp_due = True  # channels added, deleted or replaced, or all of them changed
        for b in self.bindings:
            self._mark(b, b.on_dirty_channel(index, flag))

    def on_refresh(self, flags):
        # An edit (or undo): content unless a live value changed. Not the
        # refresh of everything FL does as the script starts or a project
        # loads (measured in FL 26.1.7: 11 flags, a moment after the first
        # tick; an edit's refresh has 3): nothing was edited then.
        everything = bin(flags).count("1") >= FULL_REFRESH
        if (flags & flsync_content.UNDO and not everything and not (self.loading or self.loaded)
                and self.last_tick >= self.undo_from):
            self.undo_steps.append(self.last_tick)
        for b in self.bindings:
            self._mark(b, b.on_refresh(flags))

    def _fetch_over(self):
        fetch, self.fetch = self.fetch, None
        self.link.send({"t": "notes_end", "nonce": fetch.nonce, "ok": fetch.ok, "why": fetch.why})
        if fetch.ok:
            self.waiting_hint = "FLSync: %s noter kom in" % _genitive(_who(fetch.by))
            log("%s noter kom in i pianorullen, utan att projektet laddades om" % _genitive(_who(fetch.by)))

    def _keep_view(self, now):
        """Note where the user is in FL, and put it back after the bridge had FL
        open a version (`reopening`): FL brings that version's view instead."""
        pending = self.restore_view
        if pending is not None:
            view, until, loads = pending
            if now > until:
                self.restore_view = None  # FL never opened it
            elif (self.loads > loads and self.loaded_at is not None and now - self.loaded_at >= RESTORE_DELAY
                  and _safe_to_edit() and not flsync_content.in_menu()):
                self.restore_view = None
                done = flsync_view.restore(view)
                if done and self.debug:
                    log("tillbaka där du var: %s" % ", ".join(done))
                self.next_view = now + VIEW_INTERVAL
            return  # (meanwhile what FL shows is the version's, not the user's)
        if now >= self.next_view:
            self.next_view = now + VIEW_INTERVAL
            self.view = flsync_view.capture()

    def project_load(self, status):
        if status == midi.PL_Start:
            self.loading = True
            self.load_started = time.monotonic()
            if self.fetch is not None:
                self.fetch.view = None  # (the project's view goes with it)
                self.fetch.end(False, "ett annat projekt")
                self._fetch_over()  # someone's notes are for the project that was open, not the next one
            return
        self.loading = False
        self.loads += 1
        took = None if self.load_started is None else time.monotonic() - self.load_started
        self.load_started = None
        self.inbox.clear()
        self.hold_until, self.held_back = None, False  # the version the bridge opened (or another project): see below
        self._forget_content()                # the project as loaded has it all
        self.status = self.last_flag = None   # a load leaves FL unchanged: that's no save
        self.loaded = True                    # ... nor is the flag settling right after it (measured in FL 2025)
        self.offline_base.clear()             # edits of the project that was open
        self.unacked.clear()
        for b in self.bindings:
            b.on_project_load()
        self._refresh_units()
        self._snapshot()
        self.local_fp = self.load_fp = self.fingerprint()
        old_fp, self.my_fp = self.my_fp, self._effective_fp()
        self.fp_due = False  # what FL reported while loading: the new project's structure is known now
        if status != getattr(midi, "PL_LoadError", None):
            msg = {"t": "opened", "fp": self.local_fp}  # for the bridge: which version FL has now...
            if took is not None:
                msg["took"] = round(took, 1)  # ...and how long opening it took (FLSync waits longer when slow)
            self.link.send(msg)
        if not self.joined:
            return
        if self.my_fp != old_fp:
            # Another project, not an edit of this one: it mustn't take the pen.
            self.link.send({"t": "fp", "fp": self.my_fp, "load": True})
        if self._other_fls():
            self.link.send({"t": "sync"})  # others are here: take the session's values (where they fit)
            self._update_pause("other")
        elif self.my_fp == old_fp or self.my_fp == self.session_fp:
            self.need_seed = True              # alone: this project becomes the session
            self.seed_initial = False          # ...and overwrites what the session had
        # Alone with another structure: the relay adopts it from our `fp` and
        # answers with `reshare`, which shares everything then.

    # --- the bridge and the relay -------------------------------------------

    def _bridge_present(self, now):
        if not self.bridge_seen:
            self.bridge_seen = True
            self.sent_context = self.sent_status = None  # a new bridge needs to hear them again
            log("bryggan hittad")
        if not self.link.can_send():
            if not self.warned_no_output:
                self.warned_no_output = True
                log("bryggan hörs, men FLSync har ingen MIDI-utgång. Välj porten 'FLSync' under "
                    "Output i MIDI-inställningarna och ge den samma Port-nummer som ingången.")
            return
        self.warned_no_output = False
        # Heartbeat first: it carries our epoch, so a bridge that knew an earlier
        # run of this script starts over before our hello arrives.
        if now >= self.next_heartbeat:
            self.link.heartbeat()
            self.next_heartbeat = now + HEARTBEAT_INTERVAL
        if not self.told_capabilities:
            self.told_capabilities = True
            self.link.send({"t": "log", "data": capabilities()})  # for the launcher's log only
        if now >= self.next_context:
            self.next_context = now + CONTEXT_INTERVAL
            context = self.context()
            if context is not None and context != self.sent_context:
                self.sent_context = context
                self.link.send({"t": "pr", "p": context[0], "c": context[1], "n": context[2]})
        if self.link.receiving():
            self.next_hello = max(self.next_hello, now + WELCOME_TIMEOUT)  # maybe the welcome: let it come
        if not self.joined and now >= self.next_hello:
            hello = {"t": "hello", "proto": PROTO, "app": "fl", "name": self.name,
                     "api": api_version(), "fp": self.my_fp}
            if self.flv:
                hello["flv"] = self.flv
            self.link.send(hello)
            self.next_hello = now + WELCOME_TIMEOUT
            self.sent_status = None  # a (re)starting session: the bridge hears FL's state again
        if self.status is not None and self.status != self.sent_status:
            self._send_status()

    def _send_status(self):
        """`st` for the bridge (never forwarded): FL's unsaved-changes flag and
        whether it plays, the parts this FL can tell."""
        flag, playing = self.sent_status = self.status
        msg = {"t": "st"}
        if flag is not None:
            msg["dirty"] = flag
        if playing is not None:
            msg["playing"] = playing
        if len(msg) > 1:
            self.link.send(msg)

    def _read_status(self, now):
        flag = changed_flag()
        self._watch_saves(flag, now)
        self.status = (flag, is_playing())

    def _watch_saves(self, flag, now):
        """FL's changed flag going from set to 0 is a save (project_load forgets
        the flag first, a load isn't one). Others' values then wait until the
        bridge says how the save went, so FL stays unchanged in case it opens the
        merged version. Our own changes still go out meanwhile."""
        if flag == 0 and self.last_flag and self.joined and not self.loading and now >= self.saves_from:
            self.hold_until = max(self.hold_until or now, now + HOLD_TIMEOUT)
            self.content, self.undo_steps = 0, []  # the save has it: the bridge shares it
            if self.debug:
                log("du sparade: de andras värden väntar tills bryggan har delat sparningen")
        self.last_flag = flag

    def _end_hold(self):
        """Others' values go into FL again. Those that waited are old news: no
        presence hints for them (the hint bar says how the save went)."""
        if self.hold_until is not None:
            self.hold_until = None
            self.held_back = bool(self.inbox)

    def _relay_lost(self, reason, now):
        if self.joined:
            log("tappade anslutningen till relayn (%s), försöker igen" % reason)
        elif self.last_failure_log is None or now - self.last_failure_log >= FAILURE_LOG_INTERVAL:
            log("%s, försöker igen" % reason)
            self.last_failure_log = now
        self._reset()
        self.next_hello = now + RETRY_DELAYS[min(self.retries, len(RETRY_DELAYS) - 1)]
        self.retries += 1

    def _reset(self):
        if self.joined:
            # Out of the session. If we were in step with it, what changes from
            # now on is remembered (and our changes the relay may not have got)
            # and goes out when we're back (_rejoined).
            self.offline, self.offline_fp = self.synced, self.my_fp
            self.offline_base = {key: entry[0] for key, entry in self.unacked.items()} if self.synced else {}
        self.unacked.clear()
        self.hold_until, self.held_back = None, False
        self.joined = self.need_seed = False
        self.my_id = None
        self.session_fp = self.snapshot = self.pause_reason = self.pen = None
        self.peers.clear()
        self.pending.clear()
        self.pending_since.clear()
        self.batches.clear()
        self.inbox.clear()

    # --- messages from the relay ------------------------------------------

    def _handle(self, msg, now):
        kind = msg.get("t")
        if kind == "sets":
            self._on_sets(msg)
        elif kind == "ack":
            # `stale`: refused, made for another structure. Either way it's done.
            # A list in `stale`: only those keys were refused (the others don't
            # depend on the part of the structure that differs).
            seq, stale = msg.get("l"), msg.get("stale")
            batch = self.batches.pop(seq, ()) if isinstance(seq, int) and not isinstance(seq, bool) else ()
            for key, value in batch:
                self._acknowledged(key, seq)
                self._landed(key, seq, value, not (stale is True or isinstance(stale, list) and key in stale))
            if stale and self.debug:
                log("relayn tog inte emot sats %s: gjord för en annan struktur" % msg.get("l"))
        elif kind == "keep":
            # Starting values of ours that arrived after someone had changed those
            # keys: the relay kept their values, so take them.
            for pair in msg.get("kv") or ():
                if self._valid_pair(pair) and self._acknowledged(pair[0], msg.get("l")):
                    self.inbox[pair[0]] = (pair[1], None)
        elif kind == "reject":
            self._acknowledged(msg.get("k"), msg.get("l"))
            log("relayn avvisade %s: %s" % (msg.get("k"), msg.get("msg")))
        elif kind == "welcome":
            self._on_welcome(msg, now)
        elif kind == "state":
            self._queue_state(msg.get("state"))
        elif kind == "seed":
            self.need_seed = self.seed_initial = True
        elif kind == "snapshot":
            self._on_snapshot(msg)
        elif kind == "saved":
            self._on_saved(msg, now)
        elif kind == "notes":
            self._on_notes(msg)
        elif kind == "loaded":
            self._on_loaded(msg)
        elif kind == "pen":
            self._on_pen(msg)
        elif kind == "reshare":
            # Our structure is the session's now (our snapshot, or we're alone
            # and the relay took our `fp`) and its values for the part that
            # changed are gone: send ours. As starting values: where the relay
            # kept a value (the part that stayed) and someone changed it
            # meanwhile, theirs wins and comes back in `keep`.
            self.session_fp = self.my_fp
            self.need_seed, self.seed_initial = True, True
            self._update_pause()
        elif kind == "link":
            self._relay_lost(msg.get("msg") or "relayn svarar inte", now)
        elif kind == "peer_join":
            self.peers[msg.get("id")] = msg
            log("%s anslöt" % msg.get("name"))
            self.waiting_hint = "FLSync: %s anslöt" % msg.get("name")
        elif kind == "peer_leave":
            self.peers.pop(msg.get("id"), None)
            log("%s lämnade sessionen" % msg.get("name"))
            self.waiting_hint = "FLSync: %s lämnade" % msg.get("name")
        elif kind == "autosave":
            # The bridge says whether FL may save by itself: its project is a
            # file of the session's (`broken`: FL didn't save when asked to,
            # `retry`: the bridge asks again in a while, so not given up yet).
            self.autosave_ok = msg.get("ok") is True
            if msg.get("broken") and msg.get("retry") is True:
                self.waiting_hint = "FLSync: FL sparade inte, försöker igen om en minut"
            elif msg.get("broken") and self.can_save:
                self.can_save = False
                self._tell("FL sparade inte när FLSync bad om det, så FLSync sparar inte av sig själv mer nu. "
                           "Spara med Ctrl+S för att dela det du gör.")
                self.waiting_hint = "FLSync: spara med Ctrl+S för att dela det du gör"
        elif kind == "adding":
            # The bridge has FL open someone's new channels as presets: that
            # change of structure, and its undo steps, aren't ours to share.
            fp = msg.get("fp")
            self.expect_fp = fp if isinstance(fp, str) else None
            self.remote_until = now + REMOTE_ADD_TIME
        elif kind == "added":
            # Channels someone added came in live (the bridge had FL open them as presets).
            self.remote_until = min(self.remote_until, now + REMOTE_SETTLE)
            what = ", ".join(_names(msg.get("what"), 5)) or "kanaler"
            log("%s lade till %s. Det finns nu här, med samma inställningar." % (_who(msg.get("by")), what))
            self.waiting_hint = "FLSync: %s lade till %s" % (_who(msg.get("by")), what)
        elif kind == "pull":
            # Someone shared a version FL hasn't got: it is saved and merged in
            # once FL is stopped and nothing changes here (_save_by_itself).
            self.pull = msg if not msg.get("cancel") else None
            self.pull_at = None
        elif kind == "notes_in":
            # Someone's version changed only notes: they come into the piano rolls (flsync_fetch).
            if msg.get("cancel"):
                if self.fetch is not None:
                    self.fetch.end(False, "en nyare version")
                    self.fetch = None
            elif self.fetch is None:
                fetch = flsync_fetch.Fetch(msg, now)
                if fetch.valid:
                    self.fetch = fetch
                    self.waiting_hint = "FLSync: hämtar %s noter" % _genitive(_who(fetch.by))
                else:
                    self.link.send({"t": "notes_end", "nonce": msg.get("nonce"), "ok": False, "why": "ogiltigt"})
        elif kind in ("notes_go", "notes_done"):
            if self.fetch is not None and self.fetch.on_message(kind, msg, now):
                self._fetch_over()
        elif kind == "reopening":
            # The bridge has FL open a version right now: where the user is goes back after (_keep_view).
            self.restore_view = (dict(self.view), now + RESTORE_WAIT, self.loads)
        elif kind == "opening":
            # FL has nothing unsaved: the bridge opens someone's version at this pause.
            if msg.get("cancel"):
                self.waiting_hint = "FLSync: öppnar de andras version vid nästa paus"
            else:
                self.waiting_hint = "FLSync: öppnar %s version om %d s%s" % (
                    _genitive(_who(msg.get("by"))), _number(msg.get("in"), 3), _takes(msg.get("took")))
        elif kind == "note":
            if msg.get("kind") == "unrelated":
                log("%s sparade '%s', som inte är sessionens projekt. Det delas om hen sparar igen inom kort."
                    % (_who(msg.get("by")), _shown(msg.get("name"))))
                self.waiting_hint = "FLSync: %s sparade ett annat projekt (%s)" % (_who(msg.get("by")),
                                                                                _shown(msg.get("name")))
        elif kind == "error":
            log("relayn: %s" % msg.get("msg"))

    @staticmethod
    def _valid_pair(pair):
        return isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str)

    def _on_sets(self, msg):
        sender = msg.get("from")
        for pair in msg.get("kv") or ():
            if not self._valid_pair(pair):
                continue
            key, value = pair
            if not self._key_ok(key):
                continue  # made for the session's structure, which differs from ours there
            if self.debug:
                log("<- %s = %r" % (key, value))
            if key not in self.pending:
                self.inbox.pop(key, None)  # keep the newest value last
                self.inbox[key] = (value, sender)

    def _on_welcome(self, msg, now):
        if msg.get("proto") != PROTO:
            log("relayn kör protokoll %s men scriptet %s. Uppdatera så att de matchar och ladda om scriptet."
                % (msg.get("proto"), PROTO))
            self.next_hello = float("inf")
            return
        self.my_id = msg.get("id")
        self.joined = True
        self.retries = 0
        self.last_failure_log = None
        peers = msg.get("peers") or []
        self.peers = {p["id"]: p for p in peers if isinstance(p, dict) and "id" in p}
        others = ", ".join(str(p.get("name")) for p in self.peers.values()) or "bara du"
        log("ansluten som %s. I sessionen: %s" % (msg.get("name"), others))
        self.snapshot = msg.get("snapshot") if isinstance(msg.get("snapshot"), dict) else None
        self.session_fp = msg.get("fp")
        self.pen = msg.get("pen") if isinstance(msg.get("pen"), dict) else None
        if self.pen:
            log("%s ändrar projektets struktur just nu" % self.pen.get("name"))
        if msg.get("seed"):
            self.session_fp = self.session_fp or self.my_fp  # our project becomes the session's
            self.need_seed = self.seed_initial = True
        self._rejoined(msg, now)  # before the session's values are taken: what it sends isn't overwritten
        if not msg.get("seed"):
            self._queue_state(msg.get("state"))
        self._update_pause("other")

    def _rejoined(self, msg, now):
        """Back in a session: send the changes made while we were out of it
        that the others haven't made meanwhile (the session still has the value
        from before them); where they changed it too, the session's value wins.
        The first FL in a session shares everything anyway. Then forget them."""
        edits, self.offline_base = self.offline_base, {}
        tracking, self.offline = self.offline, False
        if not tracking or msg.get("seed") or self.loading:
            return  # seeding: our values include them; a project being opened forgets them anyway
        changed = {}
        for key, before in edits.items():
            value = self.shadow.get(key, _MISSING)
            if value is not _MISSING and value != before:  # not changed and back
                changed[key] = before
        if not changed:
            return
        if not self.synced or self.session_fp != self.offline_fp:
            log("%s du gjorde offline skickas inte: sessionen har ett annat projekt nu"
                % _amount(len(changed), "ändring", "ändringar"))
            return
        state = msg.get("state") if isinstance(msg.get("state"), dict) else {}
        send, conflicts = [], 0
        for key, before in changed.items():
            binding = self.binding_for(key)
            if binding is None or binding.transient(key):
                continue
            mine, theirs = self.shadow[key], state.get(key, _MISSING)
            if theirs is not _MISSING and theirs == mine:
                continue                  # the session has it already
            if theirs is _MISSING or theirs == before:
                send.append((key, mine))  # nobody else changed it: ours wins
            else:
                conflicts += 1            # they changed it too: the session's value wins
        if send:
            self._send(send, now, bases=changed)
        if not send and not conflicts:
            return
        if send:
            text = "%s du gjorde offline skickades" % _amount(len(send), "ändring", "ändringar")
            if conflicts:
                text += ", %d krockade med de andras och ersattes" % conflicts
        else:
            text = "%s du gjorde offline krockade med de andras och ersattes" % _amount(
                conflicts, "ändring", "ändringar")
        log("ansluten igen: %s" % text + (" (där gäller deras värden)" if conflicts else ""))
        self.waiting_hint = "FLSync: ansluten igen, %s" % text

    def _queue_state(self, state):
        """A snapshot of the session: take everything but event keys like play/stop."""
        if not isinstance(state, dict):
            return
        for key, value in state.items():
            binding = self.binding_for(key)
            if binding is None or binding.transient(key) or key in self.pending or not self._key_ok(key):
                continue  # (values for another structure would land on the wrong things)
            self.inbox[key] = (value, None)

    # --- pending changes ------------------------------------------------------

    def _acknowledged(self, key, seq):
        """The relay has handled our batch `seq` for `key`. True if it was our newest."""
        if key in self.pending and self.pending[key] == seq:
            del self.pending[key]
            self.pending_since.pop(key, None)
            return True
        return False

    def _landed(self, key, seq, value, accepted):
        """The relay has handled our batch `seq` with `key` = `value` (`accepted`:
        not refused). Done with it if it was our newest; otherwise the session
        has this value and the newer one is still on its way."""
        entry = self.unacked.get(key)
        if entry is None:
            return
        if entry[1] == seq:
            del self.unacked[key]
        elif accepted:
            entry[0] = value

    def _expire_pending(self, now):
        # A change's clock starts once it has left FL: waiting behind a long
        # queue of our own messages isn't a lost ack.
        for key, since in list(self.pending_since.items()):
            if now - max(since, self.busy_at) > PENDING_TIMEOUT:
                self.pending.pop(key, None)
                del self.pending_since[key]
        if len(self.batches) > 1000:  # acks that never came
            for seq in sorted(self.batches)[:len(self.batches) - 1000]:
                del self.batches[seq]

    # --- reading FL -----------------------------------------------------------

    def binding_for(self, key):
        return self.by_prefix.get(key.split("/", 1)[0]) if isinstance(key, str) else None

    def _refresh_units(self):
        for b in self.bindings:
            try:
                self.units[b] = list(b.units())
            except Exception:  # keeps the units it had
                self._failed(b, self.last_tick)

    def _failed(self, binding, now):
        """An area of FL refused to be read or written (a plugin that is still
        loading does that): the other areas go on, and this one is tried again
        next time. Said at most once per ERROR_LOG_INTERVAL per area."""
        if now >= self.failing.get(binding, float("-inf")):
            self.failing[binding] = now + ERROR_LOG_INTERVAL
            self._tell("fel i %s/ (försöker igen): %s" % (binding.prefix, _error_text()))

    def _tell(self, text):
        """Log in FL's Script output and, through the bridge, in FLSync's log."""
        log(text)
        if self.bridge_seen and self.link.can_send():
            self.link.send({"t": "log", "data": text})

    def _mark(self, binding, units):
        if units == ALL:
            self.all_dirty.add(binding)
        elif units:
            self.dirty[binding].update(units)

    def _snapshot(self):
        """Take FL's current values as the baseline without sending anything."""
        self.shadow = {}
        for b in self.bindings:
            try:
                for unit in self.units[b]:
                    self.shadow.update(_readable(b.read(unit)))
            except Exception:
                self._failed(b, self.last_tick)
            self.dirty[b].clear()
        self.all_dirty.clear()

    def _detect_local_changes(self, now):
        changes = []
        bases = {}  # key -> its value before the change, for the changes sent now
        # Keys only go out where our structure is the session's (_key_ok).
        send = self.joined and not self.need_seed
        # Out of the session with the structure we left it with: remember what
        # each changed key was before, to send the changes when we're back.
        record = self.offline and not self.joined and self.my_fp == self.offline_fp
        for b in self.bindings:
            try:
                self._detect_in(b, now, send, record, changes, bases)
            except Exception:  # what it found before failing still goes out
                self._failed(b, now)
        if changes:
            self._send(changes, now, bases=bases)

    def _detect_in(self, b, now, send, record, changes, bases):
        """Look for local changes in one binding's units."""
        units = self.units[b]
        existing = set(units)
        reread, rebaseline = b.tick(now)
        for unit in rebaseline:
            if unit in existing:
                self.shadow.update(_readable(b.read(unit)))
        if b in self.all_dirty or reread == ALL:
            todo = units
        else:
            todo = set(self.dirty[b])
            todo.update(reread)
            # Also re-read a few units every tick, in case FL didn't report a change.
            count = len(units)
            for _ in range(min(b.sweep_budget, count)):
                self.sweep[b] %= count
                todo.add(units[self.sweep[b]])
                self.sweep[b] += 1
        self.dirty[b].clear()
        self.all_dirty.discard(b)
        for unit in todo:
            if unit not in existing:
                continue
            for key, value in _readable(b.read(unit)):
                until = self.settling.get(key)
                if until is not None:
                    if until > now:
                        continue
                    del self.settling[key]
                before = self.shadow.get(key, _MISSING)
                if before != value:
                    self.shadow[key] = value
                    self._edited(key, now)
                    if send and self._key_ok(key):
                        changes.append((key, value))
                        if not b.transient(key):
                            bases[key] = before
                    elif record and key not in self.offline_base and not b.transient(key):
                        self.offline_base[key] = before

    # --- writing ----------------------------------------------------------------

    def _seed(self, now):
        """Share everything. Starting values (seed) never overwrite changes others
        made meanwhile; a project loaded while alone (not seed) does."""
        self.need_seed = False
        pairs = []
        for key, value in self.shadow.items():
            binding = self.binding_for(key)
            if (binding is not None and binding.seed and not binding.transient(key)
                    and binding.shared_at_start(key, value)):
                pairs.append((key, value))
        self._send(pairs, now, seed=self.seed_initial)
        log("delade dina inställningar med sessionen (%d värden)" % len(pairs))

    def _send(self, pairs, now, seed=False, bases=None):
        """Send changes. `bases`: key -> its value before the change, for the
        changes to send again after a lost connection unless acknowledged."""
        for start in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[start:start + BATCH_SIZE]
            self.seq += 1
            msg = {"t": "sets", "l": self.seq, "kv": [[k, v] for k, v in chunk]}
            if self.my_fp is not None:
                msg["fp"] = self.my_fp  # the relay refuses it if the session's structure changed meanwhile
            if seed:
                msg["seed"] = True
            for key, _ in chunk:
                self.pending[key] = self.seq
                self.pending_since[key] = now
                self.inbox.pop(key, None)  # what came before: the relay puts this after it
                entry = self.unacked.get(key)
                if entry is not None:
                    entry[1] = self.seq
                elif bases is not None and key in bases and not seed:
                    self.unacked[key] = [bases[key], self.seq]
            self.batches[self.seq] = chunk
            self.link.send(msg)
            if self.debug:
                log("-> %s" % ", ".join("%s=%r" % pair for pair in chunk))

    def _apply_inbox(self, now):
        if not self.inbox or self.hold_until is not None:
            return  # holding: a save of ours is being merged (_watch_saves); the newest per key waits
        self._watch_saves(changed_flag(), now)  # saved since the last look? Then don't change FL now
        if self.hold_until is not None or not _safe_to_edit():
            return
        inbox, self.inbox = self.inbox, {}
        announce, self.held_back = not self.held_back, False
        _each(inbox.items(), self._apply, now, announce)  # one value FL can't take doesn't cost the others

    def _apply(self, item, now, announce):
        """Write one of others' values into FL."""
        key, (value, sender) = item
        if key in self.pending:
            return  # our own newer change is on its way and will win everywhere
        binding = self.binding_for(key)
        if binding is None:
            return
        if not binding.transient(key) and self.shadow.get(key, _MISSING) == value:
            return  # FL has it already: writing it would only mark the project changed (and cost a call)
        try:
            actual = binding.write(key, value)
        except Exception:  # (a plugin that is loading): the session's value comes again with the next change
            self._failed(binding, now)
            return
        if actual is None:
            return
        self.shadow[key] = actual
        self.settling[key] = now + SETTLE_TIME
        self.live_times.append(now)  # (FL may count our write as an undo step: that's no content)
        if sender is not None and announce:
            who = self.peers.get(sender, {}).get("name") or "någon"
            self.waiting_hint = "%s: %s" % (who, binding.describe(key))

    def _show_hint(self, now):
        if self.waiting_hint and now >= self.next_hint:
            self.hint(self.waiting_hint)
            self.waiting_hint = None
            self.next_hint = now + HINT_INTERVAL

    # --- content (flsync_content.py) ---------------------------------------------------

    def _edited(self, key, now):
        """A change was seen here: a live value (a key that means the same in
        the session), or else content, which only a saved version can carry
        (not while the bridge brings someone's channels in: `adding`)."""
        if key is None and now < self.remote_until:
            return
        self.local_at = max(self.local_at, now)
        if key is not None and self._key_ok(key):
            self.live_times.append(now)
        else:
            self.content += 1

    def _look_for_content(self, now):
        """Edits of the playlist, of notes, of plugins' insides show only as
        undo steps (OnRefresh too), more patterns or a longer song. An undo step
        is content once CONTENT_WINDOW has shown no live change around it."""
        marks = flsync_content.marks()
        if self.marks is not None:
            if marks[0] != self.marks[0]:
                self.undo_steps.append(now)
            if marks[1:] != self.marks[1:]:
                self._edited(None, now)
        self.marks = marks
        self.live_times = [t for t in self.live_times if now - t <= 2 * CONTENT_WINDOW + STATUS_INTERVAL]
        waiting = []
        for at in self.undo_steps:
            if now - at < CONTENT_WINDOW:
                waiting.append(at)
            elif not any(abs(t - at) <= CONTENT_WINDOW for t in self.live_times):
                self._edited(None, at)
        self.undo_steps = waiting

    def _forget_content(self):
        self.content, self.undo_steps, self.marks = 0, [], None
        self.pull = self.pull_at = self.structure_at = None

    def _save_by_itself(self, now):
        """Content goes out without Ctrl+S: FL saves by itself once nothing has
        changed here for AUTOSAVE_QUIET, and the bridge shares the save as for
        Ctrl+S. For someone else's newer version (`pull`) FL saves once it is
        stopped and nothing has changed for PULL_IDLE, after a countdown in the
        hint bar: the bridge merges the save with that version and opens it,
        so nothing of ours is lost."""
        if not (self.can_save and self.autosave_ok and self.from_idle) or self.hold_until is not None:
            return
        if self.fetch is not None:
            return  # (someone's notes coming in: saved once they all are, not half of them)
        if not self._other_fls() or now < self.next_autosave or self.link.busy():
            return
        quiet = now - self.local_at
        if self.pull is not None:
            if is_playing() or flsync_content.recording() or quiet < PULL_IDLE:
                self.pull_at = None  # not while it plays or someone works here
            elif self.pull_at is None:
                self.pull_at = now + PULL_COUNTDOWN
                self.waiting_hint = "FLSync: hämtar %s ändringar om %d s%s" % (
                    _genitive(_who(self.pull.get("by"))), PULL_COUNTDOWN, _takes(self.pull.get("took")))
            elif now >= self.pull_at:
                self._save_now(now, pull=True)
            return
        new_structure = self.structure_at is not None and now - self.structure_at >= STRUCTURE_SAVE
        if (self.content and (quiet >= AUTOSAVE_QUIET or new_structure) and not flsync_content.recording()
                and not (self.slow_save and is_playing())):
            # A new instrument goes out at once, so the others get it (live, as a
            # preset) while it's being played with here.
            self._save_now(now, pull=False)
        elif (self.status and self.status[0] and quiet >= DIRTY_QUIET and now >= self.next_check
              and now >= self.remote_until and not is_playing() and not flsync_content.recording()):
            # An edit nothing above showed (FL doesn't make every edit an undo
            # step): the bridge compares the save with the session's version.
            self.next_check = now + DIRTY_INTERVAL
            self._save_now(now, pull=False, check=True)

    def _save_now(self, now, pull, check=False):
        if not _safe_to_edit() or flsync_content.in_menu():
            return  # FL is in a menu or a dialog: next time
        self.next_autosave = now + AUTOSAVE_INTERVAL
        self.saved_by_itself = now
        self.structure_at = None
        self.pull = self.pull_at = None
        # The bridge must hear it before FL saves (which holds FL up), to tell
        # this save from a Ctrl+S: it shares one with nothing new quietly.
        msg = {"t": "autosave", "content": self.content, "pull": pull}
        if check:
            msg["check"] = True
        self.link.send(msg)
        self.link.flush()
        self.content, self.undo_steps = 0, []  # in this save (if FL doesn't, the bridge says so: `broken`)
        began = time.monotonic()
        if not flsync_content.save():
            self.can_save = False
            self._tell("FL kunde inte spara av sig självt, så FLSync gör inte det mer nu. Spara med Ctrl+S för att "
                       "dela det du gör.")
            return
        took = time.monotonic() - began
        if took >= SLOW_SAVE:
            # Heavy plugins (Kontakt, say) make a save hold FL up: less often,
            # and never while it plays, where it would stutter.
            self.next_autosave = now + max(AUTOSAVE_INTERVAL, 10 * took)
            if not self.slow_save:
                self.slow_save = True
                self._tell("FL tog %.1f s att spara, så FL sparar inte av sig självt mitt i en uppspelning" % took)
        if self.debug:
            log("FL sparar av sig självt (%s)" % ("för att hämta de andras version" if pull
                                                  else "%d ändringar som inte synkas live" % self.content))

    # --- structure ----------------------------------------------------------------

    @property
    def synced(self):
        """Whether our project has the session's structure, so all values can flow."""
        return self.session_fp is None or self.my_fp is None or self.my_fp == self.session_fp

    def _key_ok(self, key):
        """Whether a key means the same here as in the session: our structure
        is the session's in the part the key's thing is found by."""
        return not parts_differ(self.my_fp, self.session_fp, key_area(key))

    def _ok_areas(self):
        """The parts of the structure ("c", "m") that are the session's here."""
        return {area for area, _ in AREAS if not parts_differ(self.my_fp, self.session_fp, area)}

    def _paused_text(self):
        """'kanalerna pausade', 'mixern pausad' or 'kanalerna och mixern pausade'."""
        ok = self._ok_areas()
        names = [name for area, name in AREAS if area not in ok] or [name for _, name in AREAS]
        return "%s %s" % (" och ".join(names), "pausad" if names == ["mixern"] else "pausade")

    def _other_fls(self):
        return any(p.get("app") == "fl" for p in self.peers.values())

    def _effective_fp(self):
        """Our structure as the session should see it: the version we opened
        (`loaded`) while FL's own reading of it hasn't changed since, else FL's."""
        if self.alias is not None and self.alias[0] == self.local_fp:
            return self.alias[1]
        return self.local_fp

    def _check_fp(self, now):
        self.next_fp = now + FP_INTERVAL
        self.fp_due = False
        fp = self.fingerprint()
        if fp is None and self.local_fp is not None:
            return  # FL can't tell just now (a plugin loading): the structure is what it said before
        self.local_fp = fp
        fp = self._effective_fp()
        if fp == self.my_fp:
            return
        remote = fp == self.expect_fp  # someone's channels the bridge brought in: like opening their version
        if remote:
            self.expect_fp = None
        self._new_fp(fp, load=remote)

    def _new_fp(self, fp, load=False):
        """Our structure changed (as the session sees it): tell the relay, and
        pause or resume. `load`: because of what we opened, not an edit."""
        was_ok, self.my_fp = self._ok_areas(), fp
        lost, gained = was_ok - self._ok_areas(), self._ok_areas() - was_ok
        if not load:
            self._edited(None, self.last_tick)  # channels or plugins added, removed or replaced: content
            self.structure_at = self.last_tick  # ...saved soon, also while the new plugin is played with
        if lost:  # others' values not written yet: made for what the numbers meant before
            self.inbox = {k: v for k, v in self.inbox.items() if key_area(k) not in lost}
        if not self.joined:
            if self.offline_base and fp != self.offline_fp:
                # Keys address channels and plugins by number: they may mean other things now.
                log("projektets kanaler eller plugins ändrades, så %s du gjort offline skickas inte"
                    % _amount(len(self.offline_base), "ändring", "ändringar"))
                self.offline_base.clear()
            return
        msg = {"t": "fp", "fp": fp}
        if load:
            msg["load"] = True
        self.link.send(msg)
        if not self._other_fls():
            return  # alone: the relay takes our structure and answers with `reshare`
        if gained:
            self.link.send({"t": "sync"})  # back to the session's structure there: take its values
        if lost:
            self._tell("du har ändrat projektets plugins eller kanaler, så %s tills du sparar (Ctrl+S) och "
                       "delar ändringen. Resten synkar som vanligt." % self._paused_text().replace("pausad", "är pausad"))
            self._update_pause("local")
        elif gained:
            self._update_pause()

    def _on_loaded(self, msg):
        """The bridge saw FL open the project it wrote for version `v`: our
        structure is that version's, even if FL reads it differently here (a
        plugin missing on this computer, another FL version)."""
        fp = msg.get("fp")
        if not isinstance(fp, str) or not fp or self.local_fp is None:
            return
        if self.local_fp != self.load_fp:
            return  # the structure was edited since FL opened it: that's ours now, not the version's
        self.alias = (self.local_fp, fp)
        if fp != self.local_fp:
            log("du har öppnat version %s; det som skiljer här (t.ex. plugins som saknas) hindrar inte synken"
                % msg.get("v"))
        if fp != self.my_fp and self.joined:
            self._new_fp(fp, load=True)
        else:
            self.my_fp = fp

    def _on_pen(self, msg):
        """Who is changing the session's structure (one at a time)."""
        holder = msg.get("id")
        if holder is None:
            self.pen = None
            return
        self.pen = {"id": holder, "name": msg.get("name")}
        if holder == self.my_id:
            return  # our own change: the pause hint already says to save it
        if msg.get("busy"):
            log("%s ändrar redan projektets struktur. Din ändring kan skrivas över när %s sparar."
                % (msg.get("name"), msg.get("name")))
            self.waiting_hint = "FLSync: %s ändrar redan strukturen, din ändring kan skrivas över" % msg.get("name")
        else:
            log("%s ändrar projektets struktur" % msg.get("name"))
            self.waiting_hint = "FLSync: %s ändrar projektets struktur" % msg.get("name")

    def _on_snapshot(self, msg):
        """Someone (maybe we) shared a new version of the project. The bridge
        tells us at once, and once more with `path` when it has written the
        project folder for us."""
        was_ok, previous = self._ok_areas(), self.snapshot or {}
        self.snapshot = msg
        if msg.get("fp"):
            self.session_fp = msg.get("fp")
        lost, gained = was_ok - self._ok_areas(), self._ok_areas() - was_ok
        if lost:
            self.inbox = {k: v for k, v in self.inbox.items() if key_area(k) not in lost}
        if msg.get("from") == self.my_id:
            log("din version (%s) är delad med sessionen" % msg.get("v"))
            self.waiting_hint = "FLSync: din version är delad"
            self._update_pause()
            return  # a `reshare` follows if it changed the structure
        if (msg.get("v"), msg.get("from")) == (previous.get("v"), previous.get("from")):
            self._log_whereabouts(msg)
            return  # the same version, now with its folder
        log("%s delade version %s av %s" % (msg.get("by"), msg.get("v"), msg.get("name")))
        saved = msg.get("how") == "save"  # a save: our Ctrl+S merges it in (else: open it on the FLSync page)
        if lost:
            log("den har andra plugins eller kanaler än ditt projekt, så %s tills du %s"
                % (self._paused_text().replace("pausad", "är pausad"),
                   "sparar (Ctrl+S) och får med den" if saved else "öppnar den"))
        if gained:
            self.link.send({"t": "sync"})
        if msg.get("switch"):
            # Another project: Ctrl+S keeps ours in its file, and FL opens theirs then.
            log("%s bytte sessionens projekt till %s. %s" % (
                _who(msg.get("by")), _shown(msg.get("name")),
                "Det öppnas nu." if msg.get("how") is None else "Spara ditt (Ctrl+S), så öppnas det."))
            self.waiting_hint = "FLSync: %s bytte projekt till %s%s" % (
                _who(msg.get("by")), _shown(msg.get("name")), "" if msg.get("how") is None else ", tryck Ctrl+S")
        elif saved and self.can_save and self.autosave_ok:
            # FL saves by itself and gets it in (_save_by_itself, with the bridge's `pull`).
            log("%s har sparat. Ändringarna hämtas in när du tar en paus: uppspelningen stoppad och ingen tangent "
                "eller mus en stund (eller nu med Ctrl+S)." % _who(msg.get("by")))
            self.waiting_hint = "FLSync: %s har sparat, hämtas in när du tar en paus (Ctrl+S nu)" % _who(msg.get("by"))
        elif saved:
            log("spara (Ctrl+S) för att få med %s ändringar i ditt projekt" % _genitive(_who(msg.get("by"))))
            self.waiting_hint = "FLSync: %s har sparat, tryck Ctrl+S för att få med ändringarna" % _who(msg.get("by"))
        elif msg.get("how") is None:
            # FL has nothing unsaved: the bridge opens it at a pause (`opening` says when).
            self.waiting_hint = "FLSync: %s har sparat, öppnas när du tar en paus" % _who(msg.get("by"))
        else:
            self.waiting_hint = "FLSync: %s delade en ny version av %s" % (_who(msg.get("by")), _shown(msg.get("name")))
        self._log_whereabouts(msg)  # its warnings (missing plugins, too new for this FL) win the hint bar
        self._update_pause("remote")

    def _log_whereabouts(self, msg):
        """Where the bridge put a version, the plugins it couldn't find here, and
        whether this FL is too old to open it."""
        if msg.get("path"):
            log("version %s av %s ligger i %s" % (msg.get("v"), msg.get("name"), msg.get("path")))
        missing = [_missing_text(m) for m in msg.get("missing") or []]
        if missing:
            log("saknas här: %s. Projektet går att öppna, men de kanalerna låter inte här; synken fungerar ändå."
                % ", ".join(missing))
            self.waiting_hint = "FLSync: saknas här: %s" % ", ".join(missing)
        newer = msg.get("newer")
        if newer:
            # FL opens a newer FL's project only after a warning: Ctrl+S brings it
            # in instead, merged in this FL's version (the bridge never opens it).
            log("versionen är sparad i FL %s och du har %s. Tryck Ctrl+S, så slås den ihop med ditt projekt i "
                "din FL-version (öppnar du den direkt varnar FL)." % (newer, self.flv or "en äldre"))
            self.waiting_hint = "FLSync: sparad i FL %s, tryck Ctrl+S för att få den i din FL" % newer

    def _on_notes(self, msg):
        """Someone sent the notes of a pattern's channel from their piano roll."""
        if msg.get("from") == self.my_id:
            self.waiting_hint = "FLSync: dina noter är skickade (%s st)" % msg.get("n")
            return
        try:
            channel = int(msg.get("c")) + 1
        except (TypeError, ValueError):
            channel = msg.get("c")
        where = "kanal %s i mönster %s" % (channel, msg.get("p"))
        log("%s skickade noter till %s (%s st). Hämta dem: öppna pianorullen för den kanalen och kör "
            "Tools > Scripts > FLSync hämta noter." % (msg.get("by"), where, msg.get("n")))
        self.waiting_hint = "FLSync: %s skickade noter till %s" % (msg.get("by"), where)

    def _on_saved(self, msg, now):
        """The bridge has dealt with a save of ours (docs/phase4.md, "Saving").
        `open: "now"`: it opens the merged version as soon as it can, so others'
        values keep waiting until that project load (at most HOLD_OPEN_TIMEOUT);
        any other answer ends the wait."""
        outcome, opening = msg.get("outcome"), msg.get("open")
        if not isinstance(opening, str) or opening not in OPEN_HINTS:
            opening = None
        if opening == "now":
            self.hold_until = now + HOLD_OPEN_TIMEOUT  # project_load ends it
        else:
            self._end_hold()
        theirs = _possessive(_names(msg.get("with"))) or "de andras"
        why, version = _text(msg.get("why")), _count(msg.get("v"), None)
        conflicts, taken = _count(msg.get("conflicts")), _count(msg.get("taken"), None)
        then = OPEN_HINTS.get(opening, "")
        because = " (%s)" % why if why else ""
        hint = None
        if outcome == "shared":
            log("sparningen är delad%s" % (" som version %d" % version if version is not None else ""))
            hint = "FLSync: din version är delad"
        elif outcome == "merged":
            text = "sparningen slogs ihop med %s ändringar" % theirs
            if version is not None:
                text += " till version %d" % version
            if taken is not None:
                text += ", %s kom från dem" % _amount(taken, "del", "delar")
            if conflicts:
                text += ", %s hade ni båda ändrat och där behölls dina" % _amount(conflicts, "del", "delar")
            log(text)
            clash = ""
            if conflicts:
                clash = " (%s, %s behölls)" % (_amount(conflicts, "krock", "krockar"), "din" if conflicts == 1 else "dina")
            hint = "FLSync: sammanslaget med %s ändringar%s%s" % (theirs, clash, then)
        elif outcome == "pulled":
            log("du hade inget nytt att dela, så du får %s version" % theirs)
            hint = "FLSync: hämtar %s version%s" % (theirs, then)
        elif outcome == "unchanged":
            if now - self.saved_by_itself > HOLD_TIMEOUT or self.debug:
                log("du hade inget nytt att dela")  # (FL saving by itself says nothing about it)
            if opening:
                hint = "FLSync: inget nytt att dela%s" % then
        elif outcome == "unrelated":
            confirm = _count(msg.get("confirm"), None)
            if confirm:
                self._tell("projektet hör inte till sessionen, så sparningen delades inte. Spara igen (Ctrl+S) inom "
                           "%d sekunder för att göra det till allas projekt, eller öppna sessionens version i "
                           "FLSync-fönstret." % confirm)
                hint = "FLSync: inte sessionens projekt. Ctrl+S igen inom %d s gör det till allas" % confirm
            else:
                log("projektet hör inte till sessionen, så sparningen delades inte. Öppna sessionens version i "
                    "FLSync-fönstret, eller gör ditt projekt till sessionens där (Dela mitt projekt).")
                hint = "FLSync: projektet hör inte till sessionen, öppna sessionens version i FLSync-fönstret"
        elif outcome == "switched":
            self._tell("ditt projekt är nu sessionens%s: de andra får det öppnat"
                       % (" (version %d)" % version if version is not None else ""))
            hint = "FLSync: ditt projekt är nu allas"
        elif outcome == "old":
            log("%s bytte sessionens projekt, så din sparning delades inte (den finns kvar i din fil)"
                % _who(", ".join(_names(msg.get("with")))))
            hint = "FLSync: %s bytte projekt%s" % (_who(", ".join(_names(msg.get("with")))), then)
        elif outcome == "replaced":
            log("sparningen kunde inte slås ihop med %s version%s, så din version ersatte den. Deras finns kvar "
                "i mappen Versioner." % (theirs, because))
            hint = "FLSync: kunde inte slås ihop%s, din version ersatte %s" % (because, theirs)
        elif outcome == "failed":
            log("kunde inte dela projektet%s" % (": %s" % why if why else ""))
            hint = "FLSync: kunde inte dela projektet%s" % because
        else:
            log("bryggan sa något okänt om sparningen: %s" % _shown(outcome))
        if opening:
            log(OPEN_LOGS[opening])
        if hint:
            self.waiting_hint = hint

    def _update_pause(self, cause=None):
        """Keep the hint bar's reason for a pause up to date (shown now and every
        PAUSE_HINT_INTERVAL). `cause` says what just made us differ: "local" (our
        own structure changed), "remote" (someone shared another structure) or
        "other" (we joined or opened a project unlike the session's)."""
        if not self.joined or self.synced:
            if self.pause_reason:
                self.pause_reason = None
                log("synkar igen")
                self.waiting_hint = "FLSync: synkar igen"
            return
        if cause is None and self.pause_reason:
            return
        latest = self.snapshot or {}
        theirs = latest.get("fp") == self.session_fp and latest.get("from") not in (None, self.my_id)
        paused = "FLSync: %s" % self._paused_text()  # the rest (tempo, play, playlist tracks...) still syncs
        if cause == "local":
            reason = "%s, spara för att dela din ändring" % paused
        elif theirs and latest.get("switch"):  # Ctrl+S keeps ours in its file and opens theirs
            reason = "%s, tryck Ctrl+S för att öppna %s projekt" % (paused, _genitive(_who(latest.get("by"))))
        elif theirs and latest.get("how") == "save":  # Ctrl+S merges it in and opens the result
            reason = "%s, tryck Ctrl+S för att få %s version" % (paused, _genitive(_who(latest.get("by"))))
        elif theirs:
            reason = "%s, öppna %s version av projektet" % (paused, _genitive(latest.get("by")))
        else:
            reason = "%s, öppna samma projekt som de andra, eller be dem spara det" % paused
        if reason != self.pause_reason:
            self.pause_reason = reason
            self.next_pause_hint = 0.0  # show it at once


def _each(items, work, *args):
    """work(item, *args) for every item, also when it fails for some: one bad
    message or value mustn't cost the others. The first exception is raised
    once all are done, for the script to report."""
    error = None
    for item in items:
        try:
            work(item, *args)
        except Exception as e:
            if error is None:
                error = e
    if error is not None:
        raise error


def _error_text():
    """The exception being handled, in one line."""
    error = sys.exc_info()[1]
    return ("%s: %s" % (error.__class__.__name__, error))[:200] if error is not None else "okänt fel"


def _readable(pairs):
    """The (key, value) pairs of a read without numbers FL got wrong: NaN never
    equals itself, so it would look changed at every read and be sent over and
    over (some buggy plugins report it), and neither it nor infinity is JSON."""
    for key, value in pairs:
        if not isinstance(value, float) or math.isfinite(value):
            yield key, value


def _missing_text(entry):
    """'Serum (kanal 3, 7)' from {plugin, channels}; plain names as they are."""
    if not isinstance(entry, dict):
        return str(entry)
    channels = entry.get("channels") or []
    if channels:
        return "%s (kanal %s)" % (entry.get("plugin"), ", ".join(str(c) for c in channels))
    if entry.get("mixer"):
        return "%s (mixern)" % entry.get("plugin")
    return str(entry.get("plugin"))


def _genitive(name):
    """'Anna' -> 'Annas', 'Jonas' -> 'Jonas' (Swedish)."""
    name = str(name or "någons")
    return name if name[-1:].lower() in ("s", "x", "z") else name + "s"


# --- fields from the network: anything may be missing or of another type ----------

def _who(name):
    """A name to show: the text, or 'någon'."""
    return name.strip() if isinstance(name, str) and name.strip() else "någon"


def _names(value, limit=10):
    """The names in a list: its non-empty strings, at most `limit`, each kept short."""
    if not isinstance(value, list):
        return []
    return [name.strip()[:40] for name in value if isinstance(name, str) and name.strip()][:limit]


def _possessive(names):
    """'Kims', 'Kims och Annas', 'Kims, Annas och Bos'; '' for no names."""
    words = [_genitive(name) for name in names]
    if len(words) < 2:
        return "".join(words)
    return "%s och %s" % (", ".join(words[:-1]), words[-1])


def _count(value, default=0):
    """A whole number of at least 0, else `default`."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _text(value, limit=100):
    """A text on one line, cut to `limit` characters; None if there is none."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _shown(value):
    """Anything, briefly, for a log line."""
    return repr(value)[:60]


def _number(value, default):
    """A whole number of seconds from the bridge, or `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 3600:
        return default
    return int(round(value))


def _takes(seconds):
    """' (tar ~14 s)': how long opening the project took last time, if FL knows."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds < 3600:
        return ""
    return " (tar ~%d s)" % int(round(seconds))


def _amount(count, one, many):
    """'1 ändring', '3 ändringar'."""
    return "%d %s" % (count, one if count == 1 else many)
