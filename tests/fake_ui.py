"""FL's windows, the piano roll's Tools menu and the playlist selection, as the
script sees them through `ui`, `arrangement` and a few others (measured in FL
26.1.7, see docs/protocol_v2.md, "Notes"):

* ui.openEventEditor(channels.getRecEventId(c) + REC_Chan_PianoRoll, EE_PR)
  opens channel c's piano roll and puts it in front; showWindow/setFocused put
  a window in front.
* transport.globalTransport(FPT_ItemMenu, ...) with the piano roll in front
  opens its Tools menu: one list, the built-in tools, then the scripts sorted by
  name. ui.up from a menu just opened goes to its last item, then up from there;
  ui.enter runs the item (a script: `project.on_script(name)` if a test set it),
  ui.escape closes the menu.
* None of it is an edit: no undo step, nothing unsaved.
"""

MIDI = {"widMixer": 0, "widChannelRack": 1, "widPlaylist": 2, "widPianoRoll": 3, "widBrowser": 4,
        "REC_Chan_PianoRoll": 0x2001, "EE_PR": 0, "FPT_ItemMenu": 91}
BUILT_IN = ["Riff machine...", "Quantize...", "LFO..."]  # (a few of them: the scripts come after)
FACTORY_SCRIPTS = ["Arpeggiator", "Euclidean", "Humanize", "Note repeater sprinkler", "Select by velocity and more"]


class FakeWindows:
    def __init__(self, project):
        self.project = project
        self._focused = 1          # the channel rack (unless the plugins' fake says which window is in front)
        self.visible = {0, 1, 2}
        self.piano_roll = None     # (pattern, channel) it shows
        self.menu = None           # the open menu's items...
        self.at = None             # ...and the one highlighted
        self.selection = (-1, -1)  # the playlist's, in ticks
        self.scripts = list(FACTORY_SCRIPTS)  # the user's are added by tests
        self.ran = []              # scripts the user (or FLSync) ran
        self.selects = []          # selectOneChannel's (index, useGlobalIndex)

    @property
    def focused(self):
        """The main window in front: the plugins' fake keeps it when there is one (("window", wid))."""
        plugins = getattr(self.project, "plugins", None)
        if plugins is None:
            return self._focused
        return plugins.focus[1] if plugins.focus and plugins.focus[0] == "window" else None

    @focused.setter
    def focused(self, wid):
        plugins = getattr(self.project, "plugins", None)
        if plugins is None:
            self._focused = wid
        else:
            plugins.focus = None if wid is None else ("window", wid)


def install(project):
    w = FakeWindows(project)
    project.windows = w
    project.on_script = None
    midi = project.module("midi")
    for name, value in MIDI.items():
        setattr(midi, name, value)

    ui = project.module("ui")
    if not hasattr(ui, "getFocused"):  # (the plugins' fake has one that knows plugin windows too)
        ui.getFocused = lambda wid: int(w.focused == wid)
    ui.getVisible = lambda wid: int(wid in w.visible)

    def show(wid, value=1):
        (w.visible.add if value else w.visible.discard)(wid)
    ui.showWindow = show

    def focus(wid):
        w.visible.add(wid)
        w.focused = wid
    ui.setFocused = focus

    def open_editor(event, mode):
        if mode == MIDI["EE_PR"]:
            channel = event - MIDI["REC_Chan_PianoRoll"]
            w.piano_roll = (project.rack.current, channel >> 16)
            focus(MIDI["widPianoRoll"])
    ui.openEventEditor = open_editor
    ui.isInPopupMenu = lambda: int(w.menu is not None)

    def step(by):
        def move():
            if w.menu is None:
                return 0
            w.at = (len(w.menu) - 1 if by < 0 else 0) if w.at is None else (w.at + by) % len(w.menu)
            return 1
        return move
    ui.up, ui.down = step(-1), step(1)

    def enter():
        if w.menu is None:
            return 0
        item, w.menu, w.at = (w.menu[w.at] if w.at is not None else None), None, None
        if item in w.scripts:
            w.ran.append(item)
            if project.on_script:
                project.on_script(item)
        return 1
    ui.enter = enter

    def escape():
        w.menu, w.at = None, None
        return 1
    ui.escape = escape

    t = project.module("transport")
    saving = t.globalTransport

    def global_transport(command, value, pmeflags=2, flags=15):
        if command == MIDI["FPT_ItemMenu"]:
            if w.focused == MIDI["widPianoRoll"]:
                w.menu, w.at = BUILT_IN + sorted(w.scripts, key=str.casefold), None
            return 1
        return saving(command, value, pmeflags, flags)
    t.globalTransport = global_transport

    c = project.module("channels")
    c.getRecEventId = lambda index, global_index=1: index << 16

    def select_one(index, global_index=0):
        w.selects.append((index, global_index))
        project.rack.selected = {index}  # (no display groups here: a group's index is the global one)
    c.selectOneChannel = select_one

    m = project.module("mixer")
    m.setTrackNumber = lambda track, flags=0: setattr(project.mixer, "selected", track)

    a = project.module("arrangement")
    a.selectionStart = lambda: w.selection[0]
    a.selectionEnd = lambda: w.selection[1]

    def select(start, end, *rest):
        w.selection = (start, end)
        return 0
    a.selectionSet = select
