"""The sync client's protocol logic, driven message by message with a fake link
and a controlled clock (no bridge, no relay)."""

import types
import unittest

from fake_fl import FakeFL
from fake_plugins import native


class FakeLink:
    """Stands in for the MIDI link: tests feed it messages and read what was sent."""

    def __init__(self):
        self.incoming = []
        self.sent = []
        self.up = True
        self.sending = False  # True: earlier messages are still queued for the bridge

    def alive(self, now):
        return self.up

    def can_send(self):
        return True

    def busy(self):
        return self.sending

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


class LaggingBinding:
    """A binding whose readback shows a written value only some ticks later,
    with one event key - to test settling and transient keys."""

    prefix = "x"
    seed = True
    sweep_budget = 1

    def __init__(self):
        self.value = 1
        self.shown = 1
        self.lag = 0
        self.writes = []

    def units(self):
        return [0]

    def read(self, unit):
        if self.lag:
            self.lag -= 1
        else:
            self.shown = self.value
        yield "x/val", self.shown

    def write(self, key, value):
        self.writes.append((key, value))
        if key == "x/val":
            self.value, self.lag = value, 3
            return value
        return value

    def describe(self, key):
        return "test"

    def transient(self, key):
        return key == "x/play"

    def shared_at_start(self, key, value):
        return True

    def on_dirty_mixer_track(self, index):
        return ()

    def on_dirty_channel(self, index, flag):
        return ()

    def on_refresh(self, flags):
        return ()

    def on_project_load(self):
        pass

    def tick(self, now):
        return [0], ()


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fl = FakeFL("A")
        self.link = FakeLink()
        self.extra = LaggingBinding()
        self.client = self.fl.script.SyncClient(
            "A", self.fl.script.make_bindings() + [self.extra], self.link,
            hint=self.fl.project.hints.append, fingerprint=self.fl.script.flsync_structure.fingerprint,
            context=self.fl.script.flsync_notes.context)
        self.client.start()
        self.now = 100.0
        self.proto = self.fl.script.flsync_client.PROTO

    def tearDown(self):
        self.assertEqual(self.fl.project.invalid, [])
        self.fl.close()

    def tick(self, count=1, seconds=0.05):
        for _ in range(count):
            project = self.fl.project
            project.refresh()
            dirty, project.dirty_mixer = project.dirty_mixer, []
            for track in dirty:
                self.client.on_dirty_mixer_track(track)
            self.now += seconds
            self.client.tick(self.now)

    def join(self, seed=False, state=None, peers=(), fp=None, snapshot=None):
        """Say hello and get welcomed; the session's structure is ours unless `fp` says otherwise."""
        self.tick()
        hello = self.link.take("hello")
        self.assertEqual(len(hello), 1)
        self.assertEqual(hello[0]["proto"], self.proto)
        self.link.incoming.append({"t": "welcome", "proto": self.proto, "id": "c1", "name": "A", "seed": seed,
                                   "state": state or {}, "peers": list(peers), "s": 0,
                                   "fp": fp or hello[0]["fp"], "snapshot": snapshot})
        self.tick()

    def sent_batches(self):
        return self.link.take("sets")

    def pairs(self, batches):
        return [tuple(pair) for batch in batches for pair in batch["kv"]]

    def ack_all(self, batches):
        self.link.incoming.extend({"t": "ack", "l": b["l"], "s": 1} for b in batches)
        self.tick()

    # --- joining ----------------------------------------------------------------

    def test_first_fl_shares_everything_as_seed_batches(self):
        self.join(seed=True)
        batches = self.sent_batches()
        self.assertTrue(all(b.get("seed") for b in batches))
        self.assertTrue(all(len(b["kv"]) <= 64 for b in batches))
        keys = [k for k, _ in self.pairs(batches)]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("m/0/v", keys)
        self.assertIn("mr/3", keys)
        self.assertIn("x/val", keys)
        self.ack_all(batches)
        self.assertEqual(self.client.pending, {})

    def test_joining_takes_the_session_but_not_event_keys(self):
        self.join(state={"m/3/v": 0.25, "x/play": True, "x/val": 7})
        self.assertEqual(self.fl.volume(3), 0.25)
        self.assertEqual(self.extra.writes, [("x/val", 7)])
        self.tick(10)
        self.assertEqual(self.sent_batches(), [])

    def test_wrong_protocol_stops_trying(self):
        self.tick()
        self.link.take("hello")
        self.link.incoming.append({"t": "welcome", "proto": 0, "id": "c1"})
        self.tick(100, seconds=0.1)
        self.assertEqual(self.link.take("hello"), [])
        self.assertFalse(self.client.joined)

    # --- changes ------------------------------------------------------------------

    def test_local_change_is_sent_once(self):
        self.join()
        self.fl.set_volume(3, 0.5)
        self.tick(5)
        batches = self.sent_batches()
        self.assertEqual(self.pairs(batches), [("m/3/v", 0.5)])
        self.assertFalse(batches[0].get("seed"))
        self.assertIn("m/3/v", self.client.pending)
        self.ack_all(batches)
        self.assertNotIn("m/3/v", self.client.pending)

    def test_remote_change_is_written_and_not_sent_back(self):
        self.join()
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/2/p", 0.5], ["mr/4", "0:1.0,5:0.5"]], "s": 3})
        self.tick(10)
        self.assertEqual(self.fl.pan(2), 0.5)
        self.assertEqual(self.fl.mixer.tracks[4].routes, {0: 1.0, 5: 0.5})
        self.assertEqual(self.sent_batches(), [])

    def test_others_are_ignored_while_our_change_is_pending(self):
        self.join()
        self.fl.set_volume(3, 0.5)
        self.tick()
        batches = self.sent_batches()
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/3/v", 0.1]], "s": 4})
        self.tick(3)
        self.assertEqual(self.fl.volume(3), 0.5)  # theirs is older than ours
        self.ack_all(batches)
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/3/v", 0.1]], "s": 6})
        self.tick(3)
        self.assertEqual(self.fl.volume(3), 0.1)

    def test_keep_gives_us_the_session_value(self):
        self.join(seed=True)
        batches = self.sent_batches()
        first = batches[0]
        self.link.incoming.append({"t": "keep", "l": first["l"], "kv": [["m/0/v", 0.3]]})
        self.ack_all(batches)
        self.tick(5)
        self.assertEqual(self.fl.volume(0), 0.3)
        self.assertEqual(self.client.pending, {})
        self.assertEqual(self.sent_batches(), [])

    def test_reject_releases_the_key(self):
        self.join()
        self.fl.set_volume(1, 0.2)
        self.tick()
        batch = self.sent_batches()[0]
        self.link.incoming.append({"t": "reject", "l": batch["l"], "k": "m/1/v", "msg": "nej"})
        self.tick()
        self.assertNotIn("m/1/v", self.client.pending)

    def test_a_lost_ack_stops_blocking_after_a_while(self):
        self.join()
        self.fl.set_volume(1, 0.2)
        self.tick()
        self.sent_batches()
        self.tick(220)  # 11 s without an ack
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/1/v", 0.6]], "s": 9})
        self.tick(2)
        self.assertEqual(self.fl.volume(1), 0.6)

    def test_changes_wait_while_the_link_is_still_sending(self):
        self.join()
        self.link.sending = True
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.fl.set_volume(3, 0.6)
        self.tick(3)
        self.assertEqual(self.sent_batches(), [])
        self.link.sending = False
        self.tick()
        self.assertEqual(self.pairs(self.sent_batches()), [("m/3/v", 0.6)])  # only the newest

    def test_a_change_still_on_its_way_out_keeps_blocking(self):
        self.join()
        self.fl.set_volume(1, 0.2)
        self.tick()
        self.sent_batches()
        self.link.sending = True
        self.tick(220)  # 11 s, and our messages are still going out
        self.link.sending = False
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/1/v", 0.6]], "s": 9})
        self.tick(2)
        self.assertEqual(self.fl.volume(1), 0.2)  # ours reaches the relay after theirs and wins

    def test_slow_readback_is_not_mistaken_for_a_local_change(self):
        self.join()
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["x/val", 5]], "s": 2})
        self.tick(20)
        self.assertEqual(self.extra.shown, 5)
        self.assertEqual(self.sent_batches(), [])

    def test_many_changes_are_split_into_batches(self):
        self.join()
        for track in range(18):
            self.fl.set_volume(track, 0.1)
            self.fl.set_pan(track, 0.2)
            self.fl.api.setTrackName(track, "Spår %d" % track)
            self.fl.api.setTrackStereoSep(track, 0.3)
        self.tick()
        batches = self.sent_batches()
        self.assertEqual(len(self.pairs(batches)), 72)
        self.assertEqual([len(b["kv"]) for b in batches], [64, 8])

    # --- presence -------------------------------------------------------------------

    def test_hint_says_who_changed_what(self):
        self.join(peers=[{"id": "c2", "name": "Kim", "app": "fl"}])
        self.fl.api.setTrackName(3, "Bas")
        self.tick()
        self.ack_all(self.sent_batches())
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/3/v", 0.4]], "s": 5})
        self.tick(2)
        self.assertEqual(self.fl.project.hints[-1], "Kim: Insert 3 (Bas) volym")
        self.link.incoming.append({"t": "peer_leave", "id": "c2", "name": "Kim", "app": "fl"})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: Kim lämnade")

    # --- projects ---------------------------------------------------------------------

    def test_loading_a_project_alone_shares_it_as_normal_changes(self):
        self.join()
        midi = self.fl.project.modules["midi"]
        self.client.project_load(midi.PL_Start)
        self.fl.api.setTrackVolume(2, 0.3)
        self.client.project_load(midi.PL_LoadOk)
        self.tick()
        batches = self.sent_batches()
        self.assertFalse(any(b.get("seed") for b in batches))
        self.assertIn(("m/2/v", 0.3), self.pairs(batches))

    def test_loading_a_project_with_others_asks_for_the_session(self):
        self.join(peers=[{"id": "c2", "name": "Kim", "app": "fl"}])
        midi = self.fl.project.modules["midi"]
        self.client.project_load(midi.PL_Start)
        self.client.project_load(midi.PL_LoadOk)
        self.tick()
        self.assertEqual(len(self.link.take("sync")), 1)
        self.assertEqual(self.sent_batches(), [])


    # --- structure --------------------------------------------------------------------

    KIM = {"id": "c2", "name": "Kim", "app": "fl"}

    def structure(self):
        return self.fl.script.flsync_structure.fingerprint()

    def add_channel(self):
        rack = self.fl.project.rack
        rack.add_channel(len(rack.channels), "Perc")

    def other_fl_with_an_extra_channel(self):
        """The fingerprint of this project with one more channel at the end."""
        other = FakeFL("B")
        try:
            rack = other.project.rack
            rack.add_channel(len(rack.channels), "Perc")
            return other.script.flsync_structure.fingerprint()
        finally:
            other.close()

    def wait_for_hint(self, text):
        self.tick(10)
        self.assertIn(text, self.fl.project.hints[-1])

    def test_hello_and_batches_carry_the_structure(self):
        self.tick()
        fp = self.link.take("hello")[0]["fp"]
        self.assertRegex(fp, "^[0-9a-f]{12}$")
        self.assertEqual(fp, self.structure())
        self.link.incoming.append({"t": "welcome", "proto": self.proto, "id": "c1", "name": "A", "seed": False,
                                   "state": {}, "peers": [], "s": 0, "fp": fp, "snapshot": None})
        self.tick()
        self.fl.set_volume(3, 0.5)
        self.tick()
        self.assertEqual([b["fp"] for b in self.sent_batches()], [fp])

    def test_renaming_and_values_dont_change_the_structure(self):
        before = self.structure()
        self.fl.api.setTrackName(3, "Bas")
        self.fl.set_volume(3, 0.5)
        self.fl.project.modules["channels"].setChannelName(0, "Kick", 1)
        self.assertEqual(self.structure(), before)
        self.add_channel()
        self.assertNotEqual(self.structure(), before)

    def test_joining_a_session_with_another_structure_pauses_both_ways(self):
        self.join(state={"m/3/v": 0.25}, peers=[self.KIM], fp="0123456789ab")
        self.assertEqual(self.fl.volume(3), 0.8)  # their values are for other channels
        self.wait_for_hint("FLSync pausad: öppna samma projekt som de andra")
        self.fl.set_volume(2, 0.5)
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/4/v", 0.1]], "s": 3})
        self.tick(5)
        self.assertEqual(self.sent_batches(), [])
        self.assertEqual(self.fl.volume(4), 0.8)

    def test_changing_the_structure_pauses_until_it_is_shared(self):
        self.join(peers=[self.KIM])
        self.add_channel()
        self.tick(25)  # the fingerprint is checked once a second
        new_fp = self.structure()
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": new_fp}])
        self.assertIn("du har ändrat kanaler eller plugins, spara för att dela", self.fl.project.hints[-1])
        self.fl.set_volume(2, 0.5)
        self.tick(3)
        self.assertEqual(self.sent_batches(), [])
        # Saving shares it: the relay announces our snapshot and asks for our values.
        self.link.incoming.append({"t": "snapshot", "v": 1, "name": "Beat", "fp": new_fp, "by": "A", "from": "c1"})
        self.link.incoming.append({"t": "reshare"})
        self.tick(10)
        batches = self.sent_batches()
        self.assertFalse(any(b.get("seed") for b in batches))
        self.assertTrue(all(b["fp"] == new_fp for b in batches))
        self.assertIn(("m/2/v", 0.5), self.pairs(batches))
        self.assertIn(("c/4/n", "Perc"), self.pairs(batches))
        self.assertEqual(self.fl.project.hints[-1], "FLSync: synkar igen")

    def test_someone_elses_snapshot_pauses_until_it_is_opened(self):
        self.join(peers=[self.KIM])
        new_fp = self.other_fl_with_an_extra_channel()
        self.link.incoming.append({"t": "snapshot", "v": 2, "name": "Beat", "fp": new_fp, "by": "Kim", "from": "c2",
                                   "path": "C:\\Projects\\FLSync\\Beat\\Beat.flp"})
        self.wait_for_hint("FLSync pausad: öppna Kims version av projektet")
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/4/v", 0.1]], "s": 3})
        self.tick(5)
        self.assertEqual(self.fl.volume(4), 0.8)
        # Opening it: a project load that gives us the same structure.
        midi = self.fl.project.modules["midi"]
        self.client.project_load(midi.PL_Start)
        self.add_channel()
        self.client.project_load(midi.PL_LoadOk)
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": new_fp, "load": True}])
        self.assertEqual(len(self.link.take("sync")), 1)
        self.link.incoming.append({"t": "state", "state": {"m/4/v": 0.1}, "s": 4})
        self.tick(10)
        self.assertEqual(self.fl.volume(4), 0.1)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: synkar igen")

    def test_a_snapshot_with_our_structure_keeps_us_in_sync(self):
        self.join(peers=[self.KIM])
        self.link.incoming.append({"t": "snapshot", "v": 2, "name": "Beat", "fp": self.structure(), "by": "Kim",
                                   "from": "c2"})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: Kim delade en ny version av Beat")
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/4/v", 0.1]], "s": 3})
        self.tick(3)
        self.assertEqual(self.fl.volume(4), 0.1)

    def test_alone_the_relay_takes_over_a_new_structure(self):
        self.join()
        self.add_channel()
        self.tick(25)
        new_fp = self.structure()
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": new_fp}])
        self.assertNotIn("pausad", " ".join(self.fl.project.hints))
        self.link.incoming.append({"t": "reshare"})
        self.tick(3)
        batches = self.sent_batches()
        self.assertTrue(batches)
        self.assertTrue(all(b["fp"] == new_fp and not b.get("seed") for b in batches))

    def test_a_refused_batch_stops_being_pending(self):
        self.join(peers=[self.KIM])
        self.fl.set_volume(1, 0.2)
        self.tick()
        batch = self.sent_batches()[0]
        self.link.incoming.append({"t": "ack", "l": batch["l"], "s": 7, "stale": True})
        self.tick()
        self.assertNotIn("m/1/v", self.client.pending)

    def test_loading_another_structure_with_others_pauses(self):
        self.join(peers=[self.KIM])
        midi = self.fl.project.modules["midi"]
        self.client.project_load(midi.PL_Start)
        self.add_channel()
        self.client.project_load(midi.PL_LoadOk)
        self.assertEqual(len(self.link.take("fp")), 1)
        self.assertEqual(self.link.take("sync"), [])
        self.wait_for_hint("FLSync pausad: öppna samma projekt som de andra")

    def test_swedish_genitive(self):
        genitive = self.fl.script.flsync_client._genitive
        self.assertEqual([genitive("Kim"), genitive("Jonas"), genitive("Max"), genitive(None)],
                         ["Kims", "Jonas", "Max", "någons"])

    # --- notes ------------------------------------------------------------------------

    def test_the_bridge_hears_which_pattern_and_channel_the_piano_roll_shows(self):
        self.tick()
        name = self.fl.project.modules["channels"].getChannelName(0, 1)
        self.assertEqual(self.link.take("pr"), [{"t": "pr", "p": 1, "c": 0, "n": name}])
        self.tick(10)
        self.assertEqual(self.link.take("pr"), [])  # only changes
        self.fl.project.rack.selected = {2}
        self.tick(10)
        self.assertEqual([(m["p"], m["c"]) for m in self.link.take("pr")], [(1, 2)])

    def test_notes_from_others_are_announced(self):
        self.join(peers=[self.KIM])
        self.link.incoming.append({"t": "notes", "p": 2, "c": 4, "by": "Kim", "from": "c2", "n": 32})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: Kim skickade noter till kanal 5 i mönster 2")
        self.link.incoming.append({"t": "notes", "p": 2, "c": 4, "by": "A", "from": "c1", "n": 12})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: dina noter är skickade (12 st)")

    # --- phase 3: FL version, opened versions, the pen ------------------------------------

    def test_hello_says_which_fl_this_is(self):
        self.tick()
        self.assertEqual(self.link.take("hello")[0]["flv"], "25.2.4.4960")

    def test_an_opened_version_counts_as_its_structure_even_with_a_plugin_missing(self):
        theirs = "0123456789ab"  # what the version reads as on Kim's computer (with the plugin)
        self.join(peers=[self.KIM], fp=theirs)
        self.wait_for_hint("FLSync pausad")
        self.link.incoming.append({"t": "loaded", "v": 3, "fp": theirs})  # the bridge saw us open version 3
        self.tick(10)
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": theirs, "load": True}])  # no pen for opening
        self.assertEqual(len(self.link.take("sync")), 1)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: synkar igen")
        self.fl.set_volume(2, 0.5)
        self.tick()
        self.assertEqual([b["fp"] for b in self.sent_batches()], [theirs])
        # Changing the structure here ends it: we're our own structure again, and paused.
        self.add_channel()
        self.tick(25)
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])
        self.assertIn("du har ändrat kanaler eller plugins", self.fl.project.hints[-1])

    def test_a_project_load_ends_the_alias(self):
        self.join(peers=[self.KIM], fp="0123456789ab")
        self.link.incoming.append({"t": "loaded", "v": 3, "fp": "0123456789ab"})
        self.tick(3)
        midi = self.fl.project.modules["midi"]
        self.client.project_load(midi.PL_Start)
        self.add_channel()  # another project
        self.client.project_load(midi.PL_LoadOk)
        self.assertEqual(self.link.take("fp")[-1], {"t": "fp", "fp": self.structure(), "load": True})

    def test_the_pen_says_who_changes_the_structure(self):
        self.join(peers=[self.KIM])
        self.link.incoming.append({"t": "pen", "id": "c2", "name": "Kim"})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: Kim ändrar projektets struktur")
        self.link.incoming.append({"t": "pen", "id": "c2", "name": "Kim", "busy": True})
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: Kim ändrar redan strukturen, din ändring kan skrivas över")
        self.link.incoming.append({"t": "pen", "id": None})
        self.tick(10)
        self.assertIsNone(self.client.pen)
        self.link.incoming.append({"t": "pen", "id": "c1", "name": "A"})  # our own: no extra hint
        self.tick(10)
        self.assertEqual(self.client.pen, {"id": "c1", "name": "A"})
        self.assertNotIn("A ändrar", " ".join(self.fl.project.hints))

    def test_a_version_tells_which_plugins_are_missing_and_if_fl_is_too_old(self):
        self.join(peers=[self.KIM])
        notice = {"t": "snapshot", "v": 2, "name": "Beat", "fp": self.structure(), "by": "Kim", "from": "c2"}
        self.link.incoming.append(notice)
        self.tick(10)
        self.link.incoming.append(dict(notice, path="C:\\FLSync\\Beat\\Beat.flp",
                                       missing=[{"plugin": "Serum", "channels": [3, 7]}, {"plugin": "Pro-Q 3", "mixer": True},
                                                "Kontakt"]))
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: saknas här: Serum (kanal 3, 7), Pro-Q 3 (mixern), Kontakt")
        self.assertTrue(any("synken fungerar ändå" in line for line in self.fl.logs))
        self.link.incoming.append(dict(notice, v=3, newer="26.1.7.5512"))
        self.tick(10)
        self.assertEqual(self.fl.project.hints[-1], "FLSync: sparad i FL 26.1.7.5512, tryck Ctrl+S för att få den i din FL")
        self.assertTrue(any("FL 26.1.7.5512 och du har 25.2.4.4960" in line for line in self.fl.logs))

    def test_loaded_is_ignored_after_the_structure_was_edited(self):
        self.join(peers=[self.KIM], fp="0123456789ab")
        self.add_channel()  # edited since FL opened it: the structure is ours, not a version's
        self.tick(25)
        self.link.take("fp")
        self.link.incoming.append({"t": "loaded", "v": 3, "fp": "0123456789ab"})
        self.tick(5)
        self.assertEqual(self.link.take("fp"), [])
        self.assertIsNone(self.client.alias)

    # --- inserts and channels that get other numbers (FL 2025+) ---------------------------

    def name_inserts(self):
        """Give every insert a name and volume of its own, as FL had them when the project opened."""
        for track in range(1, len(self.fl.mixer.tracks)):
            self.fl.mixer.tracks[track].name = "Spår %d" % track
            self.fl.mixer.tracks[track].vol = 500 * track
        self.client.start()

    def delete_insert(self, track, report=True):
        """Delete an insert as FL 2025 does: the ones after it, the sends to them and
        the channels sent to them move down. FL reports it for all tracks (-1)."""
        mixer = self.fl.mixer
        del mixer.tracks[track]
        for t in mixer.tracks:
            t.routes = {(d - 1 if d > track else d): level for d, level in t.routes.items() if d != track}
        for channel in self.fl.project.rack.channels:
            channel.target = 0 if channel.target == track else channel.target - (channel.target > track)
        if report:
            self.fl.project.dirty_mixer.append(-1)

    def test_a_deleted_insert_pauses_before_anything_it_shifted_is_sent(self):
        self.name_inserts()
        self.join(peers=[self.KIM])
        self.delete_insert(3)
        self.tick()  # FL reported it: the structure is checked before anything is sent
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])
        self.tick(40)
        self.assertEqual(self.sent_batches(), [])  # not m/3/n = "Spår 4", c/3/t = 3, ...
        self.assertIn("du har ändrat kanaler eller plugins", self.fl.project.hints[-1])

    def test_others_values_still_waiting_are_dropped_when_inserts_get_other_numbers(self):
        self.name_inserts()
        self.join(peers=[self.KIM])
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [["m/4/n", "Bas"]], "s": 3})
        self.delete_insert(3)
        self.tick()
        self.assertEqual(self.fl.api.getTrackName(4), "Spår 5")  # "Bas" was for the insert that was 4

    def test_a_deleted_insert_fl_does_not_report_sends_nothing_it_shifted(self):
        self.name_inserts()
        self.join(peers=[self.KIM])
        self.delete_insert(3, report=False)
        self.tick(25)  # the structure is checked once a second anyway
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])
        self.assertEqual(self.sent_batches(), [])

    def test_alone_a_deleted_insert_is_shared_as_it_is_now(self):
        self.name_inserts()
        self.join()
        self.delete_insert(3)
        self.tick()
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])
        self.assertEqual(self.sent_batches(), [])
        self.link.incoming.append({"t": "reshare"})
        self.tick(3)
        pairs = dict(self.pairs(self.sent_batches()))
        self.assertEqual((pairs["m/3/n"], pairs["m/3/v"], pairs["c/3/t"]), ("Spår 4", 2000 / 12800, 3))

    def test_a_moved_insert_with_an_effect_pauses_before_anything_is_sent(self):
        plugins = self.fl.project.plugins
        plugins.add_effect(3, 0, native("Fruity Limiter", ["Gain"]))
        self.name_inserts()
        self.join(peers=[self.KIM])
        mixer = self.fl.mixer
        mixer.tracks[3], mixer.tracks[4] = mixer.tracks[4], mixer.tracks[3]  # insert 3 moved one to the right
        plugins.effects[(4, 0)] = plugins.effects.pop((3, 0))
        self.fl.project.dirty_mixer.append(-1)
        self.tick()
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])
        self.tick(30)
        self.assertEqual(self.sent_batches(), [])

    def test_moved_inserts_without_effects_are_values_like_any_others(self):
        # The structure stays the same: the others get the inserts' settings in their new places.
        self.name_inserts()
        self.join(peers=[self.KIM])
        mixer = self.fl.mixer
        mixer.tracks[3], mixer.tracks[4] = mixer.tracks[4], mixer.tracks[3]
        self.fl.project.dirty_mixer.append(-1)
        self.tick()
        self.assertEqual(self.link.take("fp"), [])
        pairs = dict(self.pairs(self.sent_batches()))
        self.assertEqual((pairs["m/3/n"], pairs["m/4/n"]), ("Spår 4", "Spår 3"))

    def test_a_new_channel_pauses_at_once(self):
        self.join(peers=[self.KIM])
        self.add_channel()
        for index, flag in self.fl.project.dirty_channels:  # FL says: a new channel
            self.client.on_dirty_channel(index, flag)
        self.tick()
        self.assertEqual(self.link.take("fp"), [{"t": "fp", "fp": self.structure()}])

    def test_selecting_or_renaming_channels_costs_no_structure_check(self):
        self.join(peers=[self.KIM])
        checks = []
        fingerprint = self.client.fingerprint
        self.client.fingerprint = lambda: checks.append(self.now) or fingerprint()
        midi = self.fl.project.modules["midi"]
        for index, flag in ((-1, midi.CE_Select), (2, midi.CE_Select), (2, midi.CE_Rename)):
            self.client.on_dirty_channel(index, flag)
        self.fl.set_volume(3, 0.5)
        self.tick()
        self.assertEqual(checks, [])
        self.assertEqual(self.pairs(self.sent_batches()), [("m/3/v", 0.5)])

    def test_reports_over_and_over_cost_few_structure_checks_and_hold_up_no_change(self):
        self.join(peers=[self.KIM])
        checks = []
        fingerprint = self.client.fingerprint
        self.client.fingerprint = lambda: checks.append(self.now) or fingerprint()
        for step in range(40):  # 2 s in which FL says "all mixer tracks changed" before every tick
            if step == 20:
                self.fl.set_volume(3, 0.5)
            self.fl.project.dirty_mixer.append(-1)
            self.tick()
            if step == 20 + 5:
                self.assertEqual(self.pairs(self.sent_batches()), [("m/3/v", 0.5)])
        interval = self.fl.script.flsync_client.FP_REPORT_INTERVAL
        self.assertLessEqual(len(checks), 2.0 / interval + 2)
        self.assertEqual(self.link.take("fp"), [])

    # --- what FL can't read right, and errors ---------------------------------------------

    def test_a_value_fl_reads_as_nan_is_never_sent(self):
        self.join()
        self.fl.mixer.tracks[3].vol = float("nan")  # a value that never equals itself
        self.fl.mixer.tracks[4].pan = float("inf")
        self.tick(40)
        self.assertEqual(self.sent_batches(), [])
        self.assertEqual(self.client.shadow["m/3/v"], 0.8)
        self.fl.mixer.tracks[3].vol = 6400
        self.tick(40)
        self.assertEqual(self.pairs(self.sent_batches()), [("m/3/v", 0.5)])

    def drive_by_script(self):
        """FL's callbacks (device_FLSync.py) drive this client from now on."""
        self.fl.script._client = self.client

    def script_tick(self):
        self.now += 0.05
        self.fl.script._run(self.fl.script.SyncClient.tick, self.now)

    def test_one_value_that_fails_does_not_cost_the_others(self):
        self.join()
        self.drive_by_script()
        write = self.extra.write

        def broken(key, value):
            if key == "x/val":
                raise RuntimeError("trasig binding")
            return write(key, value)
        self.extra.write = broken
        self.link.incoming.append({"t": "sets", "from": "c2", "s": 3,
                                   "kv": [["m/2/v", 0.25], ["x/val", 5], ["m/4/v", 0.5]]})
        self.script_tick()
        self.assertEqual((self.fl.volume(2), self.fl.volume(4)), (0.25, 0.5))
        self.assertEqual(self.fl.script._errors, 1)  # still counted and logged
        self.assertTrue(any("trasig binding" in line for line in self.fl.logs))
        self.tick(5)
        self.assertEqual(self.sent_batches(), [])

    def test_a_message_that_fails_does_not_cost_the_ones_after_it(self):
        self.join()
        self.fl.set_volume(1, 0.2)
        self.tick()
        batch, = self.sent_batches()
        self.drive_by_script()

        def broken(msg):
            raise RuntimeError("trasigt meddelande")
        self.client._on_notes = broken
        self.link.incoming.append({"t": "notes", "p": 1, "c": 0, "by": "Kim", "from": "c2", "n": 3})
        self.link.incoming.append({"t": "ack", "l": batch["l"], "s": 2})
        self.script_tick()
        self.assertNotIn("m/1/v", self.client.pending)
        self.assertEqual(self.fl.script._errors, 1)


