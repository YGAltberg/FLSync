"""The channel rack area (keys c/, g/, gp/, pt/): each binding against the fake
channel rack, then end to end through the real sync client, with a small
in-process stand-in for the relay. Step parameters (gp/) have their own tests
in test_step_params.py, which uses the fixtures here.

    python -m unittest discover -s tests -p "test_channels*.py"
"""

import json
import unittest

import fake_channels
from fake_fl import FakeFL

AREA = ("c", "g", "gp", "pt")
STEP_CALLS = ("getGridBit", "setGridBit", "getCurrentStepParam", "setStepParameterByIndex")


def counted(module, name, test):
    """Record the calls to one fake FL function until the test ends."""
    calls = []
    original = getattr(module, name)

    def call(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    setattr(module, name, call)
    test.addCleanup(setattr, module, name, original)
    return calls


def step_calls(fl, test):
    """Record the step sequencer calls each binding of `fl`'s client makes in
    its reads and ticks until the test ends: {prefix: [(function, args)]}.
    Calls outside a binding are under None."""
    calls, current = {}, [None]
    module = fl.project.modules["channels"]
    for name in STEP_CALLS:
        def call(*args, _name=name, _original=getattr(module, name)):
            calls.setdefault(current[0], []).append((_name, args))
            return _original(*args)
        test.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, call)

    def inside(binding, method, consume):
        original = getattr(binding, method)

        def run(*args):
            outer, current[0] = current[0], binding.prefix
            try:
                result = original(*args)
                return list(result) if consume else result  # a read is a generator
            finally:
                current[0] = outer
        setattr(binding, method, run)
        test.addCleanup(delattr, binding, method)
    for binding in fl.client.bindings:
        inside(binding, "read", True)
        inside(binding, "tick", False)
    return calls


class Rack(unittest.TestCase):
    """One simulated FL; the bindings its script made are called directly."""

    def setUp(self):
        self.use(self.new_fl())

    def new_fl(self, **options):
        fl = FakeFL("A", calls_idle=False, **options)
        self.addCleanup(self.finish, fl)
        return fl

    def finish(self, fl):
        self.assertEqual(fl.project.invalid, [], "scriptet rörde kanaler, steg eller mönster som inte finns")
        self.assertEqual(fl.script._errors, 0)
        fl.close()

    def use(self, fl):
        self.fl, self.rack = fl, fl.project.rack
        self.channels = fl.project.modules["channels"]  # FL's API, for doing what a user does
        self.patterns = fl.project.modules["patterns"]
        self.midi = fl.project.modules["midi"]
        (self.c,), (self.g,), (self.gp,), (self.pt,) = (fl.bindings(prefix) for prefix in AREA)


