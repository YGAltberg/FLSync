"""Plugin parameters of the plugin whose window has focus (keys pc/ and pm/).

pc/<channel>/<plugin>/<param> is a parameter of a channel's generator (global
channel index), pm/<track>/<slot>/<plugin>/<param> one of a mixer effect.
Values are 0..1; <plugin> is the plugin's own name with "/" replaced by "|",
<param> the parameter's index.

Only the focused plugin is read: it is the one the user is tweaking, it keeps
the work per tick small (a VST reports 4240 parameter slots), and automation
moving other plugins isn't broadcast. When a plugin gets focus its values
become the baseline, so nothing is sent for what changed while nobody watched.
Its real parameters (the named ones below a VST's MIDI slots) are listed a few
names per tick and remembered for the next time it gets focus. Units are chunks
of them: they are swept, and chunks that just moved are re-read every tick.

Others' values go into the plugin at their place whether it has focus or not,
but only if it is the plugin the key names: someone else may have another
plugin there. Plugin parameters are never starting values, they are far too
many for the MIDI link.
"""

import math

import channels
import general
import midi
import mixer
import plugins
import ui

from flsync_binding import Binding
from flsync_log import log
from flsync_mixer import tracks

MIN_API = 26          # the API version that added useGlobalIndex to the plugins functions
SLOTS = 10            # effect slots per mixer track
VST_PARAMS = 4096     # a VST reports 4240 slots: 4096 parameters, then 128 MIDI CCs and 16 aftertouch
CHUNK = 16            # parameters per unit
NAME_BUDGET = 128     # parameter names read per tick while the focused plugin is listed
UNIT_BUDGET = 8       # units added per tick (each is read whole as the baseline the tick after)
REMEMBERED = 16       # plugins whose parameter lists are kept for when they get focus again
HOT_TIME = 2.0        # a chunk whose parameter moved is re-read every tick for this long...
MAX_HOT = 8           # ...but only this many chunks (a preset change moves them all)


def bindings():
    try:
        api = general.getVersion()
    except Exception:
        api = None
    if not isinstance(api, int) or api < MIN_API:
        log("plugin-parametrar synkas inte: FL:s script-API är version %s, det behövs %d eller senare"
            % (api, MIN_API))
        return []
    return [GeneratorParams(), EffectParams()]


# --- the plugins functions. A place is (channel, -1) for a generator, with the
# channel's global index, or (track, slot) for a mixer effect. The last argument
# is useGlobalIndex, passed by position: the form measured in FL 2025.2.4. ------

def _valid(place):
    index, slot = place
    return plugins.isValid(index, slot, slot < 0)


def _plugin_name(place, user=False):
    index, slot = place
    return plugins.getPluginName(index, slot, user, slot < 0)


def _slots(place):
    """The plugin's parameter slots, without a VST's MIDI ones."""
    index, slot = place
    return min(plugins.getParamCount(index, slot, slot < 0), VST_PARAMS)


def _param_name(place, param):
    index, slot = place
    return plugins.getParamName(param, index, slot, slot < 0).strip()


def _value(place, param):
    index, slot = place
    return round(plugins.getParamValue(param, index, slot, slot < 0), 6)


def _set_value(place, param, value):
    index, slot = place
    plugins.setParamValue(value, param, index, slot, midi.PIM_None, slot < 0)


# --- keys and values ---------------------------------------------------------------

def _key_name(name):
    return name.replace("/", "|")


def _index(text):
    """'12' -> 12; None for anything else (signs, spaces, leading zeros)."""
    return int(text) if text.isdecimal() and str(int(text)) == text else None


