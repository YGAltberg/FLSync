"""Song settings, transport and the playlist's tracks (keys s/ and pl/).

Song: tempo, time signature, pattern/song mode and play/stop. s/play and s/pos
are events (transient): they act when someone presses play or stop, never when
a client joins. s/pos is where playback started, in quarter notes, and -1 while
stopped. It never follows the playhead, and since it changes with every start
and stop, every start carries its own position, even to an FL that joined after
the previous one.

Only the time signature's numerator is synced: FL can set the denominator
(general.setDenominator) but no script function reads it.

Playlist tracks are numbered from 1, as in FL. FL 2025 always has 500 of them,
nearly all untouched, so untouched ones stay out of the starting values, and
the tracks in use are re-read more often than the rest.
"""

import math

import channels
import general
import midi
import mixer
import playlist
import transport

from flsync_binding import Binding, is_color

SONG = "song"             # the song binding's only unit
STOPPED = -1              # s/pos while nothing plays
MIN_TEMPO, MAX_TEMPO = 10.0, 522.0
MAX_NUMERATOR = 32
HOLD_TIME = 1.0           # isPlaying() can lag our own start/stop: trust what we asked for this long
HOT_TRACKS = 32           # playlist tracks re-read often: at least the first 32...
HOT_MARGIN = 8            # ...and 8 past the last one in use
HOT_PER_TICK = 2
PLAYLIST_PROPS = {"n": "namn", "c": "färg", "mu": "mute"}
SONG_LABELS = {"s/tempo": "Tempo", "s/num": "Taktart", "s/swing": "Swing", "s/loop": "Mönster-/låtläge",
               "s/play": "Uppspelning", "s/pos": "Startposition"}


def _number(value):
    """A finite int or float as float (bools aren't numbers here), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def tempo():
    # FL 2025.2.4 gives BPM * 1000 from getCurrentTempo() and BPM from
    # getCurrentTempo(True), the reverse of its documentation. Tempos stop at
    # 522 BPM, so above 1000 it's the scaled kind, whichever FL does it.
    raw = mixer.getCurrentTempo()
    return round(raw / 1000 if raw > 1000 else raw, 3)


def set_tempo(bpm):
    """True if FL took it. REC_Tempo is BPM * 1000; mixer.setCurrentTempo
    raises "Range error" in FL 2025.2.4 whatever it's given."""
    return general.processRECEvent(midi.REC_Tempo, int(round(bpm * 1000)),
                                   midi.REC_Control | midi.REC_UpdateControl) != midi.REC_InvalidID


def numerator():
    """Beats per bar: getRecPPB() is the timebase (PPQ) times the numerator.
    None if that doesn't come out whole."""
    ppq = general.getRecPPQ()
    if ppq <= 0:
        return None
    beats, rest = divmod(general.getRecPPB(), ppq)
    return beats if beats > 0 and not rest else None


def _ppq():
    return general.getRecPPQ() or 96


def _ticks():
    return transport.getSongPos(midi.SONGLENGTH_ABSTICKS)


def _beats(ticks):
    return round(ticks / _ppq(), 4)


def _swing():
    """(get, set) of the channel rack's main swing, 0..1: FL 2026's
    channels.getSwing/setSwing, or getShuffle/setShuffle as API 42 was
    documented. None if this FL has neither."""
    for get, put in (("getSwing", "setSwing"), ("getShuffle", "setShuffle")):
        if hasattr(channels, get) and hasattr(channels, put):
            return getattr(channels, get), getattr(channels, put)
    return None


class SongBinding(Binding):
    """Tempo, time signature numerator, pattern/song mode and play/stop."""

    prefix = "s"
    sweep_budget = 1  # one unit, so it is read every tick and play/stop shows at once

    def __init__(self):
        self.now = 0.0
        self.hold_until = float("-inf")
        self.playing = bool(transport.isPlaying())
        self.cue = _ticks()  # the position while stopped: where FL starts playing
        self.pos = _beats(self.cue) if self.playing else STOPPED

    def units(self):
        return (SONG,)

    def tick(self, now):
        self.now = now
        return (), ()

    def read(self, unit):
        yield "s/tempo", tempo()
        num = numerator()
        if num is not None:
            yield "s/num", num
        swing = _swing()
        if swing is not None:
            try:
                level = round(swing[0](), 4)
            except (TypeError, ValueError):  # not now, or this FL's function wants something else
                level = None
            if level is not None:
                yield "s/swing", level
        yield "s/loop", int(transport.getLoopMode())
        playing = self._is_playing()
        if playing != self.playing:  # pressed play or stop here
            self.playing = playing
            self.pos = self._start_position() if playing else STOPPED
        elif not playing:
            self.cue = _ticks()
        # Position first: a batch with both must seek before it starts.
        yield "s/pos", self.pos
        yield "s/play", playing

    def _is_playing(self):
        if self.now < self.hold_until:
            return self.playing  # FL may not show our own start/stop yet
        return bool(transport.isPlaying())

    def _start_position(self):
        # FL starts where it stood while stopped, and we see the start a tick
        # late: snap back to that spot unless the playhead clearly came from elsewhere.
        ticks = _ticks()
        if 0 <= ticks - self.cue <= _ppq():
            ticks = self.cue
        return _beats(ticks)

    def write(self, key, value):
        try:
            return self._write(key, value)
        except TypeError:  # FL: "Operation unsafe at current time"
            return None

    def _write(self, key, value):
        if key == "s/tempo":
            bpm = _number(value)
            if bpm is None or not set_tempo(min(MAX_TEMPO, max(MIN_TEMPO, bpm))):
                return None
            return tempo()  # FL shows the new tempo at once
        if key == "s/num":
            if (_number(value) is None or value != int(value) or not 1 <= value <= MAX_NUMERATOR
                    or not hasattr(general, "setNumerator")):
                return None
            general.setNumerator(int(value))
            return int(value)  # getRecPPB() may show it a moment later
        if key == "s/swing":
            level, swing = _number(value), _swing()
            if level is None or swing is None:
                return None
            swing[1](min(1.0, max(0.0, level)))
            return round(swing[0](), 4)
        if key == "s/loop":
            if value not in (0, 1):
                return None
            if transport.getLoopMode() != value:
                transport.setLoopMode()  # toggles between pattern and song
            return int(value)
        if key == "s/pos":
            pos = _number(value)
            if pos is None or (pos < 0 and pos != STOPPED):
                return None
            self.pos = STOPPED if pos < 0 else round(pos, 4)  # used by the next remote play
            return self.pos
        if key == "s/play" and value in (0, 1):
            self._set_playing(bool(value))
            return bool(value)
        return None

    def _set_playing(self, play):
        # Our own state changes only after FL took the commands (they can raise).
        playing = self._is_playing()
        pos = self.pos if play else STOPPED
        if play and not playing:
            if pos == STOPPED:  # no position came with it: start here, and say where
                pos = _beats(_ticks())
            else:
                transport.setSongPos(int(round(pos * _ppq())), midi.SONGLENGTH_ABSTICKS)
            transport.start()  # play/pause: only ever called while stopped
            self.hold_until = self.now + HOLD_TIME
        elif not play and playing:
            transport.stop()
            self.hold_until = self.now + HOLD_TIME
        self.pos = pos
        self.playing = play

    def describe(self, key):
        return SONG_LABELS.get(key, key)

    def transient(self, key):
        return key in ("s/play", "s/pos")


