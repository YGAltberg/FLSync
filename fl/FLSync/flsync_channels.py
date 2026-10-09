"""The channel rack: channel settings, the step sequencer and patterns (keys
c/, g/, gp/, pt/).

Channels are addressed by their global index. FL's channel functions count
within the group the channel rack is filtered to unless useGlobalIndex is set
(API 33+), so every call here sets it and channelCount(1) counts them all: a
user picking a group must not shift the keys. The flag is passed by position,
as FL's own scripts do; FLEngine's argument lists confirm its place in every
function used here (channelCount has no keywords at all).

Steps: FL's grid functions only see the current pattern, so the grid's units
are the current pattern's rows. Rows others change in another pattern wait
here until the user switches to it. A row is sent without its trailing '0's,
so a pattern growing longer changes no row.

Step parameters (pitch, velocity, ... of every step that is on) are read the
same way. FL keeps them only for steps that are on, so parameters others set
for a step that is off here wait until a row from them switches it on (the
client sends a row before its parameters, see bindings()). They can be
written into any pattern, but FL only says which steps are on in the current
one: in another pattern the steps that were on when it was last current get
theirs at once, and all of them are checked when the user switches to it.

Keys are numbers, so adding, removing or moving a channel gives the ones after
it other numbers. Their values are then taken as they are instead of being
sent, which would rename and re-route the other FLs' channels. The same goes
for the channels' mixer tracks when an insert is added or deleted (FL 2025+):
a channel sent to an insert after it follows that insert to its new number.
(FL reports no such thing for patterns: their names and colors simply follow
the numbers.)
"""

import re

import channels
import general
import midi
import patterns

from flsync_binding import Binding
from flsync_log import log
from flsync_mixer import tracks as mixer_tracks

GLOBAL = 1          # useGlobalIndex, passed to every channel function
MIN_API = 33        # the API version that added useGlobalIndex
CENTS = 1           # getChannelPitch/setChannelPitch mode: cents (0 is a share of the pitch range)
MAX_CENTS = 4800    # FL limits the pitch to the channel's own range anyway
MIN_STEPS = 16      # FL shows a whole bar even of an empty pattern
MAX_STEPS = 512     # steps of a row that are synced: reading one costs an API call per step
PARAMS = 8          # parameters of a step, midi.pPitch (0) .. midi.pShift (7): pitch, velocity,
                    # release, fine pitch, pan, mod X, mod Y, shift
MAX_VALUE = 4096    # the longest string the relay takes
GRAPH_EDITOR = getattr(midi, "HW_Dirty_GraphEditor", 0x80000)  # OnRefresh flag of FLs newer than 2025.2.4
PATTERN_NAME = "Pattern %d"  # what FL calls a pattern nobody named

CHANNEL_PROPS = {"v": "volym", "p": "pan", "pi": "tonhöjd", "mu": "mute", "n": "namn",
                 "c": "färg", "t": "mixerspår"}
PATTERN_PROPS = {"n": "namn", "c": "färg"}


def bindings():
    try:
        api = general.getVersion()
    except Exception:
        api = None
    if not isinstance(api, int) or api < MIN_API:
        log("kanalracket synkas inte: FL:s script-API är version %s, det behövs %d eller senare"
            % (api, MIN_API))
        return []
    rows = StepRows()
    # The grid before its step parameters: the client sends a tick's changes in
    # this order, and a receiver can only give parameters to steps that are on.
    return [ChannelBinding(), GridBinding(rows), StepParamsBinding(rows), PatternBinding()]


# --- keys and values -------------------------------------------------------------

def _index(text):
    """'12' -> 12; None for anything else (signs, spaces, leading zeros)."""
    return int(text) if text.isdecimal() and str(int(text)) == text else None


def _split(key, prefix):
    """'c/3/v' -> (3, 'v') for prefix 'c'; None if the key doesn't fit."""
    parts = key.split("/") if isinstance(key, str) else ()
    if len(parts) != 3 or parts[0] != prefix:
        return None
    index = _index(parts[1])
    return None if index is None else (index, parts[2])


def _row_key(key, prefix):
    """'g/2/5' -> (pattern 2, channel 5) for prefix 'g'; None if the key doesn't fit."""
    parsed = _split(key, prefix)
    channel = _index(parsed[1]) if parsed else None
    return None if channel is None else (parsed[0], channel)


