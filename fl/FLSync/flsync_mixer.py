"""The mixer: track settings, effect slots and routing (keys m/, mx/, mr/).

Split in three bindings because their costs differ a lot: a track's settings
are a handful of API calls, its effect slots ten more, and its routing one
call per other track. Each gets its own sweep budget.

Keys are track numbers, and FL 2025+ can insert, delete and move inserts,
which gives the ones after them other numbers (and re-routes the sends to
them). When the number of tracks changes, every track's values are taken as
they are instead of being sent: they would rename and re-route the other FLs'
inserts. The structure changed, so the client pauses until it's shared.
"""

import math
import re

import general
import midi
import mixer

from flsync_binding import ALL, Binding, is_color

SLOTS = 10
MUTE_API = 30  # muteTrack's value argument; before it, muteTrack only toggles
_EQ_KEY = re.compile(r"e(\d+)([gfw])$")


def tracks():
    # FL 2024+ has a dynamic mixer: trackCount() is how many tracks exist, while
    # TN_LastIns is the highest possible insert (500). Older versions count the
    # "current" track as the last one. Either way the tracks we want come first.
    return range(min(mixer.trackCount(), mixer.getTrackInfo(midi.TN_Sel)))


def default_name(track):
    return "Master" if track == 0 else "Insert %d" % track


def track_label(track):
    name = mixer.getTrackName(track)
    if track == 0 or name == default_name(track):
        return name
    return "Insert %d (%s)" % (track, name)


def _number(value, low, high):
    """Someone's `value` limited to low..high, or None if it isn't a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(min(high, max(low, value)))


def _switch(value):
    """Someone's on/off value (a bool, or 0 or 1) as a bool, or None if it isn't one."""
    return bool(value) if value in (0, 1) else None


def _split(key, prefix, parts):
    """'m/3/v' -> (3, 'v') for prefix 'm'; None if the key doesn't fit."""
    items = key.split("/")
    if len(items) != parts or items[0] != prefix:
        return None
    try:
        items[1] = int(items[1])
        if len(items) > 3:
            items[2] = int(items[2])
    except ValueError:
        return None
    return items[1:]


class TrackBinding(Binding):
    """A binding whose units are the mixer's tracks. When the number of tracks
    changes (inserts added or deleted), tick() makes all of them the baseline."""

    def __init__(self):
        self.on_project_load()

    def on_project_load(self):
        self.found = tracks()         # the tracks units() found last
        self.count = len(self.found)  # how many tracks the last tick saw

    def units(self):
        self.found = tracks()
        return self.found

    def tick(self, now):
        # units() was asked first this tick: no FL calls needed here.
        if len(self.found) == self.count:
            return (), ()
        self.count = len(self.found)
        return (), self.found


