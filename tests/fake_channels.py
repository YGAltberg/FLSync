"""Fake FL channel rack, step sequencer and patterns: FL's `channels` and
`patterns` modules.

Like FL 2025.2.4 where it matters (partly measured in the user's FL):
  * Channel functions count within the group the channel rack is filtered to,
    unless useGlobalIndex is set. channelCount() counts the channels the filter
    shows, channelCount(1) all of them; getChannelIndex() turns one into the other.
  * Volume and pan are stored at FL's resolution, pitch in whole cents within
    the channel's pitch range. Colors are signed 32-bit ints (ARGB). New
    channels go to inserts 1, 2, 3...
  * isChannelMuted() shows a mute change only after FL refreshes, like the mixer.
  * Every channel has a row of steps in every pattern, but the grid functions
    only see the current pattern. A pattern is as long as its steps need, in
    whole bars of 16 steps and at least one; getPatternLength() counts steps.
  * Steps that are on have parameters (pitch, velocity, ... as in the graph
    editor; assumed, not measured): a step switched on gets FL's defaults, one
    switched off forgets them. getCurrentStepParam() reads the current
    pattern, and reading a step that is off is recorded like a missing one.
    setStepParameterByIndex() writes any pattern, stores what fits FL's range,
    and does nothing to a step that is off (it doesn't switch it on).
  * Patterns are numbered 1-999 and all of them can be used; patternCount()
    counts only the ones that aren't untouched (isPatternDefault).
  * A call that addresses a channel, step, step parameter or pattern that
    doesn't exist is recorded with project.bad() (FL only prints a warning),
    and so is setChannelPitch's mode 2, which breaks FL's pitch range.

Which callback FL makes for which change is a guess (see the setters); the
bindings must also find changes FL doesn't report.

Options (FakeFL keyword arguments): channels=4.
"""

import types

MIDI = {  # the midi constants this area uses, with FL's values (Shared/Python/Lib/midi.py)
    "CE_New": 0, "CE_Delete": 1, "CE_Replace": 2, "CE_Rename": 3, "CE_Select": 4,
    "HW_Dirty_FocusedWindow": 32, "HW_Dirty_Patterns": 1024, "HW_Dirty_ControlValues": 4096,
    "HW_Dirty_Colors": 8192, "HW_Dirty_Names": 16384, "HW_Dirty_ChannelRackGroup": 32768,
    "HW_ChannelEvent": 65536, "PIM_None": 0, "ChannelDefaultVolume": 1000 / 1280,
    "CT_Sampler": 0, "CT_Hybrid": 1, "CT_GenPlug": 2, "CT_Layer": 3, "CT_AudioClip": 4, "CT_AutoClip": 5,
    "pPitch": 0, "pVelocity": 1, "pRelease": 2, "pFinePitch": 3, "pPan": 4, "pModX": 5, "pModY": 6,
    "pShift": 7,
}
midi = types.SimpleNamespace(**MIDI)

# A step's parameters, midi.pPitch .. midi.pShift: what a step switched on gets,
# and the most FL keeps of each (the least is 0; the ranges are a guess).
STEP_DEFAULTS = (60, 100, 64, 120, 64, 128, 128, 0)
STEP_MAX = (127, 127, 127, 240, 128, 255, 255, 255)

VOL_STEPS = 12800       # FL's default 10000 is midi.ChannelDefaultVolume
PAN_STEPS = 6400        # either way
PITCH_RANGE = 2         # semitones the pitch knob reaches either way
BAR = 16                # steps
PATTERN_MAX = 999
NAMES = ("Kick", "Clap", "Hat", "Snare")
SAMPLER_COLOR = -10860435   # a new sampler channel in FL 2025.2.4 (0xFF5A486D)
PATTERN_COLOR = -10721942   # 0xFF5C656A


def int32(value):
    return (int(value) + 2 ** 31) % 2 ** 32 - 2 ** 31