def _number(value, low, high):
    """`value` limited to low..high, or None if it isn't a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # NaN
        return None
    return float(min(high, max(low, value)))


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _color(value):
    """FL colors are signed 32-bit ints (0xAARRGGBB, so often negative)."""
    return _integer(value) and -2 ** 31 <= value < 2 ** 31


def _is_row(value):
    return isinstance(value, str) and not value.strip("01")


_PARAM = re.compile(r"-?[0-9]{1,9}\Z")  # fits FL's 32-bit ints


def _entry(step, params):
    """One step of a gp value: '4:60,100,64,120,64,128,128,0'."""
    return "%d:%s" % (step, ",".join("%d" % value for value in params))


def _join(entries):
    """Entries joined with ';', as many as fit into a value the relay takes.
    A row with more than some 130 steps on syncs the parameters of its first
    ones only (and only those are read): rows that long are rare."""
    taken, size = [], -1
    for entry in entries:
        size += len(entry) + 1
        if size > MAX_VALUE:
            break
        taken.append(entry)
    return ";".join(taken)


def _step_params(value):
    """A gp value as {step: (pitch, velocity, ...)}; None if it isn't one."""
    if not isinstance(value, str):
        return None
    found = {}
    for entry in value.split(";") if value else ():
        step, _, params = entry.partition(":")
        step, params = _index(step), params.split(",")
        if (step is None or step >= MAX_STEPS or step in found or len(params) != PARAMS
                or not all(_PARAM.match(param) for param in params)):
            return None
        found[step] = tuple(int(param) for param in params)
    return found


# --- channels ----------------------------------------------------------------------

def channel_count():
    return channels.channelCount(GLOBAL)


def channel_name(channel):
    """The channel's name, or '' if this FL has no such channel."""
    return channels.getChannelName(channel, GLOBAL) if channel < channel_count() else ""


def channel_label(channel):
    name = channel_name(channel)
    return "Kanal %d (%s)" % (channel + 1, name) if name else "Kanal %d" % (channel + 1)


def _reported(index):
    """The channels an OnDirtyChannel index can mean. FL doesn't say whether it
    counts within the channel rack's group filter, so take both."""
    found = {index} if index < channel_count() else set()
    if index < channels.channelCount():
        found.add(channels.getChannelIndex(index))
    return found


def _restructures(index, flag):
    """OnDirtyChannel: channels added, removed or replaced, or all changed at
    once (-1). A moved channel presumably shows up as one of these (unverified)."""
    return index < 0 or flag in (midi.CE_New, midi.CE_Delete, midi.CE_Replace)


