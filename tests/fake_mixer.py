"""Fake FL mixer: tracks with volume, pan, mute, name, color, stereo separation,
EQ, effect slots and routing, plus the song tempo (which lives in FL's mixer
module).

Like FL 2025 by default: a dynamic number of tracks with TN_LastIns at 500.
Options (FakeFL keyword arguments): inserts=17, dynamic=True. dynamic=False
gives the fixed 125-insert mixer of older FL, with the "current" track counted
in trackCount().
"""

VOL_STEPS = 12800
PAN_STEPS = 6400
EQ_BANDS = 3
SLOTS = 10


class Track:
    def __init__(self, index):
        self.vol = int(0.8 * VOL_STEPS)
        self.pan = 0
        self.mute = False       # what isTrackMuted() reports...
        self.mute_real = False  # ...lags behind until FL refreshes
        self.name = ""
        self.color = -10261391
        self.sep = 0.0
        self.eq = [[0.5, 0.25 * (band + 1), 0.5] for band in range(EQ_BANDS)]  # gain, freq, width
        self.slots_on = True
        self.slots = {}         # slot -> {"mix": shown, "mix_real": float, "on": bool}
        self.routes = {0: 1.0} if index else {}  # dest -> level; inserts go to master


class FakeMixer:
    def __init__(self, project, inserts=17, dynamic=True):
        self.project = project
        self.inserts = inserts
        self.dynamic = dynamic
        self.current = 501 if dynamic else inserts + 1
        self.tracks = [Track(i) for i in range(inserts + 1)]
        self.selected = 0
        self.tempo = 140.0

    def valid(self, track):
        if isinstance(track, int) and 0 <= track < len(self.tracks):
            return True
        self.project.bad(("mixer", track))
        return False

    def touch(self, track):
        self.project.dirty_mixer.append(track)

    def refresh(self):
        for track in self.tracks:
            track.mute = track.mute_real
            for entry in track.slots.values():
                entry["mix"] = entry["mix_real"]

    def snapshot(self):
        return [(t.vol, t.pan, t.mute, t.name, t.color, t.sep, [list(b) for b in t.eq],
                 t.slots_on, sorted(t.slots.items()), sorted(t.routes.items()))
                for t in self.tracks]

    # user actions that FL's API can't do
    def add_plugin(self, track, slot, mix=1.0):
        self.tracks[track].slots[slot] = {"mix": mix, "mix_real": mix, "on": True}
        self.touch(track)