def _level(value):
    """Someone's value as a parameter value 0..1, or None if it isn't a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(min(1.0, max(0.0, float(value))), 6)


class PluginParams(Binding):
    """The focused plugin of one kind; subclasses say where that kind lives."""

    seed = False       # far too many values for the MIDI link: only changes are sent
    sweep_budget = 4   # chunks re-read per tick even when nothing moved: 64 parameters
    window = None      # the ui window index (wid*) of this kind of plugin window
    indices = 1        # numbers before the plugin name in a key

    def __init__(self):
        self.focus = None    # (place, name, parameter slots) of the focused plugin, or None
        self.known = {}      # the same -> its real parameters, as last listed
        self.generation = 0  # part of every unit id, so units of an earlier focus read nothing
        self.unit_ids = []
        self.fresh = []      # units added last tick: tick() makes them the baseline
        self.params = []     # the real parameters the units are made of
        self.found = []      # the real parameters, as the names are read
        self.scanned = 0     # parameter slots whose names have been read
        self.last = {}       # parameter -> value last read, to see which ones move
        self.hot = {}        # unit -> time until which it is re-read every tick
        self.now = 0.0
        self.warned = False

    # --- where this kind of plugin lives (subclasses) ----------------------------

    def exists(self, place):
        """Whether the channel or mixer slot `place` exists in this project."""
        raise NotImplementedError

    def find(self, form_id, wanted):
        """The place of the focused window's plugin, from the window's form ID
        and FL's name for the plugin."""
        raise NotImplementedError

    def where(self, place):
        """'kanal 3', 'Insert 2 slot 1'."""
        raise NotImplementedError

    # --- plugins and focus ---------------------------------------------------------

    def plugin_at(self, place):
        """The name of the plugin at `place`, or None if there is none."""
        if not self.exists(place) or not _valid(place):
            return None
        return _plugin_name(place) or None

    def is_named(self, place, wanted):
        """Whether the plugin at `place` is the one FL calls `wanted`: its own
        name, or the one the user gave it."""
        name = self.plugin_at(place) if wanted else None
        return name is not None and (wanted == name or wanted == _plugin_name(place, True))

    def focused(self):
        """(place, name, parameter slots) of the focused plugin of this kind, or None."""
        if not ui.getFocused(self.window):
            return None
        place = self.find(ui.getFocusedFormID(), ui.getFocusedPluginName())
        name = self.plugin_at(place)
        return None if name is None else (place, name, _slots(place))

    # --- reading -------------------------------------------------------------------

    def units(self):
        # Asked first thing every tick and before the client takes a snapshot,
        # so a plugin that lost focus is never read again.
        try:
            found = self.focused()
        except (TypeError, ValueError, RuntimeError) as e:  # FL refused a call: as if nothing had focus
            if not self.warned:
                self.warned = True
                log("kunde inte läsa vilket plugin som har fokus: %s" % e)
            found = None
        if found != self.focus:
            self.focus = found
            self._new_units()
            self.found, self.scanned = [], 0
            # A list from last time gives units at once. The names are read
            # again all the same, as plugins can rename their parameters.
            known = self.known.get(found)
            self.params = list(known) if known is not None else self.found
        return self.unit_ids

    def _new_units(self):
        """Drop the units: those added from now on are new, read whole as the baseline."""
        self.generation += 1
        self.unit_ids, self.fresh, self.last, self.hot = [], [], {}, {}

    def tick(self, now):
        self.now = now
        rebaseline, self.fresh = self.fresh, []
        if self.focus is not None:
            self._list_more()
        self.hot = {unit: until for unit, until in self.hot.items() if until > now}
        return list(self.hot), rebaseline

    def _list_more(self):
        """Read a few more parameter names of the focused plugin, then add the
        units that are ready: chunks of real parameters (the last one short)."""
        place, _, slots = self.focus
        if self.scanned < slots:
            end = min(slots, self.scanned + NAME_BUDGET)
            self.found += [param for param in range(self.scanned, end) if _param_name(place, param)]
            self.scanned = end
            if end == slots:
                self._remember(self.focus, self.found)
                if self.params != self.found:  # renamed parameters since it last had focus
                    self._new_units()
                    self.params = self.found
        listed = self.scanned == slots or self.params is not self.found
        start = len(self.unit_ids) * CHUNK
        for _ in range(UNIT_BUDGET):
            if start >= len(self.params) or (not listed and start + CHUNK > len(self.params)):
                break
            unit = (self.generation, start)
            self.unit_ids.append(unit)
            self.fresh.append(unit)
            start += CHUNK

    def _remember(self, plugin, params):
        self.known.pop(plugin, None)
        self.known[plugin] = list(params)
        if len(self.known) > REMEMBERED:
            del self.known[next(iter(self.known))]

    def read(self, unit):
        generation, start = unit
        if generation != self.generation:
            return  # a unit of a plugin that has lost focus, or was listed anew
        place, name, _ = self.focus
        base = self.key(place, name)
        for param in self.params[start:start + CHUNK]:
            value = _value(place, param)
            if not math.isfinite(value):
                # Some buggy plugins report NaN: it never equals itself, so it
                # would look moved at every read and be sent over and over.
                continue
            if self.last.get(param, value) != value:
                self._heat(unit)
            self.last[param] = value
            yield "%s/%d" % (base, param), value

    def _heat(self, unit):
        self.hot[unit] = self.now + HOT_TIME
        if len(self.hot) > MAX_HOT:
            del self.hot[min(self.hot, key=self.hot.get)]

    # --- keys ----------------------------------------------------------------------

    def key(self, place, name):
        """A plugin's key without the parameter: 'pc/3/Serum', 'pm/2/0/Fruity Limiter'."""
        return "/".join([self.prefix] + [str(n) for n in place[:self.indices]] + [_key_name(name)])

    def parse(self, key):
        """'pm/2/0/Fruity Limiter/1' -> ((2, 0), 'Fruity Limiter', 1); None if it doesn't fit."""
        items = key.split("/")
        if len(items) != self.indices + 3 or items[0] != self.prefix or not items[-2]:
            return None
        numbers = [_index(item) for item in items[1:-2] + items[-1:]]
        if None in numbers:
            return None
        place = tuple(numbers[:2]) if self.indices == 2 else (numbers[0], -1)
        return place, items[-2], numbers[-1]

    def target(self, key):
        """(place, param) if the key's plugin is at its place here and has that parameter."""
        parsed = self.parse(key)
        if parsed is None:
            return None
        place, name, param = parsed
        found = self.plugin_at(place)
        if found is None or _key_name(found) != name or param >= _slots(place):
            return None  # nothing there, another plugin, or no such parameter
        return place, param

    # --- writing -------------------------------------------------------------------

    def write(self, key, value):
        level = _level(value)
        if level is None:
            return None
        try:
            target = self.target(key)
            if target is None:
                return None
            place, param = target
            before = _value(place, param)
            _set_value(place, param, level)
            held = _value(place, param)
        except TypeError:  # FL refused, e.g. "Operation unsafe at current time"
            return None
        if held == before != level or not math.isfinite(held):
            held = level  # FL still shows the old value (or none): the plugin may take it a moment later
        if self.focus is not None and self.focus[0] == place:
            self.last[param] = held  # not a move of the user's
        return held

    def describe(self, key):
        parsed = self.parse(key)
        if parsed is None:
            return key
        place, name, param = parsed
        label = _param_name(place, param) if self.target(key) else ""
        return "%s (%s) %s" % (name.replace("|", "/"), self.where(place),
                               label or "parameter %d" % param)