class ChannelBinding(Binding):
    """Volume, pan, pitch (cents), mute, name, color and mixer track of every channel."""

    prefix = "c"
    sweep_budget = 4

    def __init__(self):
        self.on_project_load()

    def on_project_load(self):
        self.count = channel_count()          # what the last tick saw
        self.inserts = len(mixer_tracks())    # ...and how many mixer tracks there were
        self.restructured = False

    def units(self):
        return range(channel_count())

    def read(self, channel):
        yield "c/%d/v" % channel, round(channels.getChannelVolume(channel, 0, GLOBAL), 6)
        yield "c/%d/p" % channel, round(channels.getChannelPan(channel, GLOBAL), 6)
        yield "c/%d/pi" % channel, round(channels.getChannelPitch(channel, CENTS, GLOBAL))
        yield "c/%d/mu" % channel, bool(channels.isChannelMuted(channel, GLOBAL))
        yield "c/%d/n" % channel, channels.getChannelName(channel, GLOBAL)
        yield "c/%d/c" % channel, channels.getChannelColor(channel, GLOBAL)
        yield "c/%d/t" % channel, channels.getTargetFxTrack(channel, GLOBAL)

    def write(self, key, value):
        parsed = _split(key, "c")
        if parsed is None or parsed[0] >= channel_count():
            return None
        try:
            return self._set(parsed[0], parsed[1], value)
        except (TypeError, ValueError):  # e.g. FL's "Operation unsafe at current time"
            return None

    def _set(self, channel, prop, value):
        if prop == "v":
            level = _number(value, 0.0, 1.0)
            if level is not None:
                channels.setChannelVolume(channel, level, midi.PIM_None, GLOBAL)
                return round(channels.getChannelVolume(channel, 0, GLOBAL), 6)
        elif prop == "p":
            level = _number(value, -1.0, 1.0)
            if level is not None:
                channels.setChannelPan(channel, level, midi.PIM_None, GLOBAL)
                return round(channels.getChannelPan(channel, GLOBAL), 6)
        elif prop == "pi":
            cents = _number(value, -MAX_CENTS, MAX_CENTS)
            if cents is not None:
                # In cents, not as a share of the range: FLs whose channels have
                # different pitch ranges still get the same pitch where they can.
                channels.setChannelPitch(channel, round(cents), CENTS, midi.PIM_None, GLOBAL)
                return round(channels.getChannelPitch(channel, CENTS, GLOBAL))
        elif prop == "mu":
            if isinstance(value, (bool, int, float)):
                channels.muteChannel(channel, 1 if value else 0, GLOBAL)  # sets; -1 would toggle
                # Like the mixer's, the mute FL reports may lag: trust what we asked for.
                return bool(value)
        elif prop == "n":
            if isinstance(value, str):
                channels.setChannelName(channel, value, GLOBAL)
                return channels.getChannelName(channel, GLOBAL)
        elif prop == "c":
            if _color(value):
                channels.setChannelColor(channel, value, GLOBAL)
                return channels.getChannelColor(channel, GLOBAL)
        elif prop == "t":
            if _integer(value) and value in mixer_tracks():
                channels.setTargetFxTrack(channel, value, GLOBAL)
                return channels.getTargetFxTrack(channel, GLOBAL)
        return None

    def describe(self, key):
        parsed = _split(key, "c")
        if parsed is None or parsed[1] not in CHANNEL_PROPS:
            return key
        return "%s %s" % (channel_label(parsed[0]), CHANNEL_PROPS[parsed[1]])

    def on_dirty_channel(self, index, flag):
        if _restructures(index, flag):
            self.restructured = True
            return ()
        return () if flag == midi.CE_Select else _reported(index)

    def on_refresh(self, flags):
        # FL doesn't say which channel changed: the selected one is the best guess.
        if flags & (midi.HW_ChannelEvent | midi.HW_Dirty_ControlValues
                    | midi.HW_Dirty_Names | midi.HW_Dirty_Colors):
            selected = channels.selectedChannel(1, 0, GLOBAL)
            return [selected] if selected >= 0 else ()
        return ()

    def tick(self, now):
        count, inserts = channel_count(), len(mixer_tracks())
        if not self.restructured and count == self.count and inserts == self.inserts:
            return (), ()
        self.count, self.inserts, self.restructured = count, inserts, False
        return (), range(count)


# --- the step sequencer ----------------------------------------------------------------

def _steps(pattern):
    """How many steps `pattern` has. getPatternLength counts steps (16 for an
    empty one-bar pattern in FL 2025.2.4), although FL's docs say beats."""
    return min(MAX_STEPS, max(MIN_STEPS, patterns.getPatternLength(pattern)))


def _has_steps(channel):
    """Audio and automation clip channels have no row in the step sequencer."""
    return channels.getChannelType(channel, GLOBAL) not in (midi.CT_AudioClip, midi.CT_AutoClip)


def _row(channel, steps):
    """The channel's steps in the current pattern as '0'/'1', without the trailing '0's."""
    return "".join("1" if channels.getGridBit(channel, pos, GLOBAL) else "0"
                   for pos in range(steps)).rstrip("0")


def _put_row(channel, row, steps):
    """Make the channel's steps in the current pattern match `row`, touching only
    the steps that differ. Past the pattern's end all steps are off already;
    turning one on there makes the pattern longer, as clicking it would.
    Returns whether a step changed."""
    changed = False
    for pos in range(max(len(row), steps)):
        on = pos < len(row) and row[pos] == "1"
        if (on or pos < steps) and bool(channels.getGridBit(channel, pos, GLOBAL)) != on:
            channels.setGridBit(channel, pos, 1 if on else 0, GLOBAL)
            changed = True
    return changed


def _on_steps(channel, pattern):
    """The steps of the channel's row that are on in the current pattern."""
    return [step for step in range(_steps(pattern)) if channels.getGridBit(channel, step, GLOBAL)]


def _params(channel, on):
    """The gp value of the channel's steps `on` in the current pattern."""
    return _join(_entry(step, [channels.getCurrentStepParam(channel, step, param, GLOBAL)
                               for param in range(PARAMS)]) for step in on)


def _put_params(channel, pattern, wanted):
    """Give the steps of `wanted` ({step: params}) that are on in the current
    pattern their parameters, touching only those that differ. Returns the
    ones that are off: FL keeps no parameters for them."""
    steps, off = _steps(pattern), {}
    for step, params in sorted(wanted.items()):
        if step >= steps or not channels.getGridBit(channel, step, GLOBAL):
            off[step] = params
            continue
        for param, value in enumerate(params):
            if channels.getCurrentStepParam(channel, step, param, GLOBAL) != value:
                channels.setStepParameterByIndex(channel, pattern, step, param, value, GLOBAL)
    return off


