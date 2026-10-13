"""Fake FL song settings, transport and playlist tracks.

Like FL 2025.2.4 where it matters:
  * The tempo lives in the fake mixer (project.mixer.tempo, in BPM);
    general.processRECEvent(REC_Tempo, ...) reads and sets it in BPM * 1000.
  * The time signature can be set (general.setNumerator/setDenominator) but is
    only readable through general.getRecPPB(), the timebase times the numerator.
  * transport.start() is play/pause. While playing, the song position moves on
    at every refresh, and stop() goes back to where playback started.
    isPlaying() shows a start or stop only after FL's next refresh.
  * 500 playlist tracks numbered from 1, called "Track N" until renamed and all
    in one default color. Their mutes show after the next refresh, like the
    mixer's.

`calls` records the transport commands in order; user actions (press_play,
move_to, ...) aren't recorded. Options (FakeFL keyword arguments):
playlist_tracks=500, ppq=96.
"""


PLAYLIST_TRACKS = 500
DEFAULT_COLOR = -12571053   # an untouched track's color, measured in FL 2025.2.4
TICKS_PER_REFRESH = 12      # how far the playhead moves between two script calls
SONG_BARS = 32              # the song's length, for fractional positions
MIN_TEMPO, MAX_TEMPO = 10.0, 522.0

# Real values from FL's midi.py.
MIDI = {
    "SONGLENGTH_MS": 0, "SONGLENGTH_S": 1, "SONGLENGTH_ABSTICKS": 2,
    "SONGLENGTH_BARS": 3, "SONGLENGTH_STEPS": 4, "SONGLENGTH_TICKS": 5,
    "SM_Pat": 0, "SM_Song": 1,
    "REC_Tempo": 0x4000 * 0x10000 + 5, "REC_UpdateValue": 1, "REC_GetValue": 2,
    "REC_UpdateControl": 16, "REC_Control": 973, "REC_InvalidID": 2147483647,
    "PM_Stopped": 0, "PM_Playing": 1, "PM_Precount": 2,
    "HW_Dirty_LEDs": 256, "HW_Dirty_Tracks": 2048, "HW_Dirty_Undo": 131072,
    "FPT_Save": 92, "PME_System": 2, "GT_Global": 8,
}


class PlaylistTrack:
    def __init__(self):
        self.name = ""
        self.color = DEFAULT_COLOR
        self.mute = False       # what isTrackMuted() reports...
        self.mute_real = False  # ...lags behind until FL refreshes


class FakeSong:
    def __init__(self, project, tracks=PLAYLIST_TRACKS, ppq=96):
        self.project = project
        self.ppq = ppq
        self.num = self.den = 4
        self.loop = MIDI["SM_Pat"]
        self.playing = False       # what isPlaying() reports...
        self.playing_real = False  # ...lags behind until FL refreshes
        self.recording = False
        self.length = SONG_BARS    # in bars: clips placed later in the song make it longer
        self.pos = 0               # absolute ticks
        self.started_at = 0        # where playback started: stop() goes back there
        self.calls = []            # transport commands, e.g. ("setSongPos", 768, 2)
        self.track_reads = 0       # playlist.getTrackName() calls
        self.tracks = [None] + [PlaylistTrack() for _ in range(tracks)]  # numbered from 1
        self.lagging = set()       # tracks whose mute doesn't show yet

    def valid(self, track):
        if isinstance(track, int) and 1 <= track < len(self.tracks):
            return True
        self.project.bad(("playlist", track))
        return False

    def refresh(self):
        if self.playing_real:
            self.pos += TICKS_PER_REFRESH
        self.playing = self.playing_real
        for track in self.lagging:
            self.tracks[track].mute = self.tracks[track].mute_real
        self.lagging.clear()

    def start(self):
        if self.playing_real:  # the play button pauses while playing
            self.playing_real = False
        else:
            self.playing_real = True
            self.started_at = self.pos

    def stop(self):
        if self.playing_real:
            self.playing_real = False
            self.pos = self.started_at

    # --- time --------------------------------------------------------------

    def bar(self):
        return self.ppq * self.num

    def ticks_per_ms(self):
        return self.project.mixer.tempo * self.ppq / 60000.0

    def to_unit(self, ticks, mode):
        step = self.ppq // 4
        units = {-1: lambda: ticks / float(SONG_BARS * self.bar()),
                 0: lambda: int(ticks / self.ticks_per_ms()),
                 1: lambda: int(ticks / self.ticks_per_ms()) // 1000,
                 2: lambda: ticks,
                 3: lambda: ticks // self.bar() + 1,              # bars count from 1
                 4: lambda: ticks % self.bar() // step + 1,       # so do steps, like FL's B:S:T
                 5: lambda: ticks % step}
        if mode not in units:
            self.project.bad(("song time mode", mode))
            return 0
        return units[mode]()

    def from_unit(self, value, mode):
        """Ticks for a position given to setSongPos; None for the units it can't take."""
        if mode == -1:
            return int(value * SONG_BARS * self.bar())
        if mode in (0, 1):
            return int(value * (1000 if mode else 1) * self.ticks_per_ms())
        if mode == 2:
            return int(value)
        self.project.bad(("setSongPos mode", mode))
        return None

    # --- what a user does in FL ----------------------------------------------

    def press_play(self):
        self.start()

    def press_stop(self):
        self.stop()

    def move_to(self, ticks):
        self.pos = ticks