class GeneratorParams(PluginParams):
    """The generator of the channel whose plugin window has focus."""

    prefix = "pc"
    window = midi.widPluginGenerator
    indices = 1

    def exists(self, place):
        channel, slot = place
        return slot == -1 and 0 <= channel < channels.channelCount(True)

    def find(self, form_id, wanted):
        # The form ID is a channel index: within the channel group on show, as the
        # Universal Controller Script has it, or global (FL 2025.2.4 gave 4 for
        # global channel 4, in a project without groups). FL's name for the
        # plugin decides; where it can't, the selected channel does (opening a
        # plugin window selects its channel).
        options = [form_id]
        if 0 <= form_id < channels.channelCount():
            group = channels.getChannelIndex(form_id)
            if group != form_id:
                options.insert(0, group)
        named = [channel for channel in options if self.is_named((channel, -1), wanted)]
        if len(named) != 1:
            selected = channels.channelNumber()
            if selected in named or (not named and self.is_named((selected, -1), wanted)):
                return selected, -1
        return (named or options)[0], -1

    def where(self, place):
        return "kanal %d" % (place[0] + 1)


class EffectParams(PluginParams):
    """The effect in the mixer slot whose plugin window has focus."""

    prefix = "pm"
    window = midi.widPluginEffect
    indices = 2

    def exists(self, place):
        track, slot = place
        return track in tracks() and 0 <= slot < SLOTS

    def find(self, form_id, wanted):
        # mixer.getActiveEffectIndex() gives the focused effect's track and slot.
        # The form ID is the plugin ID mixer.getTrackPluginId() builds,
        # ((track << 6) + slot) << 16. FL's name for the plugin decides.
        options = [(form_id >> 22, (form_id >> 16) & 0x3F)]
        active = mixer.getActiveEffectIndex() if hasattr(mixer, "getActiveEffectIndex") else None
        if active is not None and tuple(active) != options[0]:
            options.insert(0, tuple(active))
        for place in options:
            if self.is_named(place, wanted):
                return place
        return options[0]

    def where(self, place):
        track, slot = place
        return "%s slot %d" % ("Master" if track == 0 else "Insert %d" % track, slot + 1)
