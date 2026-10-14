"""A simulated FL Studio for the tests: fake versions of FL's script modules
(mixer, channels, plugins, ...) driving the real FLSync script.

The fakes are split by area, one file each (fake_mixer.py, fake_channels.py,
...). Each has `install(project)`, which puts that area's state on the shared
FakeProject and adds its functions to the fake FL modules. They behave like FL
2025 where it matters: values stored with FL's resolution, mute that shows up
only after a refresh, a dynamic mixer, and a record of any call that addresses
something that doesn't exist (FL itself only prints a warning).
"""

import importlib
import importlib.util
import itertools
import os
import queue
import sys
import types

sys.dont_write_bytecode = True  # keep __pycache__ out of the script folder FL scans

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(ROOT, "fl", "FLSync")
SCRIPT = os.path.join(SCRIPT_DIR, "device_FLSync.py")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # FL puts the script folder on the path too

# One module per FL area; missing ones are skipped so areas can land one at a time.
AREAS = ["fake_mixer", "fake_channels", "fake_plugins", "fake_song", "fake_ui"]

_instances = itertools.count()


def load_module(name, path, fakes=None):
    """Import a file as a fresh module, with `fakes` standing in for FL's modules."""
    fakes = fakes or {}
    saved = {key: sys.modules.get(key) for key in fakes}
    sys.modules.update(fakes)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
    return module


class FakeProject:
    """Everything one simulated FL holds, shared by the fake API modules."""

    def __init__(self, **options):
        self.options = options
        self.modules = {}
        self.dirty_mixer = []     # indices for OnDirtyMixerTrack
        self.dirty_channels = []  # (index, flag) for OnDirtyChannel
        self.refresh_flags = []   # flags for OnRefresh
        self.refreshers = []      # FL catching up between two script calls
        self.invalid = []         # calls that addressed something that doesn't exist
        self.hints = []           # ui.setHintMsg texts
        self.changed_flag = 0     # general.getChangedFlag(): 0 saved, 1 changed since, 2 changed but autosaved
        self.undo = 0             # general.getUndoHistoryCount(): tests add steps for edits FL makes undoable
        self.save_requests = 0    # transport.globalTransport(FPT_Save, ...) calls
        self.on_save = None       # what FL does then, if a test says

    def module(self, name):
        if name not in self.modules:
            self.modules[name] = types.ModuleType(name)
        return self.modules[name]

    def bad(self, what):
        self.invalid.append(what)

    def refresh(self):
        for refresher in self.refreshers:
            refresher()


def _install_base(fl, project):
    midi = project.module("midi")
    midi.TN_Master, midi.TN_FirstIns, midi.TN_LastIns, midi.TN_Sel = 0, 1, 2, 3
    midi.PIM_None = 0
    midi.HW_Dirty_Mixer_Controls = 4
    midi.PL_Start, midi.PL_LoadOk, midi.PL_LoadError = 0, 100, 101

    general = project.module("general")
    general.getVersion = lambda: 40
    general.safeToEdit = lambda: fl.safe
    general.getChangedFlag = lambda: project.changed_flag  # tests set it: FL doesn't here by itself
    general.getUndoHistoryCount = lambda: project.undo
    general.getUndoHistoryPos = lambda: 0

    device = project.module("device")
    device.isAssigned = lambda: fl.assigned
    device.midiOutMsg = fl._midi_out
    device.midiOutSysex = fl._sysex_out
    device.getName = lambda: "FLSync"

    ui = project.module("ui")
    ui.setHintMsg = project.hints.append
    ui.getHintMsg = lambda: project.hints[-1] if project.hints else ""
    version = project.options.get("fl_version", (25, 2, 4, 4960))  # as FL 2025.2.4 reports it
    ui.getVersion = lambda mode=4: version[mode] if mode < 4 else "%d.%d.%d [build %d]" % version