def _track_key(key):
    """'pl/3/mu' -> (3, 'mu'); None if the key doesn't fit."""
    items = key.split("/")
    if len(items) != 3 or items[0] != "pl" or items[2] not in PLAYLIST_PROPS:
        return None
    try:
        track = int(items[1])
    except ValueError:
        return None
    return (track, items[2]) if str(track) == items[1] else None


def default_track_name(track):
    return "Track %d" % track


def track_label(track):
    name = playlist.getTrackName(track)
    if name == default_track_name(track):
        return "Spellistespår %d" % track
    return "Spellistespår %d (%s)" % (track, name)


class PlaylistBinding(Binding):
    """Name, color and mute of the playlist's tracks."""

    prefix = "pl"
    sweep_budget = 2  # all 500 tracks, slowly; tick() adds the ones in use

    def __init__(self):
        self.count = 0
        self.on_project_load()

    def on_project_load(self):
        self.used = 0     # the last track seen with a name, color or mute of its own
        self.hot = 0      # where the rotation through the tracks in use stands
        self.default_color = None  # untouched tracks' color, read when first needed

    def units(self):
        self.count = playlist.trackCount()
        return range(1, self.count + 1)

    def tick(self, now):
        hot = min(self.count, max(HOT_TRACKS, self.used + HOT_MARGIN))
        tracks = []
        for _ in range(min(HOT_PER_TICK, hot)):
            self.hot = self.hot % hot + 1
            tracks.append(self.hot)
        return tracks, ()

    def read(self, track):
        name = playlist.getTrackName(track)
        color = playlist.getTrackColor(track)
        muted = bool(playlist.isTrackMuted(track))
        if track > self.used and (muted or name != default_track_name(track)
                                  or color != self._default_color()):
            self.used = track
        yield "pl/%d/n" % track, name
        yield "pl/%d/c" % track, color
        yield "pl/%d/mu" % track, muted

    def _default_color(self):
        # Untouched tracks share one color, and the last of FL's 500 tracks is
        # as good as certain to be untouched.
        if self.default_color is None:
            last = playlist.trackCount()
            if last < 1:
                return None
            self.default_color = playlist.getTrackColor(last)
        return self.default_color

    def write(self, key, value):
        try:
            return self._write(key, value)
        except TypeError:  # FL: "Operation unsafe at current time"
            return None

    def _write(self, key, value):
        parsed = _track_key(key)
        if parsed is None or not 1 <= parsed[0] <= playlist.trackCount():
            return None
        track, prop = parsed
        if prop == "n" and isinstance(value, str):
            # An empty name gives the track its default name ("Track 3") back.
            playlist.setTrackName(track, "" if value == default_track_name(track) else value)
            return playlist.getTrackName(track)
        if prop == "c" and is_color(value):
            playlist.setTrackColor(track, value)
            return playlist.getTrackColor(track)
        if prop == "mu" and value in (0, 1):
            # Set, don't toggle (the value argument is API 30+): isTrackMuted()
            # lags, and a toggle based on it would undo a second quick change.
            # For the same reason, trust the value we asked for.
            playlist.muteTrack(track, int(value))
            return bool(value)
        return None

    def describe(self, key):
        parsed = _track_key(key)
        if parsed is None or not 1 <= parsed[0] <= playlist.trackCount():
            return key
        track, prop = parsed
        return "%s %s" % (track_label(track), PLAYLIST_PROPS[prop])

    def shared_at_start(self, key, value):
        parsed = _track_key(key)
        if parsed is None:
            return False
        track, prop = parsed
        if prop == "n":
            return value != default_track_name(track)
        if prop == "c":
            return value != self._default_color()
        return bool(value)


def bindings():
    return [SongBinding(), PlaylistBinding()]