def _put_params_elsewhere(channel, pattern, wanted, row):
    """setStepParameterByIndex reaches any pattern, but FL only says which steps
    are on in the current one: in `pattern` the steps on in `row` (as it was
    last seen) get their parameters."""
    for step, params in sorted(wanted.items()):
        if step < len(row) and row[step] == "1":
            for param, value in enumerate(params):
                channels.setStepParameterByIndex(channel, pattern, step, param, value, GLOBAL)


class StepRows:
    """What GridBinding tells StepParamsBinding (bindings() gives both the same
    one): the rows FL has as last read or put in, also those of patterns that
    aren't current, and the rows it put in since StepParamsBinding's last tick.
    Steps such a row switched on have FL's default parameters."""

    def __init__(self):
        self.seen = {}    # (pattern, channel) -> row
        self.put = set()  # (pattern, channel)

    def forget(self):
        self.seen.clear()
        self.put.clear()

    def take(self):
        put, self.put = self.put, set()
        return put


class _PatternRows(Binding):
    """The step sequencer's bindings. Their units are the current pattern's
    rows, (pattern, channel): FL's step functions only see the current pattern.
    Another pattern or other channel numbers make the rows the new baseline.
    A row costs FL calls per step, so tick() sweeps by a budget of calls, and
    faster while looking for a change FL reported without saying where."""

    sweep_budget = 0      # the client's sweep counts units: tick() sweeps instead
    sweep_calls = 64      # FL calls per tick for re-reading rows, in case FL didn't report a change
    burst_calls = 256     # ...and while looking for a change FL reported without saying where
    dirty_flags = midi.HW_Dirty_Patterns  # the OnRefresh flags that start such a look

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else StepRows()
        self.next_row = 0  # where the sweep goes on
        self.on_project_load()

    def on_project_load(self):
        self.pattern = patterns.patternNumber()  # what the last tick saw
        self.count = channel_count()
        self.restructured = False
        self.burst = 0     # rows left to re-read at the burst rate
        self.forget()

    def forget(self):
        """Another project is open or the channels have other numbers: drop what
        is kept for rows."""

    def units(self):
        pattern = patterns.patternNumber()
        return [(pattern, channel) for channel in range(channel_count())]

    def on_dirty_channel(self, index, flag):
        if _restructures(index, flag):
            self.restructured = True
        return ()

    def on_refresh(self, flags):
        if flags & self.dirty_flags:
            self.burst = self.count  # FL doesn't say which row: look at all of them, quickly
        return ()

    def tick(self, now):
        pattern, count = patterns.patternNumber(), channel_count()
        rebaseline = []
        moved = self.restructured or count != self.count
        if moved:
            # The channels have other numbers now: what waits for rows of other
            # patterns would land on the wrong channels.
            self.forget()
        if moved or pattern != self.pattern:
            # Another pattern or other channels: take the rows as they are, unsent.
            rebaseline = [(pattern, channel) for channel in range(count)]
            self.pattern, self.count, self.restructured = pattern, count, False
        done = self.catch_up(pattern, count, bool(rebaseline))
        return self._sweep(pattern, count), rebaseline or done

    def catch_up(self, pattern, count, switched):
        """Put in what waits for the current pattern; returns the rows to take as
        they are afterwards."""
        return []

    def cost(self, channel, steps):
        """About how many FL calls reading the channel's row takes."""
        return steps

    def _sweep(self, pattern, count):
        """The rows to re-read this tick: as many as the budget allows, at least one."""
        if not count:
            return ()
        steps = _steps(pattern)
        budget = self.burst_calls if self.burst else self.sweep_calls
        todo = []
        while len(todo) < count:
            self.next_row %= count
            cost = self.cost(self.next_row, steps)
            if todo and cost > budget:
                break
            budget -= cost
            todo.append((pattern, self.next_row))
            self.next_row += 1
        self.burst = max(0, self.burst - len(todo))
        return todo


