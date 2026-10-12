"""The plugin parameter bindings (pc/, pm/) against fake plugins, and through
the real sync client with a fake link in place of the bridge."""

import sys
import unittest

from fake_fl import FakeFL
from fake_plugins import native, vst

SERUM = ["Master Volume", "A Octave", "A Fine", "Cutoff", "Resonance", "", "Drive",
         "Env1 Attack", "Macro 1", "Macro 2", "Macro 3", "Macro 4"]
LIMITER = ["Gain", "Saturation", "Ceiling", "Attack", "Hold", "Release"]
EMPHASIZER = ["Input gain", "Emphasis", "Clipping", "Level matching", "Bypass", "Output gain"]  # FL 2025


class PluginTest(unittest.TestCase):
    """Serum on channel 2 (global index), Fruity Limiter in slot 0 of Insert 2."""

    def setUp(self):
        self.fl = FakeFL("A")
        self.fake = self.fl.project.plugins
        self.ui = self.fl.project.modules["ui"]
        self.pc, = self.fl.bindings("pc")
        self.pm, = self.fl.bindings("pm")
        self.area = sys.modules[type(self.pc).__module__]
        self.serum = self.fake.add_generator(2, vst("Serum", SERUM))
        self.limiter = self.fake.add_effect(2, 0, native("Fruity Limiter", LIMITER))
        self.listing = 4096 // self.area.NAME_BUDGET + 2  # ticks to list a VST and take its baseline
        self.now = 100.0

    def tearDown(self):
        self.assertEqual(self.fl.project.invalid, [])
        self.fl.close()

    def cycle(self, binding, ticks=1):
        """Drive a binding as the client does each tick, without reading. Returns
        the last tick's units, units to re-read and units to take as baseline."""
        for _ in range(ticks):
            self.now += 0.03
            units = list(binding.units())
            reread, rebaseline = binding.tick(self.now)
        return units, list(reread), list(rebaseline)

    def read_all(self, binding):
        return dict(pair for unit in binding.units() for pair in binding.read(unit))

    def show_group(self, shown):
        """Have the channel rack show only the channels `shown` (global indices)."""
        channels = self.fl.project.modules["channels"]
        count = channels.channelCount
        channels.channelCount = lambda globalCount=False: count(True) if globalCount else len(shown)
        channels.getChannelIndex = lambda index: shown[index]