def install(project):
    options = project.options
    fake = FakeMixer(project, options.get("inserts", 17), options.get("dynamic", True))
    project.mixer = fake
    project.refreshers.append(fake.refresh)
    m = project.module("mixer")
    T = fake.tracks

    def get(track, attr, default=0):
        return getattr(T[track], attr) if fake.valid(track) else default

    def put(track, attr, value):
        if fake.valid(track) and getattr(T[track], attr) != value:
            setattr(T[track], attr, value)
            fake.touch(track)

    m.trackCount = lambda: len(T) if fake.dynamic else len(T) + 1
    m.getTrackInfo = lambda mode: {0: 0, 1: 1, 2: 500 if fake.dynamic else fake.inserts,
                                   3: fake.current}[mode]
    m.trackNumber = lambda: fake.selected

    m.getTrackVolume = lambda i, mode=0: get(i, "vol") / VOL_STEPS
    m.setTrackVolume = lambda i, v, pickupMode=0: put(i, "vol", int(round(v * VOL_STEPS)))
    m.getTrackPan = lambda i: get(i, "pan") / PAN_STEPS
    m.setTrackPan = lambda i, p, pickupMode=0: put(i, "pan", int(round(p * PAN_STEPS)))
    m.isTrackMuted = lambda i: bool(get(i, "mute", False))

    def mute(i, value=-1):
        if fake.valid(i):
            put(i, "mute_real", (not T[i].mute_real) if value == -1 else bool(value))
    m.muteTrack = mute

    def get_name(i):
        if not fake.valid(i):
            return ""
        return T[i].name or ("Master" if i == 0 else "Insert %d" % i)
    m.getTrackName = get_name
    m.setTrackName = lambda i, name: put(i, "name", name)
    m.getTrackColor = lambda i: get(i, "color")
    m.setTrackColor = lambda i, color: put(i, "color", color)
    m.getTrackStereoSep = lambda i: get(i, "sep", 0.0)
    m.setTrackStereoSep = lambda i, sep, pickupMode=0: put(i, "sep", round(sep, 4))

    m.getEqBandCount = lambda: EQ_BANDS

    def eq_get(which):
        def getter(i, band, mode=0):
            return T[i].eq[band][which] if fake.valid(i) and 0 <= band < EQ_BANDS else 0.0
        return getter

    def eq_set(which):
        def setter(i, band, value):
            if fake.valid(i) and 0 <= band < EQ_BANDS and T[i].eq[band][which] != round(value, 4):
                T[i].eq[band][which] = round(value, 4)
                fake.touch(i)
        return setter
    m.getEqGain, m.setEqGain = eq_get(0), eq_set(0)
    m.getEqFrequency, m.setEqFrequency = eq_get(1), eq_set(1)
    m.getEqBandwidth, m.setEqBandwidth = eq_get(2), eq_set(2)

    m.isTrackSlotsEnabled = lambda i: bool(get(i, "slots_on", False))
    m.enableTrackSlots = lambda i, value=False: put(i, "slots_on", bool(value))

    def slot(i, s):
        if fake.valid(i) and s in T[i].slots:
            return T[i].slots[s]
        fake.project.bad(("slot", i, s))
        return None
    m.isTrackPluginValid = lambda i, s: fake.valid(i) and s in T[i].slots
    # Like FL 2025.2.4: the mix level reads back only after a refresh; the
    # "mute state" reads 1 for on (also for empty slots), and the setter can
    # only switch a slot off.
    m.getPluginMixLevel = lambda i, s: (slot(i, s) or {"mix": 0.0})["mix"]
    m.getPluginMuteState = lambda i, s: int(T[i].slots.get(s, {"on": True})["on"]) if fake.valid(i) else 1

    def set_slot(i, s, field, value):
        entry = slot(i, s)
        if entry is not None and entry[field] != value:
            entry[field] = value
            fake.touch(i)
    m.setPluginMixLevel = lambda i, s, level: set_slot(i, s, "mix_real", round(level, 4))
    m.setPluginMuteState = lambda i, s, value: set_slot(i, s, "on", False)

    def route_active(i, dest):
        return fake.valid(i) and fake.valid(dest) and dest in T[i].routes

    def set_route(i, dest, value, updateUI=False):
        if not (fake.valid(i) and fake.valid(dest)):
            return
        if value and dest not in T[i].routes:
            T[i].routes[dest] = 1.0
            fake.touch(i)
        elif not value and dest in T[i].routes:
            del T[i].routes[dest]
            fake.touch(i)

    def route_level(i, dest):
        return T[i].routes.get(dest, 0.0) if fake.valid(i) and fake.valid(dest) else 0.0

    def set_route_level(i, dest, level):
        if route_active(i, dest) and T[i].routes[dest] != round(level, 4):
            T[i].routes[dest] = round(level, 4)
            fake.touch(i)
    m.getRouteSendActive = route_active
    m.setRouteTo = set_route
    m.getRouteToLevel = route_level
    m.setRouteToLevel = set_route_level
    m.afterRoutingChanged = lambda: None

    # Measured in FL 2025.2.4: getCurrentTempo() is BPM * 1000 as an int and
    # getCurrentTempo(True) the BPM as a float (the docs say the opposite), and
    # setCurrentTempo() raises "Range error" whatever it's given. The tempo is
    # set through general.processRECEvent(midi.REC_Tempo, ...) instead.
    m.getCurrentTempo = lambda asInt=False: fake.tempo if asInt else int(round(fake.tempo * 1000))

    def set_tempo(*args, **kwargs):
        raise RuntimeError("Range error")
    m.setCurrentTempo = set_tempo