class GridBinding(_PatternRows):
    """The step sequencer: g/<pattern>/<channel> is the channel's row of steps."""

    prefix = "g"

    def forget(self):
        # Rows waiting for the old project's patterns, or for the channels' old
        # numbers, must not end up in FL.
        self.waiting = {}  # pattern -> {channel: row} others changed while it wasn't current
        self.rows.forget()

    def read(self, unit):
        pattern, channel = unit
        # FL only shows the current pattern's steps
        if pattern == patterns.patternNumber() and _has_steps(channel):
            row = _row(channel, _steps(pattern))
            self.rows.seen[unit] = row
            yield "g/%d/%d" % unit, row

    def write(self, key, value):
        parsed = _row_key(key, "g")
        if parsed is None or not _is_row(value):
            return None
        pattern, channel = parsed
        if (not 1 <= pattern <= patterns.patternMax() or channel >= channel_count()
                or not _has_steps(channel)):
            return None
        row = value[:MAX_STEPS].rstrip("0")
        if pattern != patterns.patternNumber():
            self.waiting.setdefault(pattern, {})[channel] = row  # put in when the user switches to it
            return row
        self.waiting.get(pattern, {}).pop(channel, None)
        try:
            return self._put(parsed, row)
        except (TypeError, ValueError):
            return None

    def _put(self, unit, row):
        """Make row `unit` of the current pattern `row`; returns what FL has then."""
        pattern, channel = unit
        changed = True  # unless FL took all of it: it may refuse part way
        try:
            changed = _put_row(channel, row, _steps(pattern))
        finally:
            if changed:
                self.rows.put.add(unit)  # its new steps need their parameters
        actual = _row(channel, _steps(pattern))  # the pattern may have grown
        self.rows.seen[unit] = actual
        return actual

    def describe(self, key):
        parsed = _row_key(key, "g")
        if parsed is None:
            return key
        pattern, channel = parsed
        return "%s steg i mönster %d" % (channel_name(channel) or "Kanal %d" % (channel + 1), pattern)

    def catch_up(self, pattern, count, switched):
        """Write the rows others changed in this pattern while it wasn't current."""
        if pattern not in self.waiting or not general.safeToEdit():
            return []
        done = []
        for channel, row in self.waiting.pop(pattern).items():
            if channel < count:
                try:
                    self._put((pattern, channel), row)
                except (TypeError, ValueError):  # FL refused; take what it has
                    pass
                done.append((pattern, channel))
        return done


class StepParamsBinding(_PatternRows):
    """The parameters of the steps that are on: gp/<pattern>/<channel> lists
    `<step>:<pitch>,<velocity>,<release>,<fine pitch>,<pan>,<mod x>,<mod y>,<shift>`
    for each of them, in FL's units, joined with ';'."""

    prefix = "gp"
    sweep_calls = 64 + 16 * PARAMS  # a row costs a call per step and 8 more per step that is on
    burst_calls = 4 * sweep_calls
    dirty_flags = midi.HW_Dirty_Patterns | GRAPH_EDITOR

    def forget(self):
        self.waiting = {}     # (pattern, channel) -> {step: params} from others that FL doesn't have yet
        self.reapply = set()  # rows of the current pattern whose waiting parameters go in next tick
        self.on = {}          # channel -> steps on at its last read, for the sweep's budget

    def cost(self, channel, steps):
        return steps + PARAMS * self.on.get(channel, 0)

    def read(self, unit):
        pattern, channel = unit
        if pattern != patterns.patternNumber() or not _has_steps(channel):
            return
        on = _on_steps(channel, pattern)
        self.on[channel] = len(on)
        waiting = self.waiting.get(unit)
        if waiting and unit not in self.reapply:
            # Steps that came on without a row from others: the user's, with the
            # parameters they have here.
            for step in on:
                waiting.pop(step, None)
            if not waiting:
                del self.waiting[unit]
        yield "gp/%d/%d" % unit, _params(channel, on)

    def write(self, key, value):
        parsed, wanted = _row_key(key, "gp"), _step_params(value)
        if parsed is None or wanted is None:
            return None
        pattern, channel = parsed
        if (not 1 <= pattern <= patterns.patternMax() or channel >= channel_count()
                or not _has_steps(channel)):
            return None
        self._wait(parsed, wanted)  # all of it, should FL refuse part way
        try:
            if pattern == patterns.patternNumber():
                self._wait(parsed, _put_params(channel, pattern, wanted))
                return _params(channel, _on_steps(channel, pattern))
            row = self.rows.seen.get(parsed)
            if row:
                _put_params_elsewhere(channel, pattern, wanted, row)
        except (TypeError, ValueError):
            return None
        # All of it waits: checked against FL's steps when the user switches to the pattern.
        return _join(_entry(step, wanted[step]) for step in sorted(wanted))

    def _wait(self, unit, params):
        if params:
            self.waiting[unit] = params
        else:
            self.waiting.pop(unit, None)

    def describe(self, key):
        parsed = _row_key(key, "gp")
        if parsed is None:
            return key
        pattern, channel = parsed
        # Parameters that arrive with their row are part of a step edit: say so,
        # as the row's own hint would (the client shows only the last one).
        what = "steg" if parsed in self.rows.put else "stegparametrar"
        return "%s %s i mönster %d" % (channel_name(channel) or "Kanal %d" % (channel + 1), what, pattern)

    def shared_at_start(self, key, value):
        return value != ""  # no steps on: the row says so already, and the link is slow

    def catch_up(self, pattern, count, switched):
        """Give waiting parameters to the steps that are on now: those others set
        in this pattern while it wasn't current, and those of rows GridBinding
        put in (their new steps have FL's defaults)."""
        if switched:
            self.reapply = {unit for unit in self.waiting if unit[0] == pattern}
        self.reapply.update(unit for unit in self.rows.take() if unit[0] == pattern)
        done = sorted(unit for unit in self.reapply if unit[1] < count)
        if done and general.safeToEdit():
            for unit in done:
                wanted = self.waiting.get(unit)
                if wanted:
                    try:
                        self._wait(unit, _put_params(unit[1], pattern, wanted))
                    except (TypeError, ValueError):  # FL refused; they wait for the next row
                        pass
            self.reapply.clear()
        # Taken as they are, put in or not yet: what a row changed isn't the user's doing.
        return done