class MixerBinding(TrackBinding):
    """Volume, pan, mute, name, color, stereo separation, EQ, effects on/off."""

    prefix = "m"
    sweep_budget = 6

    def __init__(self):
        super().__init__()
        self.bands = mixer.getEqBandCount() if hasattr(mixer, "getEqBandCount") else 0
        try:
            api = general.getVersion()
        except Exception:
            api = None
        self.set_mute = isinstance(api, int) and api >= MUTE_API

    def read(self, track):
        yield "m/%d/v" % track, round(mixer.getTrackVolume(track), 6)
        yield "m/%d/p" % track, round(mixer.getTrackPan(track), 6)
        yield "m/%d/mu" % track, bool(mixer.isTrackMuted(track))
        yield "m/%d/n" % track, mixer.getTrackName(track)
        yield "m/%d/c" % track, mixer.getTrackColor(track)
        yield "m/%d/s" % track, round(mixer.getTrackStereoSep(track), 6)
        for band in range(self.bands):
            yield "m/%d/e%dg" % (track, band), round(mixer.getEqGain(track, band), 6)
            yield "m/%d/e%df" % (track, band), round(mixer.getEqFrequency(track, band), 6)
            yield "m/%d/e%dw" % (track, band), round(mixer.getEqBandwidth(track, band), 6)
        yield "m/%d/fx" % track, bool(mixer.isTrackSlotsEnabled(track))

    def write(self, key, value):
        parsed = _split(key, "m", 3)
        if parsed is None or parsed[0] not in tracks():
            return None
        try:
            return self._set(parsed[0], parsed[1], value)
        except (TypeError, ValueError):  # e.g. FL's "Operation unsafe at current time"
            return None

    def _set(self, track, prop, value):
        """Put a value of the right kind into FL; None for anything else."""
        if prop in ("v", "p", "s"):
            level = _number(value, 0.0 if prop == "v" else -1.0, 1.0)
            if level is None:
                return None
            if prop == "v":
                mixer.setTrackVolume(track, level)
                return round(mixer.getTrackVolume(track), 6)
            if prop == "p":
                mixer.setTrackPan(track, level)
                return round(mixer.getTrackPan(track), 6)
            mixer.setTrackStereoSep(track, level)
            return round(mixer.getTrackStereoSep(track), 6)
        if prop == "mu":
            muted = _switch(value)
            if muted is None:
                return None
            if self.set_mute:
                mixer.muteTrack(track, 1 if muted else 0)  # sets; -1 would toggle
            elif bool(mixer.isTrackMuted(track)) != muted:
                mixer.muteTrack(track)  # before API 30 it can only toggle
            # isTrackMuted() only shows the change after FL's next refresh (a
            # toggle based on it would undo a second quick change), so trust the
            # value we asked for.
            return muted
        if prop == "n":
            if not isinstance(value, str):
                return None
            # A default name ("Insert 3") is reset rather than set, or the
            # track would get a custom name that only looks like the default.
            mixer.setTrackName(track, "" if value == default_name(track) else value)
            return mixer.getTrackName(track)
        if prop == "c":
            if not is_color(value):
                return None
            mixer.setTrackColor(track, value)
            return mixer.getTrackColor(track)
        if prop == "fx":
            enabled = _switch(value)
            if enabled is None:
                return None
            mixer.enableTrackSlots(track, enabled)
            return enabled
        match = _EQ_KEY.match(prop)
        level = _number(value, 0.0, 1.0)
        if not match or int(match.group(1)) >= self.bands or level is None:
            return None
        band, what = int(match.group(1)), match.group(2)
        if what == "g":
            mixer.setEqGain(track, band, level)
            return round(mixer.getEqGain(track, band), 6)
        if what == "f":
            mixer.setEqFrequency(track, band, level)
            return round(mixer.getEqFrequency(track, band), 6)
        mixer.setEqBandwidth(track, band, level)
        return round(mixer.getEqBandwidth(track, band), 6)

    def describe(self, key):
        parsed = _split(key, "m", 3)
        if parsed is None:
            return key
        track, prop = parsed
        labels = {"v": "volym", "p": "pan", "mu": "mute", "n": "namn", "c": "färg",
                  "s": "stereobredd", "fx": "effekter på/av"}
        match = _EQ_KEY.match(prop)
        if match:
            label = "EQ band %d %s" % (int(match.group(1)) + 1,
                                       {"g": "gain", "f": "frekvens", "w": "bredd"}[match.group(2)])
        else:
            label = labels.get(prop, prop)
        return "%s %s" % (track_label(track), label)

    def shared_at_start(self, key, value):
        parsed = _split(key, "m", 3)
        return not (parsed and parsed[1] == "n" and value == default_name(parsed[0]))

    def on_dirty_mixer_track(self, index):
        return ALL if index < 0 else [index]

    def on_refresh(self, flags):
        # FL doesn't say which track's control moved; the selected one is the best guess.
        return [mixer.trackNumber()] if flags & midi.HW_Dirty_Mixer_Controls else ()


