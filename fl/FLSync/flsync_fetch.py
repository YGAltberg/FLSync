"""Someone's notes into FL's piano rolls without opening their version (the
bridge's `notes_in`: a version that changed only notes). Per piano roll: open
it (pattern, channel), tell the bridge (`notes_at`), which puts the notes in
context.json and says go (`notes_go`); open the piano roll's Tools menu, go up
`up` times to "FLSync hämta noter" and run it; the bridge says how it went
(`notes_done`, from the script's fetched.json). Then where the user was comes
back, and the bridge hears the end (`notes_end`). Measured in FL 26.1.7: none
of it is an edit but the notes themselves (one undo step per piano roll), and
FL's script can do all of it from OnIdle. See docs/protocol_v2.md, "Notes"."""

import channels
import midi
import ui

import flsync_view

try:
    import patterns
except ImportError:
    patterns = None
try:
    import transport
except ImportError:
    transport = None

GLOBAL = 1
START_WAIT = 30.0  # FL gets to stopped and safe within this, or the bridge opens the version after all
STEP_WAIT = 5.0    # the bridge answers each step within this
SETTLE = 0.3       # the piano roll FL just opened is in front this long before its Tools menu is opened...
MENU_WAIT = 1.5    # ...and the menu says it's open (ui.isInPopupMenu) on a tick after, within this


def _call(function, *args):
    try:
        return function(*args)
    except Exception:  # noqa: BLE001 - FL can't just now: the step fails, the bridge opens the version
        return None


class Fetch:
    """One `notes_in`: its piano rolls, one after the other."""

    def __init__(self, msg, now):
        self.nonce = msg.get("nonce")
        self.by = msg.get("by") if isinstance(msg.get("by"), str) else None
        self.up = msg.get("up")
        self.items = [i for i in msg.get("items") or [] if isinstance(i, list) and len(i) >= 2
                      and all(isinstance(v, int) and not isinstance(v, bool) for v in i[:2])]
        self.i = 0
        self.state = "start"
        self.until = now + START_WAIT
        self.opened = now  # when the piano roll was opened
        self.view = None

    @property
    def valid(self):
        return (isinstance(self.nonce, str) and isinstance(self.up, int) and not isinstance(self.up, bool)
                and 0 < self.up < 1000 and bool(self.items))

    def on_message(self, kind, msg, now):
        """`notes_go` and `notes_done` for this fetch; True when it is over: `self.ok` says how."""
        if msg.get("nonce") != self.nonce or msg.get("i") != self.i:
            return False
        if kind == "notes_go" and self.state == "go":
            self.state = "menu"
        elif kind == "notes_done" and self.state == "done":
            if msg.get("ok") is not True:
                return self.end(False, "noterna byttes inte ut")
            self.i += 1
            if self.i >= len(self.items):
                return self.end(True)
            self.state, self.until = "open", now + STEP_WAIT
        return False

    def tick(self, now, send, may_edit):
        """A step from OnIdle; True when it is over. `may_edit`: FL is stopped,
        with no dialog or menu open (its own menu is this one's: "menu open")."""
        if self.state in ("open", "menu") and not may_edit:
            if now <= self.until:
                return False  # (the user is back: not in the way)
            self.view = None  # (nor at the end: FL may play, and a jump to a pattern would switch what plays)
            return self.end(False, "FL var upptaget")
        if self.state in ("go", "menu", "done") and now > self.until:
            if _call(ui.isInPopupMenu):
                _call(ui.escape)
            return self.end(False, "inget svar")
        if self.state == "start":
            if not may_edit:
                return now > self.until and self.end(False, "FL var upptaget")
            self.view = flsync_view.capture()
            self.state = "open"
        if self.state == "open":
            p, c = self.items[self.i][:2]
            if patterns is not None and _call(patterns.patternNumber) != p:
                _call(patterns.jumpToPattern, p)
            _call(channels.selectOneChannel, c, GLOBAL)
            event = _call(channels.getRecEventId, c, GLOBAL)
            if event is None or _call(ui.openEventEditor, event + midi.REC_Chan_PianoRoll, midi.EE_PR) is False:
                return self.end(False, "pianorullen")
            send({"t": "notes_at", "nonce": self.nonce, "i": self.i})
            self.state, self.until, self.opened = "go", now + STEP_WAIT, now
        elif self.state == "menu" and now - self.opened >= SETTLE:
            if not _call(ui.getFocused, midi.widPianoRoll):
                return self.end(False, "pianorullen")
            _call(transport.globalTransport, midi.FPT_ItemMenu, 1, getattr(midi, "PME_System", 2),
                  getattr(midi, "GT_Global", 8))
            self.state, self.until = "menu open", now + MENU_WAIT
        elif self.state == "menu open":
            # (FL says it's in the menu once the menu runs: on a tick after opening it.)
            if not _call(ui.isInPopupMenu):
                return now > self.until and self.end(False, "menyn")
            for _ in range(self.up):
                _call(ui.up)
            _call(ui.enter)
            self.state, self.until = "done", now + STEP_WAIT
        return False

    def end(self, ok, why=None):
        self.ok, self.why = ok, why
        if self.view:
            flsync_view.restore(self.view)
        self.state = "over"
        return True