class FakeFL:
    """One simulated FL Studio running the FLSync script.

    MIDI events from the bridge wait in `from_bridge` until the next idle();
    events the script sends go to `to_bridge`, which the test harness wires up.
    Events are packed ints, status | data1 << 8 | data2 << 16, as in FL.
    """

    def __init__(self, name, calls_idle=True, assigned=True, debug=False, **options):
        self.name = name
        self.safe = True
        self.assigned = assigned
        self.calls_idle = calls_idle
        self.from_bridge = queue.SimpleQueue()
        self.to_bridge = None
        self.project = FakeProject(**options)
        _install_base(self, self.project)
        for area in AREAS:
            try:
                module = importlib.import_module(area)
            except ModuleNotFoundError as e:
                if e.name != area:
                    raise
                continue
            module.install(self.project)
        self.logs = []
        self.script = load_module("device_FLSync_%d" % next(_instances), SCRIPT, self.project.modules)
        self.script.flsync_log.sink = self.logs.append
        self.script.CONFIG_OVERRIDES = {"name": name, "debug": debug}
        self.script.OnInit()

    @property
    def client(self):
        return self.script._client

    def bindings(self, prefix=None):
        """The script's bindings (as the running client has them), optionally by prefix."""
        found = self.client.bindings
        return [b for b in found if b.prefix == prefix] if prefix else found

    @property
    def mixer(self):
        return self.project.mixer

    @property
    def api(self):
        return self.project.modules["mixer"]

    def _midi_out(self, message):
        if not self.assigned:
            raise RuntimeError("midiOutMsg utan kopplad utgång")
        if self.to_bridge is not None:
            self.to_bridge(message)

    def _sysex_out(self, data):
        raise AssertionError("SysEx från scriptet kraschade FL 2025.2.4 och får inte användas")

    def idle(self):
        """What FL does between two screen updates: catch up, deliver the
        change callbacks and incoming MIDI, then (unless disabled) call OnIdle."""
        p = self.project
        p.refresh()
        dirty, p.dirty_mixer = p.dirty_mixer, []
        for track in dirty:
            self.script.OnDirtyMixerTrack(track)
        dirty, p.dirty_channels = p.dirty_channels, []
        for index, flag in dirty:
            self.script.OnDirtyChannel(index, flag)
        flags, p.refresh_flags = p.refresh_flags, []
        for flag in flags:
            self.script.OnRefresh(flag)
        while True:
            try:
                message = self.from_bridge.get_nowait()
            except queue.Empty:
                break
            event = types.SimpleNamespace(status=message & 0xFF, data1=(message >> 8) & 0x7F,
                                          data2=(message >> 16) & 0x7F, handled=False)
            self.script.OnMidiIn(event)
            if not event.handled:
                raise AssertionError("FL fick spela en synk-händelse")
        if self.calls_idle:
            self.script.OnIdle()

    def close(self):
        self.script.OnDeInit()

    # What a user does in FL. quiet=True changes the value without FL reporting it.
    def set_volume(self, track, value, quiet=False):
        self.api.setTrackVolume(track, value)
        if quiet and track in self.project.dirty_mixer:
            self.project.dirty_mixer.remove(track)

    def set_pan(self, track, value):
        self.api.setTrackPan(track, value)

    def set_mute(self, track, muted):
        self.api.muteTrack(track, int(muted))
        self.project.refresh()

    def load_project(self, volumes):
        """Open another project whose mixer has the given track volumes."""
        midi = self.project.modules["midi"]
        self.script.OnProjectLoad(midi.PL_Start)
        for track, value in volumes.items():
            self.api.setTrackVolume(track, value)
        self.project.changed_flag = 0  # just opened: nothing unsaved
        self.script.OnDirtyMixerTrack(-1)
        self.script.OnProjectLoad(midi.PL_LoadOk)

    def volume(self, track):
        return self.api.getTrackVolume(track)

    def pan(self, track):
        return self.api.getTrackPan(track)

    def muted(self, track):
        return self.api.isTrackMuted(track)
