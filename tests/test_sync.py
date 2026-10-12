"""End-to-end tests: simulated FL Studios run the real script, talk MIDI to
real bridges, which talk to a real relay over TCP (protocol v1).

    python -m unittest discover -s tests -v
"""

import time
import unittest
from unittest import mock

from fake_fl import FakeFL
from harness import BridgeLink, LoopThread, RawClient, RelayServer, bridge_mod, free_port, pump


def shared_keys(fl):
    """The keys an FL shares as starting values."""
    client = fl.client
    keys = set()
    for key, value in client.shadow.items():
        b = client.binding_for(key)
        if b.seed and not b.transient(key) and b.shared_at_start(key, value):
            keys.add(key)
    return keys


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.loop = LoopThread()
        self.relay = RelayServer(self.loop)
        self.port = self.relay.start()
        self.fls = []
        self.bridges = {}

    def tearDown(self):
        for bridge in self.bridges.values():
            bridge.stop()
        invalid = {}
        for fl in self.fls:
            fl.close()
            if fl.project.invalid:
                invalid[fl.name] = fl.project.invalid
        self.relay.stop()
        self.loop.stop()
        self.assertEqual(invalid, {}, "scriptet anropade FL med index som inte finns")

    def fl(self, name, bridge=True, relay_port=None, **kwargs):
        fl = FakeFL(name, **kwargs)
        self.fls.append(fl)
        if bridge:
            self.bridges[fl] = BridgeLink(self.loop, relay_port or self.port, fl)
        return fl

    def wait_for(self, condition, timeout=5.0):
        self.assertTrue(pump(self.fls, condition, timeout), "tog för lång tid")

    def settle(self, seconds=0.3):
        pump(self.fls, lambda: False, seconds)

    def accepted(self):
        return self.relay.relay.seq  # one per change the relay has accepted

    def session(self, **kwargs):
        a, b = self.fl("A", **kwargs), self.fl("B", **kwargs)
        self.wait_for(lambda: a.client.joined and b.client.joined  # big mixers take a while over MIDI
                      and not a.client.pending and not b.client.pending
                      and a.mixer.snapshot() == b.mixer.snapshot(), timeout=30)
        return a, b

    # --- joining ------------------------------------------------------------

    def test_first_fl_shares_everything_and_the_next_one_gets_it(self):
        a = self.fl("A")
        a.set_volume(3, 0.5)
        a.set_pan(3, -0.25)
        a.set_mute(7, True)
        a.api.setTrackName(2, "Bas")
        a.api.setEqGain(4, 1, 0.7)
        a.api.setRouteTo(5, 6, True)
        self.wait_for(lambda: a.client.joined and not a.client.pending, timeout=15)
        self.assertTrue(shared_keys(a) <= set(self.relay.relay.state))
        b = self.fl("B")
        self.wait_for(lambda: b.mixer.snapshot() == a.mixer.snapshot(), timeout=15)
        self.assertTrue(b.muted(7))
        self.assertEqual(b.api.getTrackName(2), "Bas")

    def test_the_mix_is_handed_on_if_the_first_fl_leaves_before_sharing(self):
        silent = RawClient(self.port, "tyst", app="fl")
        self.assertTrue(silent.welcome["seed"])
        a = self.fl("A")
        self.wait_for(lambda: a.client.joined)
        silent.close()
        self.wait_for(lambda: shared_keys(a) <= set(self.relay.relay.state), timeout=15)

    def test_changes_made_before_the_first_fl_shares_are_kept(self):
        with RawClient(self.port) as m:
            m.send({"t": "sets", "l": 1, "kv": [["m/1/v", 0.3]]})
            m.recv_kind("ack")
        a = self.fl("A")
        a.set_volume(4, 0.6)
        self.wait_for(lambda: a.client.joined and not a.client.pending and a.volume(1) == 0.3, timeout=15)
        self.assertEqual(self.relay.relay.state["m/4/v"], 0.6)

    def test_a_change_during_sharing_beats_the_late_starting_value(self):
        a = self.fl("A", inserts=125, dynamic=False)  # thousands of values take a while over MIDI
        self.wait_for(lambda: a.client.joined and a.client.pending)
        with RawClient(self.port) as m:  # someone moves a fader while A is still sharing
            m.send({"t": "sets", "l": 1, "kv": [["m/125/v", 0.2]]})
            self.wait_for(lambda: not a.client.pending, timeout=60)
        self.wait_for(lambda: a.volume(125) == 0.2)
        self.assertEqual(self.relay.relay.state["m/125/v"], 0.2)

    # --- live changes -------------------------------------------------------

    def test_changes_reach_the_other_fl_in_both_directions(self):
        a, b = self.session()
        a.set_volume(1, 0.33)
        b.set_pan(2, 0.5)
        a.set_mute(17, True)
        b.api.setTrackName(5, "Trummor")
        a.api.setEqFrequency(6, 2, 0.9)
        b.api.setRouteToLevel(3, 0, 0.5)
        self.wait_for(lambda: a.mixer.snapshot() == b.mixer.snapshot()
                      and b.volume(1) == 0.33 and a.pan(2) == 0.5 and b.muted(17)
                      and a.api.getTrackName(5) == "Trummor")

    def test_fader_drag_ends_where_the_user_let_go(self):
        a, b = self.session()
        for step in range(60):
            a.set_volume(4, step / 100)
            a.idle()
            b.idle()
        self.wait_for(lambda: b.volume(4) == a.volume(4) == 0.59)

    def test_change_fl_did_not_report_is_found_by_the_sweep(self):
        a, b = self.session()
        a.set_volume(15, 0.25, quiet=True)
        self.wait_for(lambda: b.volume(15) == 0.25)

    def test_nothing_is_sent_when_nothing_changes(self):
        a, b = self.session()
        a.set_volume(1, 0.333333)  # not representable in FL: rounding must not echo
        b.set_mute(2, True)        # FL shows mute late: the receiver must not echo it either
        self.wait_for(lambda: b.volume(1) == a.volume(1) and a.muted(2))
        self.settle()
        before = self.accepted()
        self.settle(1.0)
        self.assertEqual(self.accepted(), before)

    def test_unrepresentable_remote_value_is_not_sent_back(self):
        a = self.fl("A")
        self.wait_for(lambda: a.client.joined and not a.client.pending, timeout=15)
        before = self.accepted()
        with RawClient(self.port) as m:
            m.send({"t": "sets", "l": 1, "kv": [["m/9/v", 0.123456789]]})
            self.wait_for(lambda: abs(a.volume(9) - 0.123456789) < 1e-4)
            self.settle()
        self.assertEqual(self.accepted(), before + 1)

    def test_simultaneous_edits_end_up_identical_everywhere(self):
        a, b = self.session()
        for round_ in range(5):
            a.set_volume(2, 0.1 + round_ / 100)
            b.set_volume(2, 0.9 - round_ / 100)
            a.idle()
            b.idle()
        self.wait_for(lambda: not a.client.pending and not b.client.pending
                      and a.volume(2) == b.volume(2))
        self.assertEqual(self.relay.relay.state["m/2/v"], a.volume(2))

    def test_remote_changes_wait_until_fl_is_safe_to_edit(self):
        a, b = self.session()
        b.safe = False
        a.set_volume(5, 0.2)
        self.settle()
        self.assertNotEqual(b.volume(5), 0.2)
        b.safe = True
        self.wait_for(lambda: b.volume(5) == 0.2)

    def test_monitor_can_drive_fl(self):
        a = self.fl("A")
        self.wait_for(lambda: a.client.joined)
        with RawClient(self.port) as m:
            m.send({"t": "set", "k": "m/9/p", "v": 0.5, "l": 1})
            m.send({"t": "sets", "l": 2, "kv": [["m/9/mu", True], ["m/9/n", "Sång"]]})
            self.wait_for(lambda: a.pan(9) == 0.5 and a.muted(9) and a.api.getTrackName(9) == "Sång")

    def test_hint_shows_who_changed_what(self):
        a, b = self.session()
        b.set_volume(3, 0.4)
        self.wait_for(lambda: "B: Insert 3 volym" in a.project.hints)

    def test_works_even_if_fl_never_calls_onidle(self):
        a, b = self.session(calls_idle=False)  # the bridge's heartbeat drives the script
        a.set_volume(6, 0.45)
        self.wait_for(lambda: b.volume(6) == 0.45)

    # --- mixers of different FL versions -----------------------------------

    def test_older_fl_with_a_fixed_mixer(self):
        a, b = self.session(inserts=125, dynamic=False)
        a.set_volume(125, 0.3)
        self.wait_for(lambda: b.volume(125) == 0.3)

    def test_keys_this_fl_does_not_have_are_ignored(self):
        small = self.fl("Liten", inserts=10)
        self.wait_for(lambda: small.client.joined)
        with RawClient(self.port) as m:
            m.send({"t": "sets", "l": 1, "kv": [["m/50/v", 0.3], ["m/x/v", 0.3], ["zz/1", 1],
                                                ["m/1/v/extra", 0.3], ["m/3/v", "hög"], ["m/2/v", 0.3]]})
            self.wait_for(lambda: small.volume(2) == 0.3)
        self.settle()
        self.assertEqual(small.script._errors, 0)

    # --- projects -------------------------------------------------------------

    def test_loading_a_project_alone_makes_it_the_session(self):
        a = self.fl("A")
        self.wait_for(lambda: a.client.joined and not a.client.pending, timeout=15)
        a.load_project({3: 0.1})
        self.wait_for(lambda: self.relay.relay.state.get("m/3/v") == 0.1, timeout=15)

    def test_loading_a_project_with_others_keeps_the_session_mix(self):
        a, b = self.session()
        b.load_project({3: 0.1})
        self.wait_for(lambda: b.volume(3) == a.volume(3) == 0.8, timeout=15)

    # --- when things go away ------------------------------------------------------

    def test_clients_rejoin_after_relay_restart(self):
        a, b = self.session()
        self.relay.stop()
        self.wait_for(lambda: not a.client.joined and not b.client.joined)
        self.relay = RelayServer(self.loop, self.port)
        self.relay.start()
        self.wait_for(lambda: a.client.joined and b.client.joined
                      and not a.client.pending and not b.client.pending, timeout=30)
        a.set_volume(6, 0.4)
        self.wait_for(lambda: b.volume(6) == 0.4)

    def test_fl_rejoins_when_the_bridge_comes_back(self):
        a, b = self.session()
        self.bridges.pop(a).stop()
        self.wait_for(lambda: not a.client.joined, timeout=6)
        self.assertTrue(any("tappade kontakten med bryggan" in line for line in a.logs))
        self.bridges[a] = BridgeLink(self.loop, self.port, a)
        self.wait_for(lambda: a.client.joined, timeout=15)
        b.set_volume(8, 0.6)
        self.wait_for(lambda: a.volume(8) == 0.6, timeout=15)

    def test_bridge_leaves_the_relay_when_fl_goes_quiet(self):
        a, b = self.session()
        a.to_bridge = None  # FL stops sending, e.g. it was closed
        self.wait_for(lambda: not b.client.peers, timeout=8)

    def test_a_busy_fl_stays_in_the_session(self):
        a, b = self.session()
        a.window_open = b.window_open = True  # FL's window is there: it is busy (a plugin loading), not closed
        # (Longer than the script's 1 s heartbeat: B's bridge must not take B for busy.)
        with mock.patch.object(bridge_mod, "FL_TIMEOUT", 1.5):
            self.fls.remove(a)  # FL does nothing at all meanwhile, not even its script
            self.settle(4)      # longer than the script's own link timeout too
            self.assertTrue(b.client.peers)
            b.set_volume(5, 0.3)
            self.settle(0.3)
            self.fls.append(a)
            self.wait_for(lambda: a.volume(5) == 0.3, timeout=15)
        logs = self.bridges[a].logs
        self.assertTrue(any("stannar kvar i sessionen" in line for line in logs))
        self.assertTrue(any("FL svarar igen" in line for line in logs))
        self.assertFalse(any("lämnar relayn" in line for line in logs))
        self.assertTrue(any("FL var upptaget" in line for line in a.logs))
        self.assertFalse(any("tappade kontakten med bryggan" in line for line in a.logs))

    def test_an_fl_gone_for_too_long_joins_again_when_it_is_back(self):
        a, b = self.session()
        a.window_open = b.window_open = True
        with mock.patch.object(bridge_mod, "FL_TIMEOUT", 1.5), mock.patch.object(bridge_mod, "FL_GONE", 3.0):
            self.fls.remove(a)
            self.wait_for(lambda: not b.client.peers, timeout=8)
            # FL hears the bridge's heartbeats first and would think it is still
            # in the session: the bridge tells it otherwise.
            self.fls.append(a)
            self.wait_for(lambda: b.client.peers and a.client.joined, timeout=15)
        b.set_volume(6, 0.45)
        self.wait_for(lambda: a.volume(6) == 0.45, timeout=15)

    def test_without_a_bridge_fl_just_waits(self):
        lonely = self.fl("Ensam", bridge=False)
        self.settle(0.5)
        self.assertFalse(lonely.client.joined)
        self.assertEqual(lonely.script._errors, 0)

    def test_missing_output_port_is_explained_once(self):
        fl = self.fl("Utan utgång", assigned=False)
        self.settle(0.5)
        self.assertFalse(fl.client.joined)
        self.assertEqual(fl.script._errors, 0)
        self.assertEqual(sum("ingen MIDI-utgång" in line for line in fl.logs), 2)  # OnInit + bridge seen

    def test_missing_relay_is_reported_once(self):
        fl = self.fl("Ensam", relay_port=free_port())
        self.settle(4.0)  # Windows takes ~2 s to refuse a local connect, then we retry
        self.assertFalse(fl.client.joined)
        self.assertEqual(fl.script._errors, 0)
        self.assertEqual(sum("når inte relayn" in line for line in fl.logs), 1)

    # --- cost ---------------------------------------------------------------------

    def test_a_tick_stays_cheap_with_a_big_project(self):
        a, b = self.session(inserts=125, dynamic=False)
        start = time.perf_counter()
        for _ in range(100):
            a.script.OnDirtyMixerTrack(5)
            a.client.tick(time.monotonic())
        per_tick = (time.perf_counter() - start) / 100
        self.assertLess(per_tick, 0.005)


if __name__ == "__main__":
    unittest.main()
