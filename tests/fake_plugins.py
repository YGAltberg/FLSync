"""Fake FL plugins: generators on channels and effects in mixer slots, behind
FL's plugins module, the window focus functions of ui (which plugin window is
in front) and the midi constants they use.

Like FL 2025.2.4 where it matters (partly measured live):
  * A VST reports 4240 parameter slots: 4096 parameters, nearly all of them
    without a name, then 128 MIDI CCs and 16 aftertouch. FL's own plugins
    report their own parameters, all named.
  * FL's own plugins keep values in steps, VSTs as 32-bit floats. A plugin
    made with lag=True shows a script's value only after FL's next refresh.
  * With no plugin window in front the form ID is -1 and the plugin name "".
    A generator window's form ID is its channel's global index (what FL gave in
    a project without channel groups; with a group on show it may count within
    the group, which tests set up themselves), an effect window's
    track * 4194304 + slot * 65536 (as mixer.getTrackPluginId()).
    getFocusedPluginName() is the plugin's own name. Opening a generator's
    window selects its channel.
  * Calls that address a channel, slot, plugin or parameter that doesn't exist
    are recorded in project.invalid (FL only prints a warning). So are calls
    that address a channel without useGlobalIndex: FLSync goes by global index.

Generators sit on the channels of the fake channel rack (project.rack) if
there is one, else on channels of their own. Turning a knob (turn()) changes
the value without telling the script. `calls` counts the plugins functions
called. Option (FakeFL keyword argument): channels=4, if there's no rack.
"""

import collections
import struct
import types

SLOTS = 10
VST_PARAMS = 4096        # ...then 128 MIDI CC and 16 aftertouch slots, 4240 in all
TRACK_ID, SLOT_ID = 4194304, 65536
CT_GENPLUG = 2           # midi.CT_GenPlug: what getChannelType() says of a channel with a plugin
MISSING = object()       # what a call addressed doesn't exist

# Real values from FL's midi.py.
MIDI = {"widMixer": 0, "widChannelRack": 1, "widPlaylist": 2, "widPianoRoll": 3, "widBrowser": 4,
        "widPlugin": 5, "widPluginEffect": 6, "widPluginGenerator": 7,
        "PIM_None": 0, "PIM_AlwaysPickup": 1, "PIM_FollowGlobal": 2}
GENERATOR_WINDOWS = (MIDI["widPlugin"], MIDI["widPluginGenerator"])
EFFECT_WINDOWS = (MIDI["widPlugin"], MIDI["widPluginEffect"])


class Plugin:
    """One plugin instance: its own name and a value per parameter slot."""

    def __init__(self, name, names, steps=None, lag=False, user_name=None, value=0.5):
        self.name = name
        self.user_name = user_name or name  # what the user called its channel or slot
        self.names = list(names)            # "" for unused slots
        self.steps = steps                  # None: a 32-bit float
        self.lag = lag
        self.values = [self.held(value)] * len(self.names)  # what the plugin has
        self.shown = list(self.values)                      # what getParamValue() says

    def held(self, value):
        """`value` as the plugin keeps it."""
        value = min(1.0, max(0.0, float(value)))
        if self.steps:
            return round(value * self.steps) / self.steps
        return struct.unpack("f", struct.pack("f", value))[0]


def native(name, names, steps=1000, **options):
    """One of FL's own plugins, with `names` as its parameters."""
    return Plugin(name, names, steps=steps, **options)


def vst(name, names, **options):
    """A VST in FL's wrapper: `names`, unnamed slots up to 4096, then 144 MIDI slots."""
    slots = list(names) + [""] * (VST_PARAMS - len(names))
    slots += ["MIDI CC #%d" % cc for cc in range(128)] + ["Aftertouch %d" % (ch + 1) for ch in range(16)]
    return Plugin(name, slots, **options)


def _channel():
    return types.SimpleNamespace(plugin=None)