class FocusTest(PluginTest):
    def test_no_plugin_window_in_front(self):
        for wid in (None, 0, 1, 2, 3, 4):
            self.fake.focus_window(wid)
            self.assertIsNone(self.pc.focused())
            self.assertIsNone(self.pm.focused())

    def test_as_measured_in_fl_2025(self):
        self.fake.focus_window(None)
        self.assertEqual((self.ui.getFocusedFormID(), self.ui.getFocusedPluginName()), (-1, ""))
        self.fake.add_generator(4, native("FLEX", ["Master", "Cutoff"], user_name="FLEX Bass"))
        self.fake.focus_generator(4)
        self.assertEqual((self.ui.getFocusedFormID(), self.ui.getFocusedPluginName()), (4, "FLEX"))
        self.assertIsNone(self.fl.project.modules["mixer"].getActiveEffectIndex())
        self.assertEqual(self.pc.focused(), ((4, -1), "FLEX", 2))
        self.fake.add_effect(0, 9, native("Emphasizer", EMPHASIZER))
        self.fake.focus_effect(0, 9)
        self.assertEqual(self.pm.focused(), ((0, 9), "Emphasizer", 6))

    def test_a_generator_window_is_its_channel(self):
        self.fake.focus_generator(2)
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))  # not the 144 MIDI slots after them
        self.assertIsNone(self.pm.focused())

    def test_an_effect_window_is_its_track_and_slot(self):
        self.fake.focus_effect(2, 0)
        self.assertEqual(self.ui.getFocusedFormID(), 2 * 4194304)
        self.assertEqual(self.pm.focused(), ((2, 0), "Fruity Limiter", 6))
        self.assertIsNone(self.pc.focused())

    def test_an_effect_by_the_active_effect_alone(self):
        self.fake.focus_effect(2, 0)
        self.ui.getFocusedFormID = lambda: -1
        self.assertEqual(self.pm.focused(), ((2, 0), "Fruity Limiter", 6))

    def test_an_effect_by_its_form_id_alone(self):
        self.fl.project.modules["mixer"].getActiveEffectIndex = lambda: None
        self.fake.add_effect(5, 3, native("Fruity Delay 3", ["Wet"]))
        self.fake.focus_effect(5, 3)
        self.assertEqual(self.pm.focused(), ((5, 3), "Fruity Delay 3", 1))

    def test_the_plugin_name_decides_between_active_effect_and_form_id(self):
        self.fake.add_effect(3, 0, native("Fruity Delay 3", ["Wet"]))
        self.fake.focus_effect(2, 0)
        self.fl.project.modules["mixer"].getActiveEffectIndex = lambda: (3, 0)
        self.assertEqual(self.pm.focused(), ((2, 0), "Fruity Limiter", 6))

    def test_windows_without_a_plugin(self):
        self.fake.focus_generator(1)  # a sampler
        self.assertIsNone(self.pc.focused())
        self.fake.focus_effect(3, 4)  # an empty slot
        self.assertIsNone(self.pm.focused())

    def test_form_ids_of_places_that_dont_exist(self):
        self.fake.focus_generator(2)
        self.fake.select(0)
        for form_id in (-1, 4, 999):
            self.ui.getFocusedFormID = lambda form_id=form_id: form_id
            self.assertIsNone(self.pc.focused())
        self.fake.focus_effect(2, 0)
        self.fl.project.modules["mixer"].getActiveEffectIndex = lambda: None
        for form_id in (-1, 99 * 4194304, 2 * 4194304 + 12 * 65536):
            self.ui.getFocusedFormID = lambda form_id=form_id: form_id
            self.assertIsNone(self.pm.focused())

    def test_a_form_id_within_the_channel_group_on_show(self):
        # The rack shows channels 1 and 2, and FL counts the form ID within that
        # group: the focused Serum on channel 2 is number 1 there.
        self.fake.add_generator(1, vst("Sylenth1", ["Cutoff"]))
        self.fake.focus_generator(2)
        self.fake.select(0)
        self.show_group([1, 2])
        self.ui.getFocusedFormID = lambda: 1
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))

    def test_a_global_form_id_while_a_group_is_on_show(self):
        self.fake.add_generator(3, vst("Sylenth1", ["Cutoff"]))
        self.fake.focus_generator(2)
        self.fake.select(0)
        self.show_group([0, 3])  # within this group, 2 doesn't exist
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))
        self.show_group([0, 1, 3])  # here, 2 would be Sylenth1's number in the group
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))

    def test_the_selected_channel_decides_between_two_of_the_same_plugin(self):
        self.fake.add_generator(1, vst("Serum", SERUM))
        self.show_group([0, 2])  # the form ID 1 could be global 1 or, in the group, 2
        for channel in (1, 2):
            self.fake.focus_generator(channel)  # selects the channel
            self.ui.getFocusedFormID = lambda: 1
            self.assertEqual(self.pc.focused(), ((channel, -1), "Serum", 4096))

    def test_the_selected_channel_is_the_last_resort(self):
        self.fake.focus_generator(2)  # selects channel 2
        self.ui.getFocusedFormID = lambda: 77
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))

    def test_fl_may_name_the_focused_plugin_as_the_user_did(self):
        self.serum.user_name = "Bas"
        self.fake.add_generator(3, vst("Sylenth1", ["Cutoff"]))
        self.fake.focus_generator(2)
        self.fake.select(0)
        self.show_group([0, 1, 3])  # in the group, 2 would be Sylenth1
        self.ui.getFocusedPluginName = lambda: "Bas"
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))

    def test_the_form_id_stands_if_no_plugin_has_the_name_fl_gives(self):
        self.fake.focus_generator(2)
        self.ui.getFocusedPluginName = lambda: "Serum (VST3)"
        self.assertEqual(self.pc.focused(), ((2, -1), "Serum", 4096))

    def test_fl_refusing_the_calls_is_said_once_and_harms_nothing(self):
        def refuse(*args, **kwargs):
            raise TypeError("isValid() takes no keyword arguments")
        self.fl.project.modules["plugins"].isValid = refuse
        self.fake.focus_generator(2)
        for _ in range(3):
            self.assertEqual(self.cycle(self.pc)[0], [])
        self.assertEqual(sum("kunde inte läsa vilket plugin" in line for line in self.fl.logs), 1)
        self.assertIsNone(self.pc.write("pc/2/Serum/3", 0.7))


