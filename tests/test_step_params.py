"""Step parameters (keys gp/): the parameters of the steps that are on. First
the binding against the fake channel rack, then end to end through the real
sync client with the small relay of test_channels_binding.py.

    python -m unittest discover -s tests -p "test_step_params.py"
"""

import unittest

import fake_channels
from fake_channels import STEP_DEFAULTS, midi
from test_channels_binding import Rack, Session, counted, step_calls

NAMES = ("pitch", "velocity", "release", "fine", "pan", "mod_x", "mod_y", "shift")


def params(**changed):
    """A step's parameters: FL's defaults for a step switched on, some of them changed."""
    unknown = set(changed) - set(NAMES)
    if unknown:
        raise TypeError("okända parametrar: %s" % ", ".join(sorted(unknown)))
    return tuple(changed.get(name, default) for name, default in zip(NAMES, STEP_DEFAULTS))


def entry(step, **changed):
    """One step of a gp value: entry(4, velocity=80) is '4:60,80,64,120,64,128,128,0'."""
    return "%d:%s" % (step, ",".join(str(value) for value in params(**changed)))


def value(*entries):
    return ";".join(entries)


class StepParamsTest(Rack):
    """The step parameter binding of one simulated FL, called directly."""

    def on(self, channel, *steps):
        """Switch steps on in the current pattern, as clicking them would."""
        for step in steps:
            self.channels.setGridBit(channel, step, 1, 1)

    def read(self, pattern, channel):
        """The gp value the binding reads for a row, or None if it reads none."""
        return dict(self.gp.read((pattern, channel))).get("gp/%d/%d" % (pattern, channel))

    # --- reading --------------------------------------------------------------------

    def test_a_value_lists_the_parameters_of_the_steps_that_are_on(self):
        self.on(1, 0, 4, 12)
        self.rack.edit_step(1, 4, midi.pVelocity, 80)
        self.rack.edit_step(1, 12, midi.pShift, 5)
        self.assertEqual(self.read(1, 1), value(entry(0), entry(4, velocity=80), entry(12, shift=5)))
        self.assertEqual(self.read(1, 0), "")
        self.assertEqual(self.gp.units(), [(1, 0), (1, 1), (1, 2), (1, 3)])
        self.patterns.jumpToPattern(2)
        self.assertEqual(self.read(2, 1), "")
        self.assertIsNone(self.read(1, 1))  # FL can't show pattern 1 now

    def test_a_step_switched_off_forgets_its_parameters(self):
        self.on(0, 2)
        self.rack.edit_step(0, 2, midi.pPan, 0)
        self.channels.setGridBit(0, 2, 0, 1)
        self.assertEqual(self.read(1, 0), "")
        self.on(0, 2)
        self.assertEqual(self.read(1, 0), entry(2))  # FL's defaults again

    def test_a_long_row_is_cut_to_what_the_relay_takes(self):
        self.on(0, *range(512))  # every step of a 32-bar pattern
        reads = counted(self.channels, "getCurrentStepParam", self)
        cut = self.read(1, 0)
        self.assertLessEqual(len(cut), 4096)
        steps = [int(item.split(":")[0]) for item in cut.split(";")]
        self.assertEqual(steps, list(range(len(steps))))  # the first steps, as many as fit
        self.assertGreater(len(cut + ";" + entry(len(steps))), 4096)
        self.assertLessEqual(len(reads), 8 * (len(steps) + 1))  # the rest isn't even read
        self.assertEqual(self.gp.write("gp/1/0", cut), cut)  # and it goes back in as it is

    def test_clip_channels_have_no_parameters(self):
        self.rack.channels[3].kind = self.midi.CT_AutoClip
        self.assertIsNone(self.read(1, 3))
        self.assertIsNone(self.gp.write("gp/1/3", ""))
        self.assertIsNone(self.gp.write("gp/2/3", entry(0)))

    def test_rows_without_steps_are_left_out_of_the_starting_values(self):
        self.assertFalse(self.gp.shared_at_start("gp/1/0", ""))
        self.assertTrue(self.gp.shared_at_start("gp/1/0", entry(0)))
        self.assertFalse(self.gp.transient("gp/1/0"))

    def test_descriptions(self):
        self.channels.setChannelName(1, "Bas", 1)
        texts = {"gp/1/0": "Kick stegparametrar i mönster 1", "gp/2/1": "Bas stegparametrar i mönster 2",
                 "gp/3/9": "Kanal 10 stegparametrar i mönster 3", "gp/1": "gp/1", "gp/x/1": "gp/x/1"}
        for key, text in texts.items():
            self.assertEqual(self.gp.describe(key), text)
        # Parameters that come with their row are part of a step edit, and say so.
        self.g.write("g/1/0", "1")
        self.assertEqual(self.gp.describe("gp/1/0"), "Kick steg i mönster 1")
        self.gp.tick(0)
        self.assertEqual(self.gp.describe("gp/1/0"), "Kick stegparametrar i mönster 1")

    # --- writing the current pattern ------------------------------------------------------

    def test_writing_gives_the_steps_that_are_on_their_parameters(self):
        self.on(2, 0, 4)
        sent = value(entry(0, pitch=62, pan=30), entry(4, velocity=300), entry(8, pitch=61))
        # Velocity 300 is more than FL keeps; step 8 is off here, and FL keeps nothing for it.
        held = value(entry(0, pitch=62, pan=30), entry(4, velocity=127))
        self.assertEqual(self.gp.write("gp/1/2", sent), held)
        self.assertEqual(self.read(1, 2), held)
        self.assertEqual(self.rack.row(2), "10001")  # no step switched on
        self.assertEqual(self.rack.params(2), {0: params(pitch=62, pan=30), 4: params(velocity=127)})

    def test_only_the_parameters_that_differ_are_written(self):
        self.on(1, 0, 3)
        sets = counted(self.channels, "setStepParameterByIndex", self)
        self.gp.write("gp/1/1", value(entry(0), entry(3, velocity=90, shift=2)))
        # (channel, pattern, step, parameter, value, useGlobalIndex)
        self.assertEqual(sets, [(1, 1, 3, midi.pVelocity, 90, 1), (1, 1, 3, midi.pShift, 2, 1)])

    def test_parameters_for_steps_that_are_off_go_in_with_their_row(self):
        self.on(0, 0)
        sent = value(entry(0, velocity=90), entry(4, pitch=72))
        self.assertEqual(self.gp.write("gp/1/0", sent), entry(0, velocity=90))  # step 4 is off: not yet
        self.assertEqual(self.g.write("g/1/0", "10001"), "10001")  # the row comes after them
        self.assertEqual(self.rack.params(0)[4], STEP_DEFAULTS)
        self.assertEqual(self.gp.tick(0)[1], [(1, 0)])  # put in, and taken as it is then
        self.assertEqual(self.read(1, 0), sent)
        self.assertEqual(self.gp.tick(0)[1], [])
        self.assertEqual(self.gp.waiting, {})

    def test_waiting_parameters_wait_while_fl_is_not_safe_to_edit(self):
        self.gp.write("gp/1/0", entry(4, velocity=90))  # step 4 is off
        self.g.write("g/1/0", "00001")
        self.fl.safe = False
        self.assertEqual(self.gp.tick(0)[1], [(1, 0)])  # taken as it is meanwhile
        self.assertEqual(self.read(1, 0), entry(4))
        self.fl.safe = True
        self.assertEqual(self.gp.tick(0)[1], [(1, 0)])
        self.assertEqual(self.rack.params(0), {4: params(velocity=90)})

    def test_a_step_the_user_switched_on_keeps_its_own_parameters(self):
        self.gp.write("gp/1/0", entry(4, pitch=72))   # someone's step 4, off here
        self.on(0, 4)                                  # the user switches it on here...
        self.rack.edit_step(0, 4, midi.pVelocity, 50)  # ...and sets its velocity
        self.assertEqual(self.read(1, 0), entry(4, velocity=50))  # the sweep sees it
        self.g.write("g/1/0", "10001")                 # then a row from someone else
        self.gp.tick(0)
        self.assertEqual(self.rack.params(0)[4], params(velocity=50))

    # --- other patterns ------------------------------------------------------------------

    def test_another_pattern_is_written_with_setStepParameterByIndex(self):
        self.patterns.jumpToPattern(2)
        self.on(1, 0, 2)
        dict(self.g.read((2, 1)))  # the grid sees the row while pattern 2 is current
        self.patterns.jumpToPattern(1)
        sets = counted(self.channels, "setStepParameterByIndex", self)
        sent = value(entry(0, velocity=90), entry(2, shift=7), entry(5))
        self.assertEqual(self.gp.write("gp/2/1", sent), sent)
        self.assertEqual({(args[1], args[2]) for args in sets}, {(2, 0), (2, 2)})  # step 5 is off there
        self.assertEqual(self.rack.params(1, 2), {0: params(velocity=90), 2: params(shift=7)})
        self.assertEqual(self.rack.params(1, 1), {})  # pattern 1 is left alone...
        self.assertEqual(self.patterns.patternNumber(), 1)  # ...and stays current

    def test_a_pattern_not_seen_yet_waits_for_the_switch(self):
        self.rack.channels[1].steps[3] = {0}  # the project has a step in pattern 3, not current yet
        sets = counted(self.channels, "setStepParameterByIndex", self)
        sent = entry(0, velocity=90)
        self.assertEqual(self.gp.write("gp/3/1", sent), sent)
        self.assertEqual(sets, [])  # which steps are on there is unknown
        self.patterns.jumpToPattern(3)
        self.g.tick(0)
        self.assertEqual(self.gp.tick(0)[1], [(3, 0), (3, 1), (3, 2), (3, 3)])
        self.assertEqual(self.rack.params(1, 3), {0: params(velocity=90)})
        self.assertEqual(self.gp.waiting, {})

    def test_a_waiting_row_gets_its_parameters_when_the_user_switches_to_it(self):
        self.assertEqual(self.g.write("g/2/1", "0011"), "0011")  # pattern 2 isn't current: both wait
        sent = value(entry(2, velocity=90), entry(3, pan=0))
        self.assertEqual(self.gp.write("gp/2/1", sent), sent)
        self.patterns.jumpToPattern(2)
        self.g.tick(0)  # the grid puts the row in first
        self.assertEqual(self.rack.row(1, 2), "0011")
        self.assertEqual(self.gp.tick(0)[1], [(2, 0), (2, 1), (2, 2), (2, 3)])
        self.assertEqual(self.read(2, 1), sent)

    def test_switching_patterns_takes_the_parameters_as_they_are(self):
        self.assertEqual(self.gp.tick(0)[1], [])
        self.patterns.jumpToPattern(3)
        self.assertEqual(self.gp.tick(0)[1], [(3, 0), (3, 1), (3, 2), (3, 3)])
        self.assertEqual(self.gp.tick(0)[1], [])

    # --- what is kept --------------------------------------------------------------------

    def test_waiting_parameters_are_dropped_when_the_channels_get_other_numbers(self):
        self.gp.write("gp/2/1", entry(0, velocity=90))  # waits: pattern 2 isn't current
        self.rack.add_channel(0, "Bas")                  # Clap is channel 2 now
        self.gp.tick(0)
        self.assertEqual(self.gp.waiting, {})
        self.rack.channels[2].steps[2] = {0}             # Clap's step 0 in pattern 2
        self.patterns.jumpToPattern(2)
        self.gp.tick(0)
        self.assertEqual(self.rack.params(2, 2), {0: STEP_DEFAULTS})

    def test_a_loaded_project_starts_afresh(self):
        self.gp.write("gp/2/1", entry(0))  # for the old project's pattern 2
        client = self.fl.client
        client.project_load(self.midi.PL_Start)
        self.rack.channels.append(fake_channels.Channel("Bas"))  # the new project: five channels,
        self.rack.current = 2                                      # pattern 2 on show
        client.project_load(self.midi.PL_LoadOk)
        self.assertEqual(self.gp.waiting, {})
        self.assertEqual(list(self.gp.tick(0)[1]), [])  # the client took the new baseline itself

    def test_what_this_fl_cannot_take_is_refused_without_touching_fl(self):
        self.on(1, 0)
        before = self.rack.snapshot()
        sets = counted(self.channels, "setStepParameterByIndex", self)
        good, rest = entry(0, velocity=90), "60,100,64,120,64,128,128"
        refused = [("gp/1/4", good), ("gp/0/1", good), ("gp/1000/1", good), ("gp/1", good),
                   ("gp/1/x", good), ("gp/01/1", good), ("gp/1/1/0", good), (5, good),
                   ("gp/1/1", 5), ("gp/1/1", None), ("gp/1/1", True), ("gp/1/1", [good]),
                   ("gp/1/1", "0:60,100"), ("gp/1/1", good + ",0"), ("gp/1/1", "x" + good[1:]),
                   ("gp/1/1", "-" + good), ("gp/1/1", "00:" + rest + ",0"), ("gp/1/1", "512:" + rest + ",0"),
                   ("gp/1/1", "0 :" + rest + ",0"), ("gp/1/1", good + ";" + good), ("gp/1/1", good + ";"),
                   ("gp/1/1", ";" + good), ("gp/1/1", "0:" + rest + ",1.5"), ("gp/1/1", "0:" + rest + ",1e3"),
                   ("gp/1/1", "0:" + rest + ", 0"), ("gp/1/1", "0:" + rest + ",9999999999")]
        for key, text in refused:
            with self.subTest(key=key, value=text):
                self.assertIsNone(self.gp.write(key, text))
        self.assertEqual(self.rack.snapshot(), before)
        self.assertEqual(sets, [])
        self.assertEqual(self.gp.waiting, {})

    # --- cost ------------------------------------------------------------------------------

    def test_the_parameters_cost_a_bounded_number_of_fl_calls_per_tick(self):
        self.use(self.new_fl(channels=40))
        for channel in range(40):
            self.on(channel, *range(0, 64, 8))  # four bars, 8 steps on in every row
        row = 64 + 8 * 8                         # FL calls reading one row takes
        client, now = self.fl.client, 100.0
        calls = step_calls(self.fl, self)
        # Rows that got steps since they were last read cost more than the sweep
        # expects, until it has read each of them once.
        for _ in range(40):
            calls.clear()
            now += 0.05
            client.tick(now)
            self.assertLessEqual(len(calls.get("gp", ())), 40 * row)  # never a row twice
        for _ in range(5):
            calls.clear()
            now += 0.05
            client.tick(now)
            self.assertLessEqual(len(calls.get("gp", ())), max(self.gp.sweep_calls, row))
            self.assertTrue(calls.get("gp"))
        client.on_refresh(self.midi.HW_Dirty_Patterns)  # a step changed, FL doesn't say where
        seen = set()
        for _ in range(-(-40 * row // self.gp.burst_calls)):
            calls.clear()
            now += 0.05
            client.tick(now)
            self.assertLessEqual(len(calls.get("gp", ())), self.gp.burst_calls)
            seen.update(args[0] for name, args in calls.get("gp", ()) if name == "getCurrentStepParam")
        self.assertEqual(seen, set(range(40)))


class StepParamsSessionTest(Session):
    """Simulated FLs syncing step parameters through the real sync client."""

    def monitor(self, *pairs):
        """A batch from someone who isn't an FL here (a monitor, say)."""
        sender = self.relay.connect("M", 0)
        self.relay.handle(sender, {"t": "sets", "l": 1, "kv": [list(pair) for pair in pairs]})

    def test_parameters_go_both_ways_and_are_not_sent_back(self):
        a, b = self.session()
        self.api(a)[0].setGridBit(0, 0, 1, 1)
        self.until(lambda: b.project.rack.row(0) == "1")
        a.project.rack.edit_step(0, 0, midi.pVelocity, 90)
        self.until(lambda: b.project.rack.params(0) == {0: params(velocity=90)})
        b.project.rack.edit_step(0, 0, midi.pPan, 20)
        self.until(lambda: a.project.rack.params(0) == {0: params(velocity=90, pan=20)})
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("g/1/0", "1"), ("gp/1/0", entry(0)),
                                                   ("gp/1/0", entry(0, velocity=90))])
        self.assertEqual(self.relay.sent_by("B"), [("gp/1/0", entry(0, velocity=90, pan=20))])
        self.assertEqual(a.project.rack.snapshot(), b.project.rack.snapshot())

    def test_a_new_step_and_its_parameters_go_in_one_batch_row_first(self):
        a, b = self.session()
        self.api(a)[0].setGridBit(1, 4, 1, 1)
        a.project.rack.edit_step(1, 4, midi.pVelocity, 70)  # in the same tick
        self.until(lambda: b.project.rack.params(1) == {4: params(velocity=70)})
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("g/1/1", "00001"), ("gp/1/1", entry(4, velocity=70))])
        self.assertIn(("A", ["g/1/1", "gp/1/1"]), self.relay.batches)
        self.assertEqual(self.relay.sent_by("B"), [])
        self.assertIn("A: Clap steg i mönster 1", b.project.hints)

    def test_a_change_fl_did_not_report_is_found_by_the_sweep(self):
        a, b = self.session(channels=12)
        self.api(a)[0].setGridBit(9, 2, 1, 1)
        self.until(lambda: b.project.rack.row(9) == "001")
        a.project.rack.edit_step(9, 2, midi.pPitch, 48, quiet=True)  # FL says nothing about it
        self.until(lambda: b.project.rack.params(9) == {2: params(pitch=48)})
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_parameters_in_another_pattern_go_in_without_switching(self):
        a, b = self.session()
        for fl in (a, b):
            self.api(fl)[1].jumpToPattern(2)
        self.step()
        self.api(a)[0].setGridBit(0, 0, 1, 1)
        self.until(lambda: b.project.rack.row(0, 2) == "1")
        self.api(b)[1].jumpToPattern(1)
        self.step()
        a.project.rack.edit_step(0, 0, midi.pVelocity, 40)  # A is still on pattern 2
        self.until(lambda: b.project.rack.params(0, 2) == {0: params(velocity=40)})
        self.assertEqual(self.api(b)[1].patternNumber(), 1)
        self.api(b)[1].jumpToPattern(2)
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_a_joining_fl_gets_the_parameters_of_every_pattern(self):
        a = self.join("A")
        self.until(lambda: self.joined("A"))
        ch, pat = self.api(a)
        ch.setGridBit(0, 0, 1, 1)
        a.project.rack.edit_step(0, 0, midi.pPitch, 50)
        self.step()
        pat.jumpToPattern(2)
        self.step()
        ch.setGridBit(1, 1, 1, 1)
        a.project.rack.edit_step(1, 1, midi.pPan, 10)
        self.until(lambda: self.relay.state.get("gp/2/1") == entry(1, pan=10))
        b = self.join("B")
        self.until(lambda: self.joined("B") and b.project.rack.params(0) == {0: params(pitch=50)})
        self.assertEqual(b.project.rack.params(1, 2), {})  # B is on pattern 1: pattern 2 waits
        self.api(b)[1].jumpToPattern(2)
        self.until(lambda: b.project.rack.params(1, 2) == {1: params(pan=10)})
        self.step(20)
        self.assertEqual(self.relay.sent_by("B"), [])

    def test_parameters_that_arrive_before_their_row_go_in_with_it(self):
        a, b = self.session()
        both = value(entry(0, velocity=33), entry(5, pitch=70))
        self.monitor(("gp/1/2", both), ("g/1/2", "100001"))
        expected = {0: params(velocity=33), 5: params(pitch=70)}
        self.until(lambda: a.project.rack.params(2) == b.project.rack.params(2) == expected)
        self.step(20)
        self.assertEqual(self.relay.sent_by("A") + self.relay.sent_by("B"), [])

    def test_a_row_without_its_parameters_sends_nothing_back(self):
        a, b = self.session()
        self.monitor(("g/1/3", "0101"))
        self.until(lambda: a.project.rack.row(3) == b.project.rack.row(3) == "0101")
        self.step(20)
        self.assertEqual(self.relay.sent_by("A") + self.relay.sent_by("B"), [])

    def test_a_value_fl_cannot_hold_is_not_sent_back(self):
        a, b = self.session()
        self.api(a)[0].setGridBit(2, 0, 1, 1)
        self.until(lambda: b.project.rack.row(2) == "1")
        self.monitor(("gp/1/2", entry(0, velocity=500)))
        self.until(lambda: a.project.rack.params(2) == b.project.rack.params(2) == {0: params(velocity=127)})
        self.step(20)
        self.assertEqual(self.relay.sent_by("A"), [("g/1/2", "1"), ("gp/1/2", entry(0))])
        self.assertEqual(self.relay.sent_by("B"), [])


if __name__ == "__main__":
    unittest.main()