def install(project):
    options = project.options
    song = FakeSong(project, options.get("playlist_tracks", PLAYLIST_TRACKS), options.get("ppq", 96))
    project.song = song
    project.refreshers.append(song.refresh)

    midi = project.module("midi")
    for name, value in MIDI.items():
        setattr(midi, name, value)

    t = project.module("transport")

    def command(name, action):
        def run():
            song.calls.append((name,))
            action()
        return run
    t.start = command("start", song.start)
    t.stop = command("stop", song.stop)
    t.isPlaying = lambda: song.playing
    t.isRecording = lambda: song.recording
    t.record = command("record", lambda: setattr(song, "recording", not song.recording))
    t.getLoopMode = lambda: song.loop
    t.setLoopMode = command("setLoopMode", lambda: setattr(song, "loop", 1 - song.loop))
    t.getSongPos = lambda mode=-1: song.to_unit(song.pos, mode)
    t.getSongLength = lambda mode: song.to_unit(song.length * song.bar(), mode)

    def global_transport(command, value, pmeflags=2, flags=15):
        song.calls.append(("globalTransport", command, value, flags))
        if command == MIDI["FPT_Save"] and value:
            project.save_requests += 1  # what FL then does (saves, asks where) is the test's
            if project.on_save:
                project.on_save()
        return 1
    t.globalTransport = global_transport

    def set_song_pos(position, mode=-1):
        song.calls.append(("setSongPos", position, mode))
        ticks = song.from_unit(position, mode)
        if ticks is not None:
            song.pos = max(0, ticks)
    t.setSongPos = set_song_pos

    g = project.module("general")
    g.getRecPPQ = lambda: song.ppq
    g.getRecPPB = lambda: song.ppq * song.num

    def set_numerator(value):
        if isinstance(value, int) and 1 <= value <= 32:
            song.num = value
        else:
            project.bad(("numerator", value))

    def set_denominator(value):
        if value in (1, 2, 4, 8, 16, 32):
            song.den = value
        else:
            project.bad(("denominator", value))
    g.setNumerator = set_numerator
    g.setDenominator = set_denominator

    other_events = getattr(g, "processRECEvent", None)  # another area's fake may have one

    def process_rec_event(event_id, value, flags):
        if event_id == midi.REC_Tempo:  # BPM * 1000, like getCurrentTempo()
            if not isinstance(value, int):
                project.bad(("rec event value", value))
            elif flags & midi.REC_UpdateValue:
                project.mixer.tempo = round(min(MAX_TEMPO, max(MIN_TEMPO, value / 1000.0)), 3)
            return int(round(project.mixer.tempo * 1000))
        if other_events is not None:
            return other_events(event_id, value, flags)
        project.bad(("rec event", event_id))
        return midi.REC_InvalidID
    g.processRECEvent = process_rec_event

    p = project.module("playlist")
    tracks = song.tracks
    p.trackCount = lambda: len(tracks) - 1

    def get_name(i):
        song.track_reads += 1
        if not song.valid(i):
            return ""
        return tracks[i].name or "Track %d" % i

    def set_name(i, name):
        if song.valid(i):
            tracks[i].name = name  # "" gives the default name back
    p.getTrackName = get_name
    p.setTrackName = set_name
    p.getTrackColor = lambda i: tracks[i].color if song.valid(i) else 0

    def set_color(i, color):
        if song.valid(i):
            tracks[i].color = color
    p.setTrackColor = set_color
    p.isTrackMuted = lambda i: tracks[i].mute if song.valid(i) else False

    def mute(i, value=-1):
        if song.valid(i):
            tracks[i].mute_real = (not tracks[i].mute_real) if value == -1 else bool(value)
            song.lagging.add(i)
    p.muteTrack = mute