class ListingTest(PluginTest):
    def test_a_vst_is_listed_a_few_slots_per_tick(self):
        names = ["P%d" % i if i % 5 else "" for i in range(300)]  # every fifth slot unnamed
        self.fake.add_generator(3, vst("Diva", names))
        self.fake.focus_generator(3)
        looked_at = []
        for _ in range(self.listing):
            before = self.fake.calls["getParamName"]
            self.cycle(self.pc)
            looked_at.append(self.fake.calls["getParamName"] - before)
        self.assertLessEqual(max(looked_at), self.area.NAME_BUDGET)
        self.assertEqual(sum(looked_at), 4096)  # each parameter slot once, none of the MIDI ones
        sizes = [len(list(self.pc.read(unit))) for unit in self.pc.units()]
        self.assertEqual(sizes[:-1], [self.area.CHUNK] * (len(sizes) - 1))
        self.assertEqual(self.read_all(self.pc), {"pc/3/Diva/%d" % i: 0.5 for i in range(300) if i % 5})

    def test_listed_chunks_become_the_baseline_a_tick_later(self):
        self.fake.focus_effect(2, 0)
        units, _, rebaseline = self.cycle(self.pm)  # focus found, its 6 slots listed
        self.assertEqual((units, rebaseline), ([], []))
        units, _, rebaseline = self.cycle(self.pm)
        self.assertEqual(len(units), 1)
        self.assertEqual(rebaseline, units)
        self.assertEqual(self.cycle(self.pm)[2], [])
        self.assertEqual(self.read_all(self.pm), {"pm/2/0/Fruity Limiter/%d" % p: 0.5 for p in range(6)})

    def test_a_plugin_that_loses_focus_is_not_read_any_more(self):
        self.fake.focus_effect(2, 0)
        old, = self.cycle(self.pm, 2)[0]
        self.fake.add_effect(3, 1, native("Fruity Delay 3", ["Wet", "Dry", "Feedback"]))
        self.fake.focus_effect(3, 1)
        self.assertEqual(self.cycle(self.pm)[0], [])
        self.assertEqual(list(self.pm.read(old)), [])
        units, _, rebaseline = self.cycle(self.pm)
        self.assertEqual(rebaseline, units)
        self.assertEqual(set(self.read_all(self.pm)), {"pm/3/1/Fruity Delay 3/%d" % p for p in range(3)})
        self.fake.focus_window(0)  # the mixer
        self.assertEqual(self.cycle(self.pm)[0], [])
        self.assertIsNone(self.pc.focused())

    def test_another_plugin_in_the_focused_place_is_listed_afresh(self):
        self.fake.focus_effect(2, 0)
        old, = self.cycle(self.pm, 2)[0]
        self.fake.add_effect(2, 0, native("Fruity Compressor", ["Threshold", "Ratio"]))
        self.assertEqual(self.cycle(self.pm)[0], [])
        self.assertEqual(list(self.pm.read(old)), [])
        units, _, rebaseline = self.cycle(self.pm)
        self.assertEqual(rebaseline, units)
        self.assertEqual(set(self.read_all(self.pm)), {"pm/2/0/Fruity Compressor/0", "pm/2/0/Fruity Compressor/1"})

    def test_a_plugin_that_had_focus_before_is_watched_at_once(self):
        self.fake.add_generator(3, vst("Diva", ["P%d" % i for i in range(300)]))  # 19 chunks
        self.fake.focus_generator(3)
        self.cycle(self.pc, self.listing)
        self.fake.focus_generator(2)
        self.cycle(self.pc, 2)
        self.fake.focus_generator(3)
        self.assertEqual(self.cycle(self.pc)[0], [])  # focus found, the first units added
        added = []
        for _ in range(3):
            units, _, rebaseline = self.cycle(self.pc)
            self.assertLessEqual(len(rebaseline), self.area.UNIT_BUDGET)  # each read whole: a few per tick
            added += rebaseline
        self.assertEqual(units, added)
        self.assertEqual(len(units), 19)
        self.cycle(self.pc, self.listing)  # the names, read again, are as before
        self.assertEqual(self.pc.units(), units)

    def test_renamed_parameters_are_noticed_when_the_names_are_read_again(self):
        diva = self.fake.add_generator(3, vst("Diva", ["P%d" % i for i in range(20)]))
        self.fake.focus_generator(3)
        self.cycle(self.pc, self.listing)
        self.fake.focus_window(1)
        self.cycle(self.pc)
        diva.names[5], diva.names[3000] = "", "Macro"
        self.fake.focus_generator(3)
        self.cycle(self.pc, 2)
        self.assertIn("pc/3/Diva/5", self.read_all(self.pc))  # the list from last time, at once
        baseline = []
        for _ in range(self.listing):
            units, _, rebaseline = self.cycle(self.pc)
            baseline += rebaseline
        self.assertLessEqual(set(units), set(baseline))  # listed anew, so taken as the baseline anew
        keys = set(self.read_all(self.pc))
        self.assertNotIn("pc/3/Diva/5", keys)
        self.assertIn("pc/3/Diva/3000", keys)