# --- patterns --------------------------------------------------------------------------

def pattern_label(pattern):
    name = patterns.getPatternName(pattern) if 1 <= pattern <= patterns.patternMax() else ""
    if name and name != PATTERN_NAME % pattern:
        return "Mönster %d (%s)" % (pattern, name)
    return "Mönster %d" % pattern


def _highest_pattern(count):
    """The number of the highest pattern that isn't untouched. patternCount()
    says how many such patterns there are, but they needn't be 1..count."""
    number = found = 0
    while found < count and number < patterns.patternMax():
        number += 1
        found += not patterns.isPatternDefault(number)
    return number


class PatternBinding(Binding):
    """Names and colors of the patterns (numbered from 1, as in FL)."""

    prefix = "pt"
    sweep_budget = 4

    def __init__(self):
        self.on_project_load()

    def on_project_load(self):
        self.count = patterns.patternCount()
        self.highest = _highest_pattern(self.count)
        self.written = 0        # the highest pattern someone else wrote to: it exists here now too
        self.top = self._top()  # the highest pattern the last tick looked at

    def _top(self):
        count = patterns.patternCount()
        if count != self.count:  # a pattern got or lost its first change: look again
            self.count, self.highest = count, _highest_pattern(count)
        return max(self.highest, patterns.patternNumber(), self.written)

    def units(self):
        return range(1, self._top() + 1)

    def read(self, pattern):
        yield "pt/%d/n" % pattern, patterns.getPatternName(pattern)
        yield "pt/%d/c" % pattern, patterns.getPatternColor(pattern)

    def write(self, key, value):
        parsed = _split(key, "pt")
        if parsed is None or not 1 <= parsed[0] <= patterns.patternMax():
            return None
        pattern, prop = parsed
        try:
            if prop == "n" and isinstance(value, str):
                # FL's own name stays unset ("" resets it), so it goes on following the number.
                patterns.setPatternName(pattern, "" if value == PATTERN_NAME % pattern else value)
                actual = patterns.getPatternName(pattern)
            elif prop == "c" and _color(value):
                patterns.setPatternColor(pattern, value)
                actual = patterns.getPatternColor(pattern)
            else:
                return None
        except (TypeError, ValueError):
            return None
        self.written = max(self.written, pattern)
        return actual

    def describe(self, key):
        parsed = _split(key, "pt")
        if parsed is None or parsed[1] not in PATTERN_PROPS:
            return key
        return "%s %s" % (pattern_label(parsed[0]), PATTERN_PROPS[parsed[1]])

    def on_refresh(self, flags):
        if flags & (midi.HW_Dirty_Patterns | midi.HW_Dirty_Names | midi.HW_Dirty_Colors):
            return [patterns.patternNumber()]
        return ()

    def tick(self, now):
        # Patterns not looked at before (the user went to a new one, say): take
        # them as they are rather than send FL's defaults for them.
        top, self.top = self.top, self._top()
        return (), range(top + 1, self.top + 1)
