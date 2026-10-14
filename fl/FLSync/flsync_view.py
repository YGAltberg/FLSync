"""Where the user is in FL (the selected pattern and channel, the mixer track,
the window in front, the playlist selection), to put back after FLSync made FL
open someone's version (which brings that version's), or ran a piano roll
script for their notes. Only what FL's script can set without it being an edit:
measured in FL 26.1.7, none of these make an undo step or unsaved changes."""

import channels
import midi
import mixer
import ui

try:
    import patterns
except ImportError:
    patterns = None
try:
    import arrangement
except ImportError:
    arrangement = None

GLOBAL = 1  # channels: indices over the whole channel rack, not the display group
WINDOWS = ("widMixer", "widChannelRack", "widPlaylist", "widPianoRoll", "widBrowser")


def _call(function, *args):
    try:
        return function(*args)
    except Exception:  # noqa: BLE001 - an FL that can't say (or not just now) keeps what it has
        return None


def _has(module, name):
    return module is not None and hasattr(module, name)


def capture():
    """The view now, as a dict (what FL can't tell is left out)."""
    view = {}
    if _has(patterns, "patternNumber"):
        number = _call(patterns.patternNumber)
        if isinstance(number, int) and number > 0:
            view["pattern"] = (number, _call(patterns.getPatternName, number) if _has(patterns, "getPatternName")
                               else None)
    if _has(channels, "selectedChannel"):
        index = _call(channels.selectedChannel, 1, 0, GLOBAL)
        if isinstance(index, int) and index >= 0:
            view["channel"] = (index, _call(channels.getChannelName, index, GLOBAL))
    if _has(mixer, "trackNumber"):
        track = _call(mixer.trackNumber)
        if isinstance(track, int) and track >= 0:
            view["track"] = track
    if _has(ui, "getFocused"):
        for name in WINDOWS:
            wid = getattr(midi, name, None)
            if wid is not None and _call(ui.getFocused, wid):
                view["focused"] = name
                break
    if _has(arrangement, "selectionStart") and _has(arrangement, "selectionEnd"):
        start, end = _call(arrangement.selectionStart), _call(arrangement.selectionEnd)
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end:
            view["selection"] = (start, end)
    return view


def restore(view):
    """Put `view` back where it still fits this project (the same pattern and
    channel by name). What was put back, for the log."""
    done = []
    pattern = view.get("pattern")
    if pattern and _has(patterns, "jumpToPattern") and _has(patterns, "patternCount"):
        number, name = pattern
        count = _call(patterns.patternCount) or 0
        here = _call(patterns.getPatternName, number) if _has(patterns, "getPatternName") else None
        if 0 < number <= max(count, 0) and (name is None or here == name) and _call(patterns.patternNumber) != number:
            _call(patterns.jumpToPattern, number)
            done.append("mönster %d" % number)
    channel = view.get("channel")
    if channel and _has(channels, "selectOneChannel") and _has(channels, "channelCount"):
        index, name = channel
        if index < (_call(channels.channelCount, GLOBAL) or 0) and _call(channels.getChannelName, index, GLOBAL) == name:
            _call(channels.selectOneChannel, index, GLOBAL)
            done.append("kanal %d" % (index + 1))
    track = view.get("track")
    if track is not None and _has(mixer, "setTrackNumber") and track < (_call(mixer.trackCount) or 0):
        _call(mixer.setTrackNumber, track, 1)
        done.append("mixerkanal %d" % track)
    selection = view.get("selection")
    if selection and _has(arrangement, "selectionSet"):
        _call(arrangement.selectionSet, selection[0], selection[1])
        done.append("markeringen")
    focused = view.get("focused")
    wid = getattr(midi, focused, None) if focused else None
    if wid is not None and _has(ui, "showWindow") and _has(ui, "setFocused"):
        _call(ui.showWindow, wid)
        _call(ui.setFocused, wid)
        done.append("fönstret")
    return done