class HotTest(PluginTest):
    def setUp(self):
        super().setUp()
        self.fake.add_generator(3, vst("Diva", ["P%d" % i for i in range(200)]))  # 13 chunks
        self.fake.focus_generator(3)
        self.cycle(self.pc, self.listing)
        self.units = self.pc.units()
        self.read_all(self.pc)  # the client's baseline

    def test_a_chunk_that_moved_is_reread_every_tick_for_a_while(self):
        chunk = self.units[150 // self.area.CHUNK]
        self.fake.turn(3, 150, 0.9)
        self.assertEqual(self.cycle(self.pc)[1], [])  # nothing has seen it move yet
        self.assertIn(("pc/3/Diva/150", 0.9), list(self.pc.read(chunk)))  # the sweep comes by
        self.assertEqual(self.cycle(self.pc, 10)[1], [chunk])
        self.now += self.area.HOT_TIME
        self.assertEqual(self.cycle(self.pc)[1], [])

    def test_only_a_few_chunks_are_hot_at_once(self):
        for unit in range(len(self.units)):  # a preset change moves everything
            self.fake.turn(3, unit * self.area.CHUNK, 0.9)
        self.read_all(self.pc)
        self.assertEqual(len(self.cycle(self.pc)[1]), self.area.MAX_HOT)

    def test_someone_elses_value_is_no_move_of_the_users(self):
        self.assertEqual(self.pc.write("pc/3/Diva/150", 0.25), 0.25)
        self.read_all(self.pc)
        self.assertEqual(self.cycle(self.pc)[1], [])


class OldFLTest(PluginTest):
    def test_no_plugin_sync_before_api_26(self):
        self.fl.project.modules["general"].getVersion = lambda: 25
        self.assertEqual(self.area.bindings(), [])
        self.assertTrue(any("plugin-parametrar synkas inte" in line for line in self.fl.logs))


class WriteTest(PluginTest):
    def test_a_value_lands_in_the_plugin_the_key_names(self):
        self.assertEqual(self.pm.write("pm/2/0/Fruity Limiter/1", 0.1234567), 0.123)  # FL's plugins keep steps
        self.assertEqual(self.fake.value((2, 0), 1), 0.123)
        self.assertEqual(self.pc.write("pc/2/Serum/3", 0.7), 0.7)  # a 32-bit float, rounded
        self.assertAlmostEqual(self.fake.value(2, 3), 0.7, places=6)
        self.assertEqual(self.pc.write("pc/2/Serum/4", 1.5), 1.0)
        self.assertEqual(self.pc.write("pc/2/Serum/4", 0), 0.0)

    def test_values_for_another_plugin_or_place_change_nothing(self):
        keys = ["pc/2/Sylenth1/3",          # someone else has another plugin there
                "pc/1/Serum/3",             # a sampler channel here
                "pc/9/Serum/3",             # no such channel
                "pm/2/1/Fruity Limiter/1",  # an empty slot
                "pm/99/0/Fruity Limiter/1", "pm/2/10/Fruity Limiter/1",
                "pc/2/Serum/4096",          # past the VST's parameters: its MIDI slots
                "pm/2/0/Fruity Limiter/6", "pc/2/Serum/-1", "pc/02/Serum/3", "pc/x/Serum/3",
                "pc/2/Serum", "pc/2//3", "pc/2/Serum/3/4", "pm/2/Fruity Limiter/1", "m/2/v"]
        for key in keys:
            with self.subTest(key=key):
                binding = self.pm if key.startswith("pm/") else self.pc
                self.assertIsNone(binding.write(key, 0.9))
        for value in ("hög", True, None, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(self.pc.write("pc/2/Serum/3", value))
        self.assertEqual(self.fake.calls["setParamValue"], 0)

    def test_a_slash_in_the_plugin_name(self):
        self.fake.add_effect(1, 0, native("Send/Return", ["Level"]))
        self.assertEqual(self.pm.write("pm/1/0/Send|Return/0", 0.25), 0.25)
        self.fake.focus_effect(1, 0)
        self.cycle(self.pm, 2)
        self.assertEqual(self.read_all(self.pm), {"pm/1/0/Send|Return/0": 0.25})

    def test_a_late_readback_gives_the_value_asked_for(self):
        self.fake.add_effect(3, 0, native("Lazy", ["Mix"], lag=True))
        self.assertEqual(self.pm.write("pm/3/0/Lazy/0", 0.25), 0.25)
        self.assertEqual(self.fake.value((3, 0), 0), 0.5)  # FL still shows the old value
        self.fl.project.refresh()
        self.assertEqual(self.fake.value((3, 0), 0), 0.25)

    def test_a_write_fl_refuses_gives_nothing(self):
        def unsafe(*args, **kwargs):
            raise TypeError("Operation unsafe at current time")
        self.fl.project.modules["plugins"].setParamValue = unsafe
        self.assertIsNone(self.pc.write("pc/2/Serum/3", 0.7))
        self.assertEqual(self.fake.value(2, 3), 0.5)


class DescribeTest(PluginTest):
    def test_plugin_place_and_parameter(self):
        self.assertEqual(self.pc.describe("pc/2/Serum/3"), "Serum (kanal 3) Cutoff")
        self.assertEqual(self.pm.describe("pm/2/0/Fruity Limiter/0"), "Fruity Limiter (Insert 2 slot 1) Gain")
        self.fake.add_effect(0, 9, native("Emphasizer", EMPHASIZER))
        self.assertEqual(self.pm.describe("pm/0/9/Emphasizer/1"), "Emphasizer (Master slot 10) Emphasis")

    def test_without_that_plugin_here(self):
        self.assertEqual(self.pc.describe("pc/2/Sylenth1/3"), "Sylenth1 (kanal 3) parameter 3")
        self.assertEqual(self.pm.describe("pm/7/1/A|B/0"), "A/B (Insert 7 slot 2) parameter 0")
        self.assertEqual(self.pc.describe("pc/x/Serum/3"), "pc/x/Serum/3")


class FakeLink:
    """Stands in for the MIDI link: the test plays the bridge and the relay."""

    def __init__(self):
        self.incoming = []
        self.sent = []

    stats = {"gaps": 0}  # MidiLink's counters the client reads

    def alive(self, now):
        return True

    def forgive(self, now):
        pass

    def can_send(self):
        return True

    def busy(self):
        return False

    def receive(self, status, data1, data2, now):
        return None

    def poll(self):
        messages, self.incoming = self.incoming, []
        return messages

    def send(self, msg):
        self.sent.append(msg)

    def heartbeat(self):
        pass

    def flush(self):
        pass

    def take(self, kind):
        taken = [m for m in self.sent if m.get("t") == kind]
        self.sent = [m for m in self.sent if m.get("t") != kind]
        return taken


class ClientTest(PluginTest):
    """Through the FakeFL's own sync client."""

    def setUp(self):
        super().setUp()
        self.link = FakeLink()
        self.client = self.fl.client
        self.client.link = self.link

    def tick(self, count=1):
        for _ in range(count):
            self.fl.project.refresh()  # FL catching up between two script calls
            self.now += 0.03
            self.client.tick(self.now)

    def join(self, seed=False, state=None, peers=()):
        self.tick()
        self.assertTrue(self.link.take("hello"))
        self.link.incoming.append({"t": "welcome", "proto": self.fl.script.flsync_client.PROTO, "id": "c1", "name": "A", "seed": seed,
                                   "state": state or {}, "peers": list(peers), "s": 0})
        self.tick()

    def sent(self):
        """Plugin values sent since the last call (their batches get acknowledged)."""
        pairs = []
        for batch in self.link.take("sets"):
            pairs += [tuple(pair) for pair in batch["kv"] if pair[0].startswith(("pc/", "pm/"))]
            self.link.incoming.append({"t": "ack", "l": batch["l"], "s": 1})
        return pairs

    def remote(self, *pairs):
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [list(p) for p in pairs], "s": 2})

    def test_turning_a_knob_on_the_focused_plugin_sends_it(self):
        self.join()
        self.fake.focus_generator(2)
        self.tick(self.listing)
        self.assertEqual(self.sent(), [])  # getting focus sends nothing
        for value in (0.2, 0.4, 0.6):
            self.fake.turn(2, 3, value)
            self.tick()
        self.assertEqual(self.sent(), [("pc/2/Serum/3", 0.2), ("pc/2/Serum/3", 0.4), ("pc/2/Serum/3", 0.6)])

    def test_a_knob_on_a_big_plugin_is_followed_every_tick_once_it_moves(self):
        self.fake.add_generator(3, vst("Diva", ["P%d" % i for i in range(1000)]))  # 63 chunks
        self.join()
        self.fake.focus_generator(3)
        self.tick(self.listing)
        self.fake.turn(3, 900, 0.1)
        self.tick(20)  # until the sweep comes by
        self.assertEqual(self.sent(), [("pc/3/Diva/900", 0.1)])
        for value in (0.2, 0.3, 0.4):
            self.fake.turn(3, 900, value)
            self.tick()
        self.assertEqual(self.sent(), [("pc/3/Diva/900", value) for value in (0.2, 0.3, 0.4)])

    def test_remote_values_are_written_and_not_sent_back(self):
        self.join()
        self.fake.focus_generator(2)
        self.tick(self.listing)
        self.remote(("pm/2/0/Fruity Limiter/1", 0.1234567), ("pc/2/Serum/4", 0.125))
        self.tick(30)
        self.assertEqual(self.fake.value((2, 0), 1), 0.123)  # FL keeps it in steps
        self.assertEqual(self.fake.value(2, 4), 0.125)       # the focused plugin, read every tick
        self.assertEqual(self.sent(), [])

    def test_values_for_another_plugin_are_not_written(self):
        self.join()
        self.remote(("pc/2/Sylenth1/4", 0.9), ("pm/2/1/Fruity Limiter/1", 0.9), ("pc/9/Serum/4", 0.9))
        self.tick(5)
        self.assertEqual(self.fake.value(2, 4), 0.5)
        self.assertEqual(self.fake.calls["setParamValue"], 0)

    def test_nothing_is_sent_for_plugins_without_focus(self):
        self.join()
        self.fake.focus_generator(2)
        self.tick(self.listing)
        self.fake.turn((2, 0), 1, 0.9)  # automation, say, in an effect nobody looks at
        self.tick(10)
        self.assertEqual(self.sent(), [])
        self.fake.focus_effect(2, 0)  # what it has when it gets focus is the baseline
        self.tick(5)
        self.assertEqual(self.sent(), [])
        self.fake.turn((2, 0), 1, 0.5)
        self.tick()
        self.assertEqual(self.sent(), [("pm/2/0/Fruity Limiter/1", 0.5)])
        self.fake.turn(2, 3, 0.9)  # the generator has lost focus
        self.tick(10)
        self.assertEqual(self.sent(), [])
        self.fake.focus_generator(2)  # and gets it back: its list is known, the baseline is new
        self.tick(5)
        self.assertEqual(self.sent(), [])
        self.fake.turn(2, 3, 0.1)
        self.tick()
        self.assertEqual(self.sent(), [("pc/2/Serum/3", 0.1)])

    def test_plugin_parameters_are_not_starting_values(self):
        self.fake.focus_generator(2)
        self.tick(self.listing)
        self.assertIn("pc/2/Serum/3", self.client.shadow)
        self.join(seed=True)
        batches = self.link.take("sets")
        keys = [key for batch in batches for key, _ in batch["kv"]]
        self.assertTrue(all(batch.get("seed") for batch in batches))
        self.assertIn("m/0/v", keys)
        self.assertEqual([key for key in keys if key.startswith(("pc/", "pm/"))], [])

    def test_joining_takes_the_session_values_of_plugins_that_match(self):
        self.join(state={"pc/2/Serum/4": 0.75, "pm/2/0/Fruity Limiter/0": 0.25, "pc/2/Sylenth1/6": 0.1})
        self.assertEqual(self.fake.value(2, 4), 0.75)
        self.assertEqual(self.fake.value((2, 0), 0), 0.25)
        self.assertEqual(self.fake.value(2, 6), 0.5)
        self.tick(30)
        self.assertEqual(self.sent(), [])

    def test_others_see_who_changed_what(self):
        self.join(peers=[{"id": "c2", "name": "Kim", "app": "fl"}])
        self.remote(("pm/2/0/Fruity Limiter/0", 0.25))
        self.tick()
        self.assertEqual(self.fl.project.hints[-1], "Kim: Fruity Limiter (Insert 2 slot 1) Gain")

    def test_a_parameter_a_plugin_reads_as_nan_is_never_sent(self):
        # Some buggy VSTs report NaN: it never equals itself, so it would look
        # changed at every read and go out over and over.
        self.join()
        self.fake.focus_effect(2, 0)
        self.tick(5)
        self.limiter.values[1] = self.limiter.shown[1] = float("nan")
        self.limiter.values[2] = self.limiter.shown[2] = float("inf")
        self.tick(60)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.pm.hot, {})  # not a move of the user's either
        self.assertNotIn("pm/2/0/Fruity Limiter/1", dict(self.read_all(self.pm)))
        self.fake.turn((2, 0), 1, 0.25)  # back to a number: that's a change
        self.tick()
        self.assertEqual(self.sent(), [("pm/2/0/Fruity Limiter/1", 0.25)])


if __name__ == "__main__":
    unittest.main()