class FakePlugins:
    def __init__(self, project, channels=4):
        self.project = project
        self.own_channels = [_channel() for _ in range(channels)]  # used if there's no rack
        self.effects = {}                    # (track, slot) -> Plugin
        self.focus = None                    # ("window", wid), ("generator", channel) or ("effect", track, slot)
        self.selected = 0                    # the selected channel's global index, if there's no rack
        self.calls = collections.Counter()

    def rack(self):
        return getattr(self.project, "rack", None)

    def channels(self):
        """All channels in global order; a channel's generator is its `plugin` (None: a sampler, say)."""
        rack = self.rack()
        return rack.channels if rack is not None else self.own_channels

    # --- what the user does in FL ---------------------------------------------------

    def add_generator(self, channel, plugin):
        """Put `plugin` on channel `channel` (global index), adding channels up to it."""
        rack = self.rack()
        while len(self.channels()) <= channel:
            if rack is not None:
                rack.add_channel(len(rack.channels), plugin.name)
            else:
                self.own_channels.append(_channel())
        holder = self.channels()[channel]
        holder.plugin = plugin
        if rack is not None:
            holder.kind = CT_GENPLUG
            holder.default_name = plugin.name  # a channel is called after its plugin until renamed
        return plugin

    def add_effect(self, track, slot, plugin):
        self.effects[(track, slot)] = plugin
        mixer = getattr(self.project, "mixer", None)
        if mixer is not None:
            mixer.add_plugin(track, slot)
        return plugin

    def focus_generator(self, channel):
        self.focus = ("generator", channel)
        self.select(channel)  # opening a channel's plugin selects the channel

    def select(self, channel):
        if self.rack() is not None:
            self.rack().selected = {channel}
        else:
            self.selected = channel

    def focus_effect(self, track, slot):
        self.focus = ("effect", track, slot)

    def focus_window(self, wid=None):
        """Another window (a wid* number), or None: nothing FL knows, e.g. Script output."""
        self.focus = None if wid is None else ("window", wid)

    def turn(self, where, param, value):
        """Turn a knob in the plugin's window."""
        plugin = self.get(where)
        plugin.values[param] = plugin.shown[param] = plugin.held(value)

    def value(self, where, param):
        return self.get(where).shown[param]

    def get(self, where):
        """The plugin on a channel (int) or in a mixer slot ((track, slot)), or None."""
        if isinstance(where, tuple):
            return self.effects.get(where)
        channels = self.channels()
        return getattr(channels[where], "plugin", None) if 0 <= where < len(channels) else None

    def refresh(self):
        generators = [getattr(channel, "plugin", None) for channel in self.channels()]
        for plugin in [p for p in generators if p] + list(self.effects.values()):
            plugin.shown = list(plugin.values)

    # --- what the plugins functions point at --------------------------------------------

    def at(self, index, slot, global_index):
        """A Plugin, None for a place without one, or MISSING (recorded)."""
        if slot == -1:
            if not global_index:
                self.project.bad(("plugins: channel without useGlobalIndex", index))
                return MISSING
            if not (isinstance(index, int) and 0 <= index < self.channel_count()):
                self.project.bad(("plugins: channel", index))
                return MISSING
            return self.get(index)
        if not (isinstance(index, int) and isinstance(slot, int) and 0 <= slot < SLOTS
                and 0 <= index < len(self.project.mixer.tracks)):
            self.project.bad(("plugins: mixer slot", index, slot))
            return MISSING
        return self.effects.get((index, slot))

    def plugin(self, call, index, slot, global_index, param=None):
        """The plugin a call that needs one points at, else None (recorded)."""
        plugin = self.at(index, slot, global_index)
        if plugin is MISSING:
            return None
        if plugin is None:
            self.project.bad(("plugins.%s without a plugin" % call, index, slot))
            return None
        if param is not None and not (isinstance(param, int) and 0 <= param < len(plugin.names)):
            self.project.bad(("plugins.%s parameter" % call, index, slot, param))
            return None
        return plugin

    def channel_count(self):
        return self.project.module("channels").channelCount(1)

    # --- focus ----------------------------------------------------------------------------

    def is_focused(self, wid):
        kind = self.focus[0] if self.focus else None
        if kind == "generator":
            return wid in GENERATOR_WINDOWS
        if kind == "effect":
            return wid in EFFECT_WINDOWS
        return kind == "window" and wid == self.focus[1]

    def form_id(self):
        if self.focus is None:
            return -1
        if self.focus[0] == "effect":
            return self.focus[1] * TRACK_ID + self.focus[2] * SLOT_ID
        return self.focus[1]  # a channel's global index, or another window's own number

    def focused_plugin(self):
        if self.focus and self.focus[0] == "generator":
            return self.get(self.focus[1])
        if self.focus and self.focus[0] == "effect":
            return self.effects.get(self.focus[1:])
        return None

    def active_effect(self):
        return self.focus[1:] if self.focus and self.focus[0] == "effect" else None