class RackTest(Rack):

    # --- channels -----------------------------------------------------------------

    def test_every_channel_key_reads_what_fl_has(self):
        ch = self.channels
        ch.setChannelVolume(1, 0.5, 0, 1)
        ch.setChannelPan(1, -0.25, 0, 1)
        ch.setChannelPitch(1, 50, 1, 0, 1)
        ch.muteChannel(1, 1, 1)
        ch.setChannelName(1, "Bas", 1)
        ch.setChannelColor(1, -16776961, 1)
        ch.setTargetFxTrack(1, 7, 1)
        self.fl.project.refresh()  # FL shows the mute after its next refresh
        self.assertEqual(dict(self.c.read(1)), {
            "c/1/v": 0.5, "c/1/p": -0.25, "c/1/pi": 50, "c/1/mu": True,
            "c/1/n": "Bas", "c/1/c": -16776961, "c/1/t": 7})
        self.assertEqual(dict(self.c.read(0)), {
            "c/0/v": 0.78125, "c/0/p": 0.0, "c/0/pi": 0, "c/0/mu": False,
            "c/0/n": "Kick", "c/0/c": fake_channels.SAMPLER_COLOR, "c/0/t": 1})
        self.assertEqual(list(self.c.units()), [0, 1, 2, 3])

    def test_writes_return_what_fl_holds(self):
        c = self.c
        self.assertEqual(c.write("c/2/v", 0.50000001), 0.5)  # FL's resolution
        self.assertEqual(c.write("c/2/p", -2), -1.0)
        self.assertEqual(c.write("c/2/pi", 500), 200)        # the channel's pitch range: 2 semitones
        self.assertEqual(c.write("c/2/pi", -12.4), -12)
        self.assertEqual(c.write("c/2/n", "Hihat"), "Hihat")
        self.assertEqual(c.write("c/2/n", ""), "Hat")         # FL goes back to the sample's name
        self.assertEqual(c.write("c/2/c", -16711936), -16711936)
        self.assertEqual(c.write("c/2/t", 9), 9)
        hat = self.rack.channels[2]
        self.assertEqual((hat.vol, hat.pan, hat.pitch, hat.color, hat.target), (6400, -6400, -12, -16711936, 9))

    def test_mute_is_trusted_although_fl_shows_it_late(self):
        self.assertIs(self.c.write("c/3/mu", True), True)
        self.assertFalse(self.channels.isChannelMuted(3, 1))  # not yet
        self.fl.project.refresh()
        self.assertIs(dict(self.c.read(3))["c/3/mu"], True)
        for _ in range(2):  # sets the mute, never toggles it
            self.assertIs(self.c.write("c/3/mu", False), False)
        self.assertFalse(self.rack.channels[3].mute_real)

    def test_what_this_fl_cannot_take_is_refused_without_touching_fl(self):
        before = self.rack.snapshot()
        refused = [("c/4/v", 0.5), ("c/x/v", 0.5), ("c/01/v", 0.5), ("c/-1/v", 0.5), ("c/1/zz", 1),
                   ("c/1", 0.5), ("c/1/v/x", 0.5), ("c/1/v", "hög"), ("c/1/v", True), ("c/1/v", None),
                   ("c/1/mu", "ja"), ("c/1/n", 5), ("c/1/c", 1.5), ("c/1/c", 2 ** 40), ("c/1/t", 99),
                   ("c/1/t", -1), ("c/1/t", "1"), ("g/1/4", "1"), ("g/0/1", "1"), ("g/1000/1", "1"),
                   ("g/1/1", "10a1"), ("g/1/1", 1), ("g/1", "1"), ("g/1/x", "1"), ("pt/0/n", "x"),
                   ("pt/1000/n", "x"), ("pt/1/n", 5), ("pt/1/c", "röd"), ("pt/1/x", 1), (5, 1)]
        bindings = {"c": self.c, "g": self.g, "pt": self.pt}
        for key, value in refused:
            with self.subTest(key=key, value=value):
                binding = bindings.get(str(key).split("/")[0], self.c)
                self.assertIsNone(binding.write(key, value))
        self.assertEqual(self.rack.snapshot(), before)
        self.assertEqual(self.g.waiting, {})

    def test_a_group_filter_does_not_move_the_keys(self):
        for index in (2, 3):
            self.rack.channels[index].group = "Trummor"
        self.rack.show_group("Trummor")  # the channel rack now shows Hat and Snare
        self.assertEqual(self.channels.channelCount(), 2)
        self.assertEqual(list(self.c.units()), [0, 1, 2, 3])
        self.assertEqual([channel for _, channel in self.g.units()], [0, 1, 2, 3])
        self.assertEqual(dict(self.c.read(3))["c/3/n"], "Snare")
        self.assertEqual(self.c.write("c/3/v", 0.25), 0.25)
        self.assertEqual(self.rack.channels[3].vol, 3200)
        self.assertEqual(self.c.write("c/0/n", "Bas"), "Bas")  # not on show, still there
        self.assertEqual(self.rack.channels[0].name, "Bas")
        self.assertEqual(self.g.write("g/1/3", "1"), "1")
        self.assertEqual(self.rack.row(3), "1")

    def test_a_reported_channel_is_read_whichever_way_fl_counts_it(self):
        self.rack.channels[3].group = "Trummor"
        self.rack.show_group("Trummor")
        CE = self.midi
        self.assertEqual(set(self.c.on_dirty_channel(0, CE.CE_Rename)), {0, 3})  # global 0, or the group's first
        self.assertEqual(set(self.c.on_dirty_channel(2, CE.CE_Rename)), {2})     # past the group: global only
        self.assertEqual(self.c.on_dirty_channel(1, CE.CE_Select), ())

    def test_moved_channels_are_taken_as_they_are(self):
        self.assertEqual(self.c.tick(0), ((), ()))
        self.rack.add_channel(1, "Bas")  # Clap, Hat and Snare get other numbers
        self.assertEqual(list(self.c.tick(0)[1]), [0, 1, 2, 3, 4])
        self.assertEqual(self.c.tick(0), ((), ()))
        for index, flag in ((-1, self.midi.CE_Rename), (2, self.midi.CE_Replace)):
            self.assertEqual(self.c.on_dirty_channel(index, flag), ())
            self.assertEqual(list(self.c.tick(0)[1]), [0, 1, 2, 3, 4])

    def test_mixer_tracks_with_other_numbers_are_taken_as_they_are(self):
        # FL 2025+ can delete an insert: the channels sent to the ones after it follow them down.
        self.assertEqual(self.c.tick(0), ((), ()))
        del self.fl.mixer.tracks[2]
        for channel in self.rack.channels:  # Kick -> 1, Clap -> 2 (gone), Hat -> 3, Snare -> 4
            channel.target = 0 if channel.target == 2 else channel.target - (channel.target > 2)
        self.assertEqual(list(self.c.tick(0)[1]), [0, 1, 2, 3])
        self.assertEqual(self.c.tick(0), ((), ()))
        self.fl.mixer.tracks.insert(1, type(self.fl.mixer.tracks[1])(1))  # and add one
        self.assertEqual(list(self.c.tick(0)[1]), [0, 1, 2, 3])
        self.c.on_project_load()
        self.assertEqual(self.c.tick(0), ((), ()))

    def test_descriptions(self):
        self.channels.setChannelName(1, "Bas", 1)
        self.patterns.setPatternName(2, "Vers")
        expected = {
            self.c: {"c/0/v": "Kanal 1 (Kick) volym", "c/1/pi": "Kanal 2 (Bas) tonhöjd",
                     "c/3/mu": "Kanal 4 (Snare) mute", "c/2/t": "Kanal 3 (Hat) mixerspår",
                     "c/0/p": "Kanal 1 (Kick) pan", "c/0/n": "Kanal 1 (Kick) namn",
                     "c/0/c": "Kanal 1 (Kick) färg", "c/9/v": "Kanal 10 volym", "c/x/v": "c/x/v"},
            self.g: {"g/1/0": "Kick steg i mönster 1", "g/3/9": "Kanal 10 steg i mönster 3", "g/1": "g/1"},
            self.pt: {"pt/1/n": "Mönster 1 namn", "pt/2/c": "Mönster 2 (Vers) färg", "pt/2/x": "pt/2/x"},
        }
        for binding, texts in expected.items():
            for key, text in texts.items():
                self.assertEqual(binding.describe(key), text)

    # --- the step sequencer ---------------------------------------------------------

    def test_a_row_is_the_steps_of_the_current_pattern(self):
        for step in (0, 4, 8, 12):
            self.channels.setGridBit(0, step, 1, 1)
        self.assertEqual(dict(self.g.read((1, 0))), {"g/1/0": "1000100010001"})
        self.assertEqual(dict(self.g.read((1, 1))), {"g/1/1": ""})
        self.assertEqual(self.g.units(), [(1, 0), (1, 1), (1, 2), (1, 3)])
        self.patterns.jumpToPattern(2)
        self.assertEqual(dict(self.g.read((2, 0))), {"g/2/0": ""})
        self.assertEqual(dict(self.g.read((1, 0))), {})  # FL can't show pattern 1 now

    def test_a_longer_pattern_changes_no_other_row(self):
        self.channels.setGridBit(1, 2, 1, 1)
        self.channels.setGridBit(0, 20, 1, 1)  # the pattern grows to two bars
        self.assertEqual(self.patterns.getPatternLength(1), 32)
        self.assertEqual(dict(self.g.read((1, 1))), {"g/1/1": "001"})
        self.assertEqual(dict(self.g.read((1, 0))), {"g/1/0": "0" * 20 + "1"})

    def test_writing_a_row_changes_only_the_steps_that_differ(self):
        self.channels.setGridBit(2, 1, 1, 1)
        self.channels.setGridBit(2, 5, 1, 1)
        sets = counted(self.channels, "setGridBit", self)
        self.assertEqual(self.g.write("g/1/2", "0110"), "011")
        self.assertEqual(sets, [(2, 2, 1, 1), (2, 5, 0, 1)])
        # A step past the pattern's end makes the pattern longer, as clicking there would.
        self.assertEqual(self.g.write("g/1/2", "0" * 31 + "1"), "0" * 31 + "1")
        self.assertEqual(self.patterns.getPatternLength(1), 32)

    def test_rows_for_another_pattern_wait_until_the_user_switches_to_it(self):
        self.assertEqual(self.g.write("g/2/1", "1010"), "101")
        self.assertEqual(self.rack.row(1, 2), "")  # FL can't reach pattern 2 now
        self.assertEqual(self.rack.row(1, 1), "")  # and pattern 1 is left alone
        self.assertEqual(list(self.g.tick(0)[1]), [])
        self.patterns.jumpToPattern(2)
        reread, rebaseline = self.g.tick(0)
        self.assertEqual(rebaseline, [(2, 0), (2, 1), (2, 2), (2, 3)])
        self.assertEqual(self.rack.row(1, 2), "101")
        # Put in once: switching away and back leaves the user's own edit alone.
        self.channels.setGridBit(1, 0, 0, 1)
        for number in (1, 2):
            self.patterns.jumpToPattern(number)
            self.g.tick(0)
        self.assertEqual(self.rack.row(1, 2), "001")

    def test_the_newest_waiting_row_wins(self):
        self.g.write("g/2/1", "1")
        self.g.write("g/2/1", "01")
        self.patterns.jumpToPattern(2)
        self.g.tick(0)
        self.assertEqual(self.rack.row(1, 2), "01")

    def test_a_row_written_directly_replaces_the_waiting_one(self):
        self.g.write("g/2/1", "1")  # waits: pattern 1 is current
        self.patterns.jumpToPattern(2)
        self.assertEqual(self.g.write("g/2/1", "01"), "01")  # now current, before any tick
        self.g.tick(0)
        self.assertEqual(self.rack.row(1, 2), "01")

    def test_waiting_rows_wait_while_fl_is_not_safe_to_edit(self):
        self.g.write("g/2/1", "1")
        self.patterns.jumpToPattern(2)
        self.fl.safe = False
        self.assertEqual(self.g.tick(0)[1], [(2, 0), (2, 1), (2, 2), (2, 3)])  # taken as they are
        self.assertEqual(self.rack.row(1, 2), "")
        self.fl.safe = True
        self.assertEqual(self.g.tick(0)[1], [(2, 1)])  # put in now, and only that row taken again
        self.assertEqual(self.rack.row(1, 2), "1")

    def test_waiting_rows_are_dropped_when_the_channels_get_other_numbers(self):
        self.g.write("g/2/1", "1")
        self.rack.add_channel(0, "Bas")  # Clap is channel 2 now
        self.g.tick(0)
        self.patterns.jumpToPattern(2)
        self.g.tick(0)
        self.assertEqual([self.rack.row(channel, 2) for channel in range(5)], [""] * 5)

    def test_a_loaded_project_starts_afresh(self):
        self.g.write("g/2/1", "1")  # someone's row for the old project's pattern 2
        self.pt.write("pt/9/c", -65536)
        client, midi = self.fl.client, self.midi
        client.project_load(midi.PL_Start)
        self.rack.channels.append(fake_channels.Channel("Bas"))  # the new project: five channels,
        self.rack.patterns.clear()                                 # no pattern 9,
        self.rack.current = 2                                      # pattern 2 on show
        client.project_load(midi.PL_LoadOk)
        self.assertEqual(self.g.waiting, {})
        self.assertEqual(list(self.pt.units()), [1, 2])
        for binding in (self.c, self.g, self.pt):  # the client took the new baseline itself
            self.assertEqual(list(binding.tick(0)[1]), [])
        self.assertEqual(self.rack.row(1, 2), "")

    def test_clip_channels_have_no_row(self):
        self.rack.channels[3].kind = self.midi.CT_AutoClip
        self.assertEqual(dict(self.g.read((1, 3))), {})
        self.assertIsNone(self.g.write("g/1/3", "1"))
        self.assertIsNone(self.g.write("g/2/3", "1"))
        self.assertEqual(dict(self.g.read((1, 2))), {"g/1/2": ""})

    def test_the_grid_reads_a_bounded_number_of_steps_per_tick(self):
        self.use(self.new_fl(channels=40))
        self.channels.setGridBit(0, 63, 1, 1)  # four bars: every row is 64 steps
        calls = step_calls(self.fl, self)
        client, now = self.fl.client, 100.0
        for _ in range(5):
            calls.clear()
            now += 0.05
            client.tick(now)
            self.assertLessEqual(len(calls.get("g", ())), max(self.g.sweep_calls, 64))
        client.on_refresh(self.midi.HW_Dirty_Patterns)  # a step changed, FL doesn't say where
        seen = set()
        for _ in range(40 * 64 // self.g.burst_calls):
            calls.clear()
            now += 0.05
            client.tick(now)
            self.assertLessEqual(len(calls.get("g", ())), self.g.burst_calls)
            seen.update(args[0] for _, args in calls.get("g", ()))
        self.assertEqual(seen, set(range(40)))

    # --- patterns -------------------------------------------------------------------

    def test_pattern_names_and_colors(self):
        self.assertEqual(dict(self.pt.read(1)), {"pt/1/n": "Pattern 1", "pt/1/c": fake_channels.PATTERN_COLOR})
        self.assertEqual(self.pt.write("pt/1/n", "Vers"), "Vers")
        self.assertEqual(self.pt.write("pt/1/c", -65536), -65536)
        self.assertEqual(dict(self.pt.read(1)), {"pt/1/n": "Vers", "pt/1/c": -65536})
        self.assertEqual(self.pt.write("pt/1/n", "Pattern 1"), "Pattern 1")  # FL's own name: left unset
        self.assertEqual(self.rack.patterns[1].name, "")

    def test_the_patterns_looked_at_are_the_used_ones_and_the_current_one(self):
        self.assertEqual(list(self.pt.units()), [1])  # a new project: patternCount() is 0
        self.channels.setGridBit(0, 0, 1, 1)          # pattern 1 gets a step
        self.patterns.setPatternName(5, "Brygga")     # and 5 a name; 2-4 stay untouched
        self.assertEqual(self.patterns.patternCount(), 2)
        self.assertEqual(list(self.pt.units()), [1, 2, 3, 4, 5])
        self.patterns.jumpToPattern(7)
        self.assertEqual(list(self.pt.units()), list(range(1, 8)))
        self.assertEqual(self.pt.write("pt/9/c", -65536), -65536)  # someone else's pattern 9
        self.assertEqual(list(self.pt.units()), list(range(1, 10)))

    def test_patterns_not_looked_at_before_are_taken_as_they_are(self):
        self.assertEqual(list(self.pt.tick(0)[1]), [])
        self.patterns.jumpToPattern(3)
        self.assertEqual(list(self.pt.tick(0)[1]), [2, 3])
        self.assertEqual(list(self.pt.tick(0)[1]), [])
        self.patterns.setPatternName(1, "Vers")  # a first change is not new structure
        self.assertEqual(list(self.pt.tick(0)[1]), [])


# --- end to end ----------------------------------------------------------------------

class MiniRelay:
    """relay/relay.py's protocol in-process, just enough for these tests:
    welcome (the first FL shares its project), batches answered with keep and
    ack and passed on to the others, sync/state. It speaks the protocol of the
    script that connects (flsync_client.PROTO) and ignores structure
    fingerprints: its welcome says `fp` null, so no FL ever pauses."""

    def __init__(self):
        self.links = []
        self.state = {}
        self.touched = set()  # keys set by normal pairs: starting values don't overwrite them
        self.seq = 0
        self.seeder = None
        self.changes = []     # (sender, key, value) of every normal pair
        self.batches = []     # (sender, keys) of every batch, in the relay's order

    def connect(self, name, proto):
        link = RelayLink(self, name, "c%d" % (len(self.links) + 1), proto)
        self.links.append(link)
        return link

    def handle(self, link, msg):
        kind = msg.get("t")
        if kind == "hello":
            seed = self.seeder is None
            self.seeder = self.seeder or link
            peers = [{"id": other.id, "name": other.name, "app": "fl"} for other in self.links if other.joined]
            link.joined = True
            link.deliver({"t": "welcome", "proto": link.proto, "id": link.id, "name": link.name,
                          "seed": seed, "state": dict(self.state), "peers": peers, "s": self.seq,
                          "fp": None, "snapshot": None})
            self.others(link, {"t": "peer_join", "id": link.id, "name": link.name, "app": "fl"})
        elif kind == "sets":
            self.batch(link, msg)
        elif kind == "sync":
            link.deliver({"t": "state", "state": dict(self.state), "s": self.seq})

    def batch(self, link, msg):
        seed, kept, accepted = msg.get("seed"), [], []
        self.batches.append((link.name, [key for key, _ in msg["kv"]]))
        for key, value in msg["kv"]:
            if seed and key in self.touched:
                kept.append([key, self.state[key]])
                continue
            if not seed:
                self.touched.add(key)
                self.changes.append((link.name, key, value))
            self.state[key] = value
            self.seq += 1
            accepted.append([key, value])
        if kept:
            link.deliver({"t": "keep", "l": msg["l"], "kv": kept})
        link.deliver({"t": "ack", "l": msg["l"], "s": self.seq})
        if accepted:
            self.others(link, {"t": "sets", "from": link.id, "kv": accepted, "s": self.seq})

    def others(self, link, msg):
        for other in self.links:
            if other is not link and other.joined:
                other.deliver(msg)

    def sent_by(self, name):
        """The changes (not starting values) `name` sent, as (key, value)."""
        return [(key, value) for sender, key, value in self.changes if sender == name]


class RelayLink:
    """What the sync client uses of its link (flsync_link.MidiLink), wired
    straight to MiniRelay. Messages go through JSON, as on the wire."""

    def __init__(self, relay, name, ident, proto):
        self.relay, self.name, self.id, self.proto = relay, name, ident, proto
        self.joined = False
        self.incoming = []

    def deliver(self, msg):
        self.incoming.append(json.loads(json.dumps(msg)))

    def alive(self, now):
        return True

    def can_send(self):
        return True

    def busy(self):
        return False

    def poll(self):
        messages, self.incoming = self.incoming, []
        return messages

    def send(self, msg):
        self.relay.handle(self, json.loads(json.dumps(msg)))

    def heartbeat(self):
        pass

    def flush(self):
        pass

    def receive(self, status, data1, data2, now):
        return None


class Session(unittest.TestCase):
    """FLs whose sync clients (with this area's bindings) talk through
    MiniRelay. Time is simulated: a tick is 50 ms."""

    def setUp(self):
        self.relay = MiniRelay()
        self.members = {}  # name -> (fl, client)
        self.now = 1000.0

    def tearDown(self):
        for name, (fl, _) in self.members.items():
            self.assertEqual(fl.project.invalid, [], name)
            fl.close()

    def join(self, name, **options):
        fl = FakeFL(name, calls_idle=False, **options)
        # New bindings, made as the script makes them (the grid and its step parameters share state).
        bindings = [b for b in fl.script.make_bindings() if b.prefix in AREA]
        link = self.relay.connect(name, fl.script.flsync_client.PROTO)
        client = fl.script.SyncClient(name, bindings, link, hint=fl.project.hints.append)
        client.start()
        self.members[name] = (fl, client)
        return fl

    def session(self, **options):
        a = self.join("A", **options)
        self.until(lambda: self.joined("A"))
        b = self.join("B", **options)
        self.until(lambda: self.joined("B"))
        return a, b

    def joined(self, name):
        client = self.members[name][1]
        return client.joined and not client.pending

    def step(self, ticks=1):
        """Let every FL run: FL reports what changed, then its client ticks."""
        for _ in range(ticks):
            self.now += 0.05
            for fl, client in self.members.values():
                project = fl.project
                project.refresh()
                project.dirty_mixer.clear()
                dirty, project.dirty_channels = project.dirty_channels, []
                for index, flag in dirty:
                    client.on_dirty_channel(index, flag)
                flags, project.refresh_flags = project.refresh_flags, []
                for flag in flags:
                    client.on_refresh(flag)
                client.tick(self.now)

    def until(self, condition, ticks=200):
        for _ in range(ticks):
            if condition():
                return
            self.step()
        self.fail("tog för lång tid")

    @staticmethod
    def api(fl):
        return fl.project.modules["channels"], fl.project.modules["patterns"]


class SessionTest(Session):

    def test_the_first_fl_shares_its_rack_and_the_next_one_gets_it(self):
        a = self.join("A")
        ch, pat = self.api(a)
        ch.setChannelVolume(1, 0.5, 0, 1)
        ch.setChannelName(2, "Hihat", 1)
        ch.setGridBit(0, 4, 1, 1)
        pat.setPatternName(1, "Vers")
        self.until(lambda: self.joined("A"))
        b = self.join("B")
        self.until(lambda: b.project.rack.snapshot() == a.project.rack.snapshot())
        self.step(20)
        self.assertEqual(self.relay.state["g/1/0"], "00001")
        self.assertEqual(self.relay.sent_by("A") + self.relay.sent_by("B"), [])  # starting values only

    def test_changes_go_both_ways_and_are_not_sent_back(self):
        a, b = self.session()
        self.api(a)[0].setChannelPan(3, 0.5, 0, 1)
        self.api(b)[0].setChannelColor(0, -16711936, 1)
        self.until(lambda: b.project.rack.channels[3].pan == 3200
                   and a.project.rack.channels[0].color == -16711936)
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("c/3/p", 0.5)])
        self.assertEqual(self.relay.sent_by("B"), [("c/0/c", -16711936)])

    def test_a_remote_mute_is_not_sent_back(self):
        a, b = self.session()
        self.api(a)[0].muteChannel(2, 1, 1)
        self.until(lambda: b.project.rack.channels[2].mute)  # what FL shows, after its refresh
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_steps_in_another_pattern_wait_for_the_switch_and_are_not_sent_back(self):
        a, b = self.session()
        ch, pat = self.api(a)
        pat.jumpToPattern(2)
        self.step()  # a user can't switch and click in the same tick
        ch.setGridBit(1, 3, 1, 1)
        self.until(lambda: self.relay.sent_by("A"))
        self.step(10)
        self.assertEqual(b.project.rack.row(1, 2), "")  # B is on pattern 1: the row waits
        self.api(b)[1].jumpToPattern(2)
        self.until(lambda: b.project.rack.row(1, 2) == "0001")
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("g/2/1", "0001"),  # switching sent nothing
                                                   ("gp/2/1", "3:60,100,64,120,64,128,128,0")])
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_a_joining_fl_gets_the_rows_of_every_pattern(self):
        a = self.join("A")
        self.until(lambda: self.joined("A"))
        ch, pat = self.api(a)
        ch.setGridBit(0, 0, 1, 1)
        self.step()
        pat.jumpToPattern(2)
        self.step()
        ch.setGridBit(1, 1, 1, 1)
        self.until(lambda: self.relay.state.get("g/2/1") == "01")
        b = self.join("B")
        self.until(lambda: self.joined("B") and b.project.rack.row(0, 1) == "1")
        self.assertEqual(b.project.rack.row(1, 2), "")
        self.api(b)[1].jumpToPattern(2)
        self.until(lambda: b.project.rack.row(1, 2) == "01")
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_a_group_filter_does_not_shift_the_keys(self):
        a, b = self.session()
        for index in (2, 3):
            b.project.rack.channels[index].group = "Trummor"
        b.project.rack.show_group("Trummor")
        self.api(a)[0].setChannelVolume(3, 0.25, 0, 1)
        self.until(lambda: b.project.rack.channels[3].vol == 3200)
        self.api(b)[0].setChannelPan(0, -0.5)  # B's user turns the first channel on show: Hat
        self.until(lambda: a.project.rack.channels[2].pan == -3200)
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [("c/2/p", -0.5)])

    def test_a_new_channel_does_not_rename_the_other_fls_channels(self):
        a, b = self.session()
        before = b.project.rack.snapshot()
        a.project.rack.add_channel(1, "Bas")  # Clap, Hat and Snare get other numbers
        self.step(40)
        self.assertEqual(self.relay.sent_by("A"), [])
        self.assertEqual(b.project.rack.snapshot(), before)
        self.api(a)[0].setChannelVolume(4, 0.5, 0, 1)  # later changes go out as usual
        self.until(lambda: self.relay.sent_by("A") == [("c/4/v", 0.5)])

    def test_a_change_fl_did_not_report_is_found_by_the_sweep(self):
        a, b = self.session(channels=12)
        a.project.rack.channels[9].steps[1] = {2}  # FL said nothing about these
        a.project.rack.channels[11].vol = 6400
        self.until(lambda: b.project.rack.row(9) == "001" and b.project.rack.channels[11].vol == 6400)

    def test_renaming_the_first_pattern_of_a_new_project_reaches_the_others(self):
        a, b = self.session()
        self.api(a)[1].setPatternName(1, "Vers")  # pattern 1 gets its first change
        self.until(lambda: self.api(b)[1].getPatternName(1) == "Vers")
        self.api(a)[1].jumpToPattern(4)  # a pattern nobody used yet: nothing to tell
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("pt/1/n", "Vers")])

    def test_the_hint_says_who_changed_what(self):
        a, b = self.session()
        ch = self.api(a)[0]
        ch.setChannelVolume(0, 0.5, 0, 1)
        self.until(lambda: "A: Kanal 1 (Kick) volym" in b.project.hints)
        ch.setGridBit(1, 0, 1, 1)
        self.until(lambda: "A: Clap steg i mönster 1" in b.project.hints)


if __name__ == "__main__":
    unittest.main()