class Channel:
    def __init__(self, name, kind=midi.CT_Sampler, target=0):
        self.default_name = name  # the sample's or plugin's, shown while the name is ""
        self.name = ""
        self.kind = kind
        self.color = SAMPLER_COLOR
        self.vol = 10000          # of VOL_STEPS
        self.pan = 0              # of PAN_STEPS
        self.pitch = 0            # cents
        self.pitch_range = PITCH_RANGE
        self.mute = False         # what isChannelMuted() reports...
        self.mute_real = False    # ...lags behind until FL refreshes
        self.target = target      # mixer track
        self.group = ""
        self.steps = {}           # pattern -> the steps that are on
        self.params = {}          # pattern -> {step: [parameters]} of steps that are on (defaults if missing)


class Pattern:
    def __init__(self):
        self.name = ""            # FL calls a pattern nobody named "Pattern <n>"
        self.color = PATTERN_COLOR


class FakeRack:
    def __init__(self, project, channels=4):
        self.project = project
        self.channels = [Channel(NAMES[i] if i < len(NAMES) else "Sampler #%d" % (i + 1), target=i + 1)
                         for i in range(channels)]
        self.patterns = {}        # number -> Pattern, once FL was asked about it
        self.current = 1
        self.group = None         # the group the channel rack shows; None shows all
        self.selected = {0}       # global indices

    def shown(self):
        """Global indices of the channels the group filter shows, in rack order."""
        return [i for i, channel in enumerate(self.channels)
                if self.group is None or channel.group == self.group]

    def channel(self, call, index, use_global):
        """The channel a call addresses, or None (recorded) if there is none."""
        order = range(len(self.channels)) if use_global else self.shown()
        if isinstance(index, int) and 0 <= index < len(order):
            return self.channels[order[index]]
        self.project.bad((call, index, "global" if use_global else "group"))
        return None

    def pattern(self, call, number):
        """Pattern `number`, or None (recorded) for a number FL doesn't have."""
        if isinstance(number, int) and 1 <= number <= PATTERN_MAX:
            return self.patterns.setdefault(number, Pattern())
        self.project.bad((call, number))
        return None

    def untouched(self, number):
        pattern = self.patterns.get(number)
        changed = pattern is not None and (pattern.name or pattern.color != PATTERN_COLOR)
        return not changed and not any(channel.steps.get(number) for channel in self.channels)

    def length(self, number):
        """Pattern `number`'s length in steps."""
        last = max((max(channel.steps.get(number) or {-1}) for channel in self.channels), default=-1)
        return max(BAR, (last // BAR + 1) * BAR)

    def step_params(self, channel, number, step):
        """The parameters of a step that is on (FL's defaults if nobody changed them)."""
        return channel.params.setdefault(number, {}).setdefault(step, list(STEP_DEFAULTS))

    def put_param(self, channel, number, step, param, value):
        """Store a step parameter as FL would: limited to its range, a change reported."""
        params = self.step_params(channel, number, step)
        value = min(STEP_MAX[param], max(0, value))
        if params[param] != value:
            params[param] = value
            self.report(midi.HW_Dirty_Patterns)

    def report(self, flag):
        self.project.refresh_flags.append(flag)

    def refresh(self):
        for channel in self.channels:
            channel.mute = channel.mute_real

    # --- what a user does that FL's API can't ----------------------------------

    def add_channel(self, index, name, kind=midi.CT_Sampler):
        """A new channel at global `index`: the ones after it move down."""
        self.channels.insert(index, Channel(name, kind))
        self.project.dirty_channels.append((index, midi.CE_New))

    def show_group(self, group):
        """Filter the channel rack to one group (None shows every channel)."""
        self.group = group
        self.report(midi.HW_Dirty_ChannelRackGroup)

    def edit_step(self, channel, step, param, value, quiet=False):
        """Change a parameter of a step in the current pattern, as in the graph
        editor (which only has steps that are on). quiet=True: FL doesn't report it."""
        holder = self.channels[channel]
        if step not in holder.steps.get(self.current, ()):
            raise AssertionError("steg %d är av: grafeditorn har bara steg som är på" % step)
        flags = len(self.project.refresh_flags)
        self.put_param(holder, self.current, step, param, value)
        if quiet:
            del self.project.refresh_flags[flags:]

    # --- for the tests -------------------------------------------------------------

    def row(self, channel, number=None):
        """A channel's steps in a pattern (the current one) as '0'/'1', up to the last step on."""
        on = self.channels[channel].steps.get(number or self.current, set())
        return "".join("1" if pos in on else "0" for pos in range(max(on, default=-1) + 1))

    def params(self, channel, number=None):
        """A channel's steps that are on in a pattern (the current one): {step: parameters}."""
        number = number or self.current
        holder = self.channels[channel]
        return {step: tuple(self.step_params(holder, number, step))
                for step in sorted(holder.steps.get(number, ()))}

    def snapshot(self):
        """Everything the sync covers, to compare two FLs."""
        return ([(c.name or c.default_name, c.color, c.vol, c.pan, c.pitch, c.mute_real, c.target,
                  sorted((n, sorted(on), [tuple(self.step_params(c, n, step)) for step in sorted(on)])
                         for n, on in c.steps.items() if on)) for c in self.channels],
                sorted((n, p.name or "Pattern %d" % n, p.color) for n, p in self.patterns.items()
                       if not self.untouched(n)))


def install(project):
    rack = FakeRack(project, project.options.get("channels", 4))
    project.rack = rack
    project.refreshers.append(rack.refresh)
    project.module("midi").__dict__.update(MIDI)
    _install_channels(project.module("channels"), rack)
    _install_patterns(project.module("patterns"), rack)


def _install_channels(c, rack):
    project = rack.project

    def put(channel, attr, value, flag):
        """Change a value the way FL does: only a real change is reported."""
        if getattr(channel, attr) != value:
            setattr(channel, attr, value)
            rack.report(flag)

    c.channelCount = lambda globalCount=False: len(rack.channels) if globalCount else len(rack.shown())

    def get_index(index):
        shown = rack.shown()
        if isinstance(index, int) and 0 <= index < len(shown):
            return shown[index]
        project.bad(("getChannelIndex", index))
        return 0
    c.getChannelIndex = get_index

    def selected(canBeNone=False, offset=0, indexGlobal=False):
        shown = rack.shown()
        picked = [pos for pos, index in enumerate(shown) if index in rack.selected]
        if not picked:
            return -1 if canBeNone else 0
        pos = picked[min(offset, len(picked) - 1)]
        return shown[pos] if indexGlobal else pos
    c.selectedChannel = selected

    def get_type(index, useGlobalIndex=False):
        channel = rack.channel("getChannelType", index, useGlobalIndex)
        return channel.kind if channel is not None else 0
    c.getChannelType = get_type

    def get_volume(index, useDb=0, useGlobalIndex=False):
        if useDb:
            raise NotImplementedError("the fake has no dB")
        channel = rack.channel("getChannelVolume", index, useGlobalIndex)
        return channel.vol / VOL_STEPS if channel is not None else 0.0

    def set_volume(index, volume, pickupMode=midi.PIM_None, useGlobalIndex=False):
        channel = rack.channel("setChannelVolume", index, useGlobalIndex)
        if channel is not None:
            put(channel, "vol", int(round(min(1.0, max(0.0, volume)) * VOL_STEPS)), midi.HW_ChannelEvent)
    c.getChannelVolume, c.setChannelVolume = get_volume, set_volume

    def get_pan(index, useGlobalIndex=False):
        channel = rack.channel("getChannelPan", index, useGlobalIndex)
        return channel.pan / PAN_STEPS if channel is not None else 0.0

    def set_pan(index, pan, pickupMode=midi.PIM_None, useGlobalIndex=False):
        channel = rack.channel("setChannelPan", index, useGlobalIndex)
        if channel is not None:
            put(channel, "pan", int(round(min(1.0, max(-1.0, pan)) * PAN_STEPS)), midi.HW_ChannelEvent)
    c.getChannelPan, c.setChannelPan = get_pan, set_pan

    def get_pitch(index, mode=0, useGlobalIndex=False):
        channel = rack.channel("getChannelPitch", index, useGlobalIndex)
        if channel is None:
            return 0.0
        if mode == 1:
            return channel.pitch
        if mode == 2:
            return channel.pitch_range
        return channel.pitch / (channel.pitch_range * 100)

    def set_pitch(index, value, pitchUnit=0, pickupMode=midi.PIM_None, useGlobalIndex=False):
        channel = rack.channel("setChannelPitch", index, useGlobalIndex)
        if channel is None:
            return
        if pitchUnit == 2:
            project.bad(("setChannelPitch", "mode 2 puts FL's pitch range out of step with its UI"))
            return
        reach = channel.pitch_range * 100
        cents = value * reach if pitchUnit == 0 else value
        put(channel, "pitch", int(round(min(reach, max(-reach, cents)))), midi.HW_ChannelEvent)
    c.getChannelPitch, c.setChannelPitch = get_pitch, set_pitch

    def is_muted(index, useGlobalIndex=False):
        channel = rack.channel("isChannelMuted", index, useGlobalIndex)
        return int(channel.mute) if channel is not None else 0

    def mute(index, value=-1, useGlobalIndex=False):
        channel = rack.channel("muteChannel", index, useGlobalIndex)
        if channel is not None:
            muted = (not channel.mute_real) if value == -1 else bool(value)
            put(channel, "mute_real", muted, midi.HW_ChannelEvent)
    c.isChannelMuted, c.muteChannel = is_muted, mute

    def get_name(index, useGlobalIndex=False):
        channel = rack.channel("getChannelName", index, useGlobalIndex)
        return (channel.name or channel.default_name) if channel is not None else ""

    def set_name(index, name, useGlobalIndex=False):
        channel = rack.channel("setChannelName", index, useGlobalIndex)
        if channel is not None and channel.name != name:
            channel.name = name  # "" goes back to the sample's or plugin's name
            project.dirty_channels.append((rack.channels.index(channel), midi.CE_Rename))
    c.getChannelName, c.setChannelName = get_name, set_name

    def get_color(index, useGlobalIndex=False):
        channel = rack.channel("getChannelColor", index, useGlobalIndex)
        return channel.color if channel is not None else 0

    def set_color(index, color, useGlobalIndex=False):
        channel = rack.channel("setChannelColor", index, useGlobalIndex)
        if channel is not None:
            put(channel, "color", int32(color), midi.HW_Dirty_Colors)
    c.getChannelColor, c.setChannelColor = get_color, set_color

    def get_target(index, useGlobalIndex=False):
        channel = rack.channel("getTargetFxTrack", index, useGlobalIndex)
        return channel.target if channel is not None else 0

    def set_target(channelIndex, mixerIndex, useGlobalIndex=False):
        channel = rack.channel("setTargetFxTrack", channelIndex, useGlobalIndex)
        tracks = len(project.mixer.tracks) if hasattr(project, "mixer") else 126
        if not (isinstance(mixerIndex, int) and 0 <= mixerIndex < tracks):
            project.bad(("setTargetFxTrack", "mixer track", mixerIndex))
        elif channel is not None:
            put(channel, "target", mixerIndex, midi.HW_ChannelEvent)
    c.getTargetFxTrack, c.setTargetFxTrack = get_target, set_target

    def grid(call, index, position, use_global):
        """The channel a grid call addresses, or None (recorded) if it or the step doesn't exist."""
        channel = rack.channel(call, index, use_global)
        if channel is not None and not (isinstance(position, int) and position >= 0):
            project.bad((call, "step", position))
            return None
        return channel

    def get_bit(index, position, useGlobalIndex=False):
        channel = grid("getGridBit", index, position, useGlobalIndex)
        return int(channel is not None and position in channel.steps.get(rack.current, ()))

    def set_bit(index, position, value, useGlobalIndex=False):
        channel = grid("setGridBit", index, position, useGlobalIndex)
        if channel is not None:
            on = channel.steps.setdefault(rack.current, set())
            if bool(value) != (position in on):
                (on.add if value else on.discard)(position)
                # A step switched on gets FL's default parameters, one switched off forgets its own.
                params = channel.params.setdefault(rack.current, {})
                if value:
                    params[position] = list(STEP_DEFAULTS)
                else:
                    params.pop(position, None)
                rack.report(midi.HW_Dirty_Patterns)
    c.getGridBit, c.setGridBit = get_bit, set_bit

    def param_call(call, index, step, param, use_global):
        """The channel a step parameter call addresses, or None (recorded) if it,
        the step or the parameter doesn't exist."""
        channel = grid(call, index, step, use_global)
        if channel is not None and not (type(param) is int and 0 <= param < len(STEP_DEFAULTS)):
            project.bad((call, "parameter", param))
            return None
        return channel

    def get_step_param(index, step, param, useGlobalIndex=False):
        channel = param_call("getCurrentStepParam", index, step, param, useGlobalIndex)
        if channel is None:
            return 0
        if step not in channel.steps.get(rack.current, ()):
            project.bad(("getCurrentStepParam", "step that is off", step))  # FL keeps no parameters for it
            return 0
        return rack.step_params(channel, rack.current, step)[param]

    def set_step_param(index, patNum, step, param, value, useGlobalIndex=False):
        channel = param_call("setStepParameterByIndex", index, step, param, useGlobalIndex)
        if channel is None or rack.pattern("setStepParameterByIndex", patNum) is None:
            return
        if type(value) is not int:
            project.bad(("setStepParameterByIndex", "value", value))
        elif step in channel.steps.get(patNum, ()):  # FL keeps parameters only for steps that are on
            rack.put_param(channel, patNum, step, param, value)
    c.getCurrentStepParam, c.setStepParameterByIndex = get_step_param, set_step_param


def _install_patterns(p, rack):
    p.patternNumber = lambda: rack.current
    p.patternMax = lambda: PATTERN_MAX

    def count():
        numbers = set(rack.patterns)
        for channel in rack.channels:
            numbers.update(number for number, on in channel.steps.items() if on)
        return sum(not rack.untouched(number) for number in numbers)
    p.patternCount = count

    def is_default(index):
        return rack.untouched(index) if rack.pattern("isPatternDefault", index) is not None else True
    p.isPatternDefault = is_default

    def get_name(index):
        pattern = rack.pattern("getPatternName", index)
        return (pattern.name or "Pattern %d" % index) if pattern is not None else ""

    def set_name(index, name):
        pattern = rack.pattern("setPatternName", index)
        if pattern is not None and pattern.name != name:
            pattern.name = name  # "" goes back to "Pattern <n>"
            rack.report(midi.HW_Dirty_Patterns)
    p.getPatternName, p.setPatternName = get_name, set_name

    def get_color(index):
        pattern = rack.pattern("getPatternColor", index)
        return pattern.color if pattern is not None else 0

    def set_color(index, color):
        pattern = rack.pattern("setPatternColor", index)
        if pattern is not None and pattern.color != int32(color):
            pattern.color = int32(color)
            rack.report(midi.HW_Dirty_Patterns)
    p.getPatternColor, p.setPatternColor = get_color, set_color

    def get_length(index):
        return rack.length(index) if rack.pattern("getPatternLength", index) is not None else 0
    p.getPatternLength = get_length

    def jump(index):
        if rack.pattern("jumpToPattern", index) is not None and rack.current != index:
            rack.current = index
            rack.report(midi.HW_Dirty_Patterns)
    p.jumpToPattern = jump