def install(project):
    fake = FakePlugins(project, project.options.get("channels", 4))
    project.plugins = fake
    project.refreshers.append(fake.refresh)

    midi = project.module("midi")
    for name, value in MIDI.items():
        setattr(midi, name, value)

    ui = project.module("ui")
    ui.getFocused = fake.is_focused
    ui.getFocusedFormID = fake.form_id
    ui.getFocusedPluginName = lambda: getattr(fake.focused_plugin(), "name", "")

    m = project.module("mixer")
    if not hasattr(m, "getActiveEffectIndex"):
        m.getActiveEffectIndex = fake.active_effect

    # The channel rack, unless its own fake provides it: no groups, so an index
    # within the group on show is the global index.
    c = project.module("channels")

    def getChannelIndex(index):
        if not 0 <= index < len(fake.own_channels):
            project.bad(("channels.getChannelIndex", index))
        return index

    def channelNumber(canBeNone=False, offset=0):  # the selected channel, always by global index
        rack = fake.rack()
        selected = sorted(rack.selected) if rack is not None else [fake.selected]
        return selected[min(offset, len(selected) - 1)] if selected else -1 if canBeNone else 0
    for name, function in (("channelCount", lambda globalCount=False: len(fake.own_channels)),
                           ("getChannelIndex", getChannelIndex), ("channelNumber", channelNumber)):
        if not hasattr(c, name):
            setattr(c, name, function)

    p = project.module("plugins")

    def isValid(index, slotIndex=-1, useGlobalIndex=False):
        return fake.at(index, slotIndex, useGlobalIndex) not in (None, MISSING)

    def getPluginName(index, slotIndex=-1, userName=False, useGlobalIndex=False):
        plugin = fake.plugin("getPluginName", index, slotIndex, useGlobalIndex)
        return "" if plugin is None else plugin.user_name if userName else plugin.name

    def getParamCount(index, slotIndex=-1, useGlobalIndex=False):
        plugin = fake.plugin("getParamCount", index, slotIndex, useGlobalIndex)
        return 0 if plugin is None else len(plugin.names)

    def getParamName(paramIndex, index, slotIndex=-1, useGlobalIndex=False):
        plugin = fake.plugin("getParamName", index, slotIndex, useGlobalIndex, paramIndex)
        return "" if plugin is None else plugin.names[paramIndex]

    def getParamValue(paramIndex, index, slotIndex=-1, useGlobalIndex=False):
        plugin = fake.plugin("getParamValue", index, slotIndex, useGlobalIndex, paramIndex)
        return 0.0 if plugin is None else plugin.shown[paramIndex]

    def setParamValue(value, paramIndex, index, slotIndex=-1, pickupMode=0, useGlobalIndex=False):
        plugin = fake.plugin("setParamValue", index, slotIndex, useGlobalIndex, paramIndex)
        if plugin is not None:
            plugin.values[paramIndex] = plugin.held(value)
            if not plugin.lag:
                plugin.shown[paramIndex] = plugin.values[paramIndex]

    def counted(function):
        def call(*args, **kwargs):
            fake.calls[function.__name__] += 1
            return function(*args, **kwargs)
        return call
    for function in (isValid, getPluginName, getParamCount, getParamName, getParamValue, setParamValue):
        setattr(p, function.__name__, counted(function))