class ScriptTest(unittest.TestCase):
    """device_FLSync.py: no exception gets through to FL, and errors now and
    then over a long session don't turn FLSync off."""

    def setUp(self):
        self.fl = FakeFL("A")
        self.script = self.fl.script
        self.clock = 1000.0
        self.script.time = types.SimpleNamespace(monotonic=lambda: self.clock)

    def tearDown(self):
        self.fl.close()

    @staticmethod
    def fail(client, *args):
        raise RuntimeError("fel i klienten")

    def test_errors_now_and_then_dont_turn_it_off(self):
        for _ in range(5):  # five minutes, almost too many errors in each
            for _ in range(self.script.MAX_ERRORS - 1):
                self.script._run(self.fail)
                self.clock += 1.0
            self.clock += 60.0
        self.assertIsNotNone(self.script._client)
        self.assertEqual(sum("fel i klienten" in line for line in self.fl.logs), 5)  # the first of each burst

    def test_many_errors_in_a_minute_turn_it_off(self):
        for _ in range(self.script.MAX_ERRORS):
            self.script._run(self.fail)
            self.clock += 2.0
        self.assertIsNone(self.script._client)
        self.assertTrue(any("för många fel" in line for line in self.fl.logs))

    def test_a_start_that_fails_is_logged_and_leaves_flsync_off(self):
        def broken():
            raise RuntimeError("trasig start")
        self.script.make_bindings = broken
        self.script.OnInit()  # mustn't raise into FL
        self.assertIsNone(self.script._client)
        self.assertTrue(any("trasig start" in line for line in self.fl.logs))
        self.script.OnIdle()
        self.script.OnRefresh(4)
        self.script.OnDirtyMixerTrack(-1)
        self.assertEqual(self.script._errors, 0)

if __name__ == "__main__":
    unittest.main()
