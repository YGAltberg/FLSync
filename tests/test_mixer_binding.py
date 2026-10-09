"""The mixer bindings (m/, mx/, mr/) against the fake FL mixer."""

import unittest

from fake_fl import FakeFL


class MixerBindingTest(unittest.TestCase):
    def setUp(self):
        self.fl = FakeFL("A")
        self.m, = self.fl.bindings("m")
        self.mx, = self.fl.bindings("mx")
        self.mr, = self.fl.bindings("mr")

    def tearDown(self):
        self.assertEqual(self.fl.project.invalid, [])
        self.fl.close()

    def test_a_track_reads_all_its_settings(self):
        keys = dict(self.m.read(3))
        expected = {"m/3/" + p for p in ("v", "p", "mu", "n", "c", "s", "fx")}
        expected |= {"m/3/e%d%s" % (b, w) for b in range(3) for w in "gfw"}
        self.assertEqual(set(keys), expected)
        self.assertEqual(keys["m/3/v"], 0.8)
        self.assertEqual(keys["m/3/n"], "Insert 3")

    def test_writes_land_in_fl_and_return_what_fl_holds(self):
        cases = [("m/3/v", 0.123456789, 0.123438), ("m/3/p", -0.5, -0.5), ("m/3/n", "Bas", "Bas"),
                 ("m/3/c", 255, 255), ("m/3/s", 0.25, 0.25), ("m/3/e1g", 0.7, 0.7),
                 ("m/3/e2f", 0.9, 0.9), ("m/3/e0w", 0.1, 0.1), ("m/3/fx", False, False)]
        for key, value, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(self.m.write(key, value), expected)
                self.assertEqual(dict(self.m.read(3))[key], expected)

    def test_mute_trusts_the_requested_value(self):
        self.assertTrue(self.m.write("m/3/mu", True))
        self.assertFalse(self.fl.muted(3))  # FL shows it only after its next refresh
        self.fl.project.refresh()
        self.assertTrue(self.fl.muted(3))

    def test_keys_this_fl_does_not_have(self):
        for key, value in [("m/99/v", 0.5), ("m/3/e7g", 0.5), ("m/3/zz", 1), ("m/x/v", 1),
                           ("m/3/v", "hög"), ("mx/3/0/m", 0.5), ("mx/3/12/m", 0.5), ("mr/99", ""),
                           ("mr/3", "nonsens"), ("mr/3", 5)]:
            with self.subTest(key=key):
                binding = {"m": self.m, "mx": self.mx, "mr": self.mr}[key.split("/")[0]]
                self.assertIsNone(binding.write(key, value))

    def test_effect_slot_mix_appears_with_its_plugin(self):
        self.assertEqual(list(self.mx.read(2)), [])
        self.fl.mixer.add_plugin(2, 1, mix=0.75)
        self.assertEqual(dict(self.mx.read(2)), {"mx/2/1/m": 0.75})
        self.assertEqual(self.mx.write("mx/2/1/m", 0.5), 0.5)  # trusted: FL reads back late
        self.assertEqual(dict(self.mx.read(2)), {"mx/2/1/m": 0.75})
        self.fl.project.refresh()
        self.assertEqual(dict(self.mx.read(2)), {"mx/2/1/m": 0.5})
        self.assertIsNone(self.mx.write("mx/2/1/on", False))  # FL can't switch slots back on

    def test_routing_round_trip(self):
        self.assertEqual(dict(self.mr.read(3)), {"mr/3": "0:1.0"})
        self.assertEqual(self.mr.write("mr/3", "0:1.0,5:0.5"), "0:1.0,5:0.5")
        self.assertEqual(self.fl.mixer.tracks[3].routes, {0: 1.0, 5: 0.5})
        self.assertEqual(self.mr.write("mr/3", "7:0.25"), "7:0.25")
        self.assertEqual(self.fl.mixer.tracks[3].routes, {7: 0.25})
        self.assertEqual(self.mr.write("mr/3", ""), "")

    def test_descriptions(self):
        self.fl.api.setTrackName(4, "Trummor")
        self.assertEqual(self.m.describe("m/0/v"), "Master volym")
        self.assertEqual(self.m.describe("m/3/p"), "Insert 3 pan")
        self.assertEqual(self.m.describe("m/4/e1g"), "Insert 4 (Trummor) EQ band 2 gain")
        self.assertEqual(self.mx.describe("mx/4/0/m"), "Insert 4 (Trummor) slot 1 mix")
        self.assertEqual(self.mr.describe("mr/3"), "Insert 3 routing")

    def test_dirty_callbacks_point_at_tracks(self):
        self.assertEqual(self.m.on_dirty_mixer_track(3), [3])
        self.assertEqual(self.m.on_dirty_mixer_track(-1), "all")
        self.fl.mixer.selected = 5
        self.assertEqual(self.m.on_refresh(4), [5])
        self.assertEqual(self.m.on_refresh(1024), ())

    def test_mute_is_set_not_toggled(self):
        # FL shows a mute only after its next refresh, so a toggle based on
        # isTrackMuted() would undo a second quick change...
        self.assertIs(self.m.write("m/3/mu", True), True)
        self.assertIs(self.m.write("m/3/mu", False), False)
        self.fl.project.refresh()
        self.assertFalse(self.fl.muted(3))
        # ...or a click here that FL doesn't show yet, meeting the same change from someone else.
        self.fl.api.muteTrack(3, 1)
        self.assertIs(self.m.write("m/3/mu", True), True)
        self.fl.project.refresh()
        self.assertTrue(self.fl.muted(3))
        self.assertIs(self.m.write("m/3/mu", 0), False)
        self.fl.project.refresh()
        self.assertFalse(self.fl.muted(3))

    def test_before_api_30_mute_can_only_toggle(self):
        self.fl.project.modules["general"].getVersion = lambda: 29
        calls = []
        mute = self.fl.api.muteTrack
        self.fl.api.muteTrack = lambda *args: calls.append(args) or mute(*args)
        old = type(self.m)()  # made on that FL
        self.assertIs(old.write("m/3/mu", True), True)
        self.assertIs(old.write("m/4/mu", False), False)  # not muted: nothing to do
        self.assertEqual(calls, [(3,)])
        self.fl.project.refresh()
        self.assertTrue(self.fl.muted(3))

    def test_values_of_the_wrong_kind_are_refused_without_touching_fl(self):
        self.fl.mixer.add_plugin(2, 1, mix=0.75)
        self.fl.project.dirty_mixer.clear()
        before = self.fl.mixer.snapshot()
        refused = [("m/3/n", None), ("m/3/n", 5), ("m/3/mu", None), ("m/3/mu", "ja"), ("m/3/mu", 2),
                   ("m/3/mu", 0.5), ("m/3/fx", None), ("m/3/fx", "nej"), ("m/3/c", None), ("m/3/c", 0.5),
                   ("m/3/c", "12"), ("m/3/c", True), ("m/3/c", 2 ** 31), ("m/3/c", -2 ** 31 - 1),
                   ("m/3/c", 2 ** 40), ("m/3/v", "0.25"), ("m/3/v", None), ("m/3/v", True),
                   ("m/3/v", float("nan")), ("m/3/p", float("inf")), ("m/3/p", [0.5]), ("m/3/s", "0.5"),
                   ("m/3/e0g", None), ("m/3/e1f", "0.5"), ("m/3/e2w", float("nan")),
                   ("mx/2/1/m", "0.5"), ("mx/2/1/m", None), ("mx/2/1/m", True), ("mx/2/1/m", float("nan")),
                   ("mr/3", "0:nan"), ("mr/3", "0:inf"), ("mr/3", "0:1.0,x:0.5"), ("mr/3", None)]
        for key, value in refused:
            with self.subTest(key=key, value=value):
                binding = {"m": self.m, "mx": self.mx, "mr": self.mr}[key.split("/")[0]]
                self.assertIsNone(binding.write(key, value))
        self.assertEqual(self.fl.mixer.snapshot(), before)
        self.assertEqual(self.fl.project.dirty_mixer, [])
        # The extremes are fine.
        self.assertEqual(self.m.write("m/3/c", -2 ** 31), -2 ** 31)
        self.assertEqual(self.m.write("m/3/c", 2 ** 31 - 1), 2 ** 31 - 1)
        self.assertEqual(self.m.write("m/3/v", 2), 1.0)
        self.assertEqual(self.m.write("m/3/n", ""), "Insert 3")

    def test_routing_fl_reports_for_all_tracks_is_reread_a_few_tracks_per_tick(self):
        fl = FakeFL("B", inserts=125, dynamic=False)
        self.addCleanup(fl.close)
        mr, = fl.bindings("mr")
        list(mr.units())
        mr.tick(0)
        self.assertEqual(mr.on_dirty_mixer_track(-1), ())  # not all 126 * 125 pairs in one tick...
        seen, ticks = set(), 0
        while len(seen) < 126:
            list(mr.units())
            reread, rebaseline = mr.tick(0)
            seen.update(reread)
            self.assertEqual(list(rebaseline), [])
            self.assertTrue(reread)
            self.assertLessEqual(len(reread) * 125, mr.burst_calls)  # a track costs a call per other track
            ticks += 1
            self.assertLess(ticks, 30)
        self.assertEqual(seen, set(range(126)))  # ...but every track soon
        self.assertEqual(list(mr.tick(0)[0]), [])  # and then it's done
        self.assertEqual(mr.on_dirty_mixer_track(7), [7])
        self.assertEqual(fl.project.invalid, [])