class MixerSlotsBinding(TrackBinding):
    """Mix level of the effect slots that hold a plugin.

    Slot on/off isn't synced: in FL 2025.2.4 setPluginMuteState can switch a
    slot off but never back on (getPluginMuteState reads 1 for "on").
    """

    prefix = "mx"
    sweep_budget = 2

    def units(self):
        if not hasattr(mixer, "getPluginMixLevel"):  # API 38+
            return ()
        return super().units()

    def read(self, track):
        for slot in range(SLOTS):
            if mixer.isTrackPluginValid(track, slot):
                yield "mx/%d/%d/m" % (track, slot), round(mixer.getPluginMixLevel(track, slot), 6)

    def write(self, key, value):
        parsed = _split(key, "mx", 4)
        if parsed is None or parsed[0] not in tracks() or not 0 <= parsed[1] < SLOTS:
            return None
        track, slot, prop = parsed
        level = _number(value, 0.0, 1.0)
        if prop != "m" or level is None:
            return None
        level = round(level, 6)
        try:
            if not mixer.isTrackPluginValid(track, slot):
                return None
            mixer.setPluginMixLevel(track, slot, level)
        except (TypeError, ValueError):  # e.g. FL's "Operation unsafe at current time"
            return None
        # getPluginMixLevel() shows the new level only after FL's next refresh.
        return level

    def describe(self, key):
        parsed = _split(key, "mx", 4)
        if parsed is None:
            return key
        return "%s slot %d mix" % (track_label(parsed[0]), parsed[1] + 1)

    def on_dirty_mixer_track(self, index):
        return ALL if index < 0 else [index]


class MixerRoutingBinding(TrackBinding):
    """Where each track sends its audio, and how loud."""

    prefix = "mr"
    sweep_budget = 1
    burst_calls = 1024  # FL calls per tick for re-reading the tracks after FL reported all of them changed

    def on_project_load(self):
        super().on_project_load()
        self.burst = []  # tracks still to re-read for a change FL reported in all of them

    def read(self, track):
        routes = self._routes(track)
        if routes is not None:
            yield "mr/%d" % track, routes

    def _routes(self, track):
        """'dest:level,...', or None if FL gives a level that isn't a number."""
        sends = []
        for dest in tracks():
            if dest != track and mixer.getRouteSendActive(track, dest):
                level = mixer.getRouteToLevel(track, dest)
                if not math.isfinite(level):
                    return None
                sends.append("%d:%s" % (dest, round(level, 4)))
        return ",".join(sends)

    def write(self, key, value):
        items = key.split("/")
        if len(items) != 2 or items[0] != "mr" or not isinstance(value, str):
            return None
        try:
            track = int(items[1])
            wanted = {}
            for part in filter(None, value.split(",")):
                dest, level = part.split(":")
                wanted[int(dest)] = float(level)
        except ValueError:
            return None
        if track not in tracks() or not all(math.isfinite(level) for level in wanted.values()):
            return None
        try:
            for dest in tracks():
                if dest == track:
                    continue
                active = bool(mixer.getRouteSendActive(track, dest))
                if active != (dest in wanted):
                    mixer.setRouteTo(track, dest, dest in wanted)
                if dest in wanted and round(mixer.getRouteToLevel(track, dest), 4) != wanted[dest]:
                    mixer.setRouteToLevel(track, dest, _number(wanted[dest], 0.0, 1.0))
            mixer.afterRoutingChanged()
            return self._routes(track)
        except (TypeError, ValueError):  # e.g. FL's "Operation unsafe at current time"
            return None

    def describe(self, key):
        parsed = key.split("/")
        try:
            return "%s routing" % track_label(int(parsed[1]))
        except (IndexError, ValueError):
            return key

    def on_dirty_mixer_track(self, index):
        if index >= 0:
            return [index]
        # All tracks changed, FL says. Reading every pair of tracks at once would
        # take some 16000 FL calls in one tick with 126 tracks: a few per tick instead.
        self.burst = list(self.found)
        return ()

    def tick(self, now):
        reread, rebaseline = super().tick(now)
        if rebaseline:
            self.burst = []  # all of them are read now anyway
            return reread, rebaseline
        per_tick = max(1, self.burst_calls // max(1, len(self.found) - 1))  # a track: a call per other track
        reread, self.burst = self.burst[:per_tick], self.burst[per_tick:]
        return reread, ()