class MixerRestructureTest(unittest.TestCase):
    """FL 2025+ can insert, delete and move inserts, and gives the ones after
    them other numbers: what that changes isn't anyone's edit."""

    def setUp(self):
        self.fl = FakeFL("A")
        self.mixer = self.fl.mixer
        self.bindings = [self.fl.bindings(prefix)[0] for prefix in ("m", "mx", "mr")]
        self.mixer.add_plugin(5, 0, mix=0.5)
        for binding in self.bindings:
            self.assertEqual(self.cycle(binding), ((), ()))

    def tearDown(self):
        self.assertEqual(self.fl.project.invalid, [])
        self.fl.close()

    @staticmethod
    def cycle(binding):
        """What the client does each tick: ask for the units, then tick."""
        list(binding.units())
        reread, rebaseline = binding.tick(0)
        return tuple(reread), tuple(rebaseline)

    def delete_insert(self, track):
        """Delete an insert as FL 2025 does: the ones after it move down, and so do sends to them."""
        del self.mixer.tracks[track]
        for t in self.mixer.tracks:
            t.routes = {(d - 1 if d > track else d): level for d, level in t.routes.items() if d != track}

    def test_a_deleted_insert_makes_every_track_the_baseline_once(self):
        self.delete_insert(3)
        for binding in self.bindings:
            with self.subTest(prefix=binding.prefix):
                self.assertEqual(self.cycle(binding)[1], tuple(range(17)))
                self.assertEqual(self.cycle(binding), ((), ()))

    def test_an_inserted_insert_too(self):
        self.mixer.tracks.insert(3, type(self.mixer.tracks[3])(3))
        for binding in self.bindings:
            with self.subTest(prefix=binding.prefix):
                self.assertEqual(self.cycle(binding)[1], tuple(range(19)))
                self.assertEqual(self.cycle(binding), ((), ()))

    def test_a_loaded_project_is_the_baseline_already(self):
        self.delete_insert(3)
        for binding in self.bindings:
            binding.on_project_load()  # the client takes every value as the baseline then
            self.assertEqual(self.cycle(binding), ((), ()))


class MixerSizeTest(unittest.TestCase):
    def test_dynamic_mixer_of_fl_2025(self):
        fl = FakeFL("A", inserts=17)
        self.assertEqual(list(fl.bindings("m")[0].units()), list(range(18)))
        fl.close()

    def test_fixed_mixer_of_older_fl_leaves_out_the_current_track(self):
        fl = FakeFL("A", inserts=125, dynamic=False)
        self.assertEqual(list(fl.bindings("m")[0].units()), list(range(126)))
        self.assertEqual(fl.project.invalid, [])
        fl.close()


if __name__ == "__main__":
    unittest.main()
