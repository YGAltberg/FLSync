"""Phase 4 in FL's script (docs/phase4.md, "FL's script"): edits made while out
of the session, FL's state for the bridge (`st`, `opened`), holding others'
values while a save is merged, and what the bridge says about saves (`saved`)
and others' versions (`snapshot` with `how`).

Mostly driven message by message with a fake link and a controlled clock, like
test_client.py; WireTest runs the script in a simulated FL over the real MIDI
wire format.

    python -m unittest discover -s tests -p "test_client_phase4.py" -v
"""

import os
import time
import unittest

from fake_fl import ROOT, FakeFL, load_module
from test_client import FakeLink

wire = load_module("wire_for_phase4_tests", os.path.join(ROOT, "bridge", "wire.py"))

KIM = {"id": "c2", "name": "Kim", "app": "fl"}
OTHER_FP = "0123456789ab"


class Phase4Test(unittest.TestCase):
    FL_OPTIONS = {}

    def setUp(self):
        self.fl = FakeFL("A", **self.FL_OPTIONS)
        self.link = FakeLink()
        self.client = self.fl.script.SyncClient(
            "A", self.fl.script.make_bindings(), self.link,
            hint=self.fl.project.hints.append, fingerprint=self.fl.script.flsync_structure.fingerprint,
            context=self.fl.script.flsync_notes.context)
        self.client.start()
        self.now = 100.0
        self.module = self.fl.script.flsync_client
        self.midi = self.fl.project.modules["midi"]

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

    def hello(self):
        """Tick until the client says hello (after a lost connection it waits a moment first)."""
        for _ in range(200):
            self.tick()
            hellos = self.link.take("hello")
            if hellos:
                return hellos[-1]
        self.fail("ingen hello")

    def join(self, seed=False, state=None, peers=(KIM,), fp=None):
        """Say hello and get welcomed (by default Kim's FL is there too)."""
        hello = self.hello()
        self.link.incoming.append({"t": "welcome", "proto": self.module.PROTO, "id": "c1", "name": "A",
                                   "seed": seed, "state": {} if state is None else state, "peers": list(peers),
                                   "s": 0, "fp": fp or hello["fp"], "snapshot": None})
        self.tick()

    def drop(self):
        """The bridge says the relay is gone."""
        self.link.incoming.append({"t": "link", "up": False, "msg": "relayn stängde anslutningen"})
        self.tick()
        self.assertFalse(self.client.joined)

    def load(self, status=None, change=None):
        """FL opens a project (`change` runs while it loads)."""
        self.client.project_load(self.midi.PL_Start)
        if change:
            change()
        self.fl.project.changed_flag = 0
        self.client.project_load(self.midi.PL_LoadOk if status is None else status)

    def add_channel(self):
        rack = self.fl.project.rack
        rack.add_channel(len(rack.channels), "Perc")

    def pairs(self):
        return sorted(tuple(pair) for batch in self.link.take("sets") for pair in batch["kv"])

    def remote(self, *pairs):
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [list(pair) for pair in pairs], "s": 5})

    def last_hint(self):
        return self.fl.project.hints[-1] if self.fl.project.hints else None

    def shown(self, text):
        self.tick(10)
        self.assertEqual(self.last_hint(), text)

    def logged(self, text):
        return any(text in line for line in self.fl.logs)


# --- offline edits --------------------------------------------------------------------

class OfflineEditsTest(Phase4Test):
    def test_edits_made_offline_are_sent_when_back(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.fl.set_pan(2, 0.25)
        self.tick(3)
        self.assertEqual(self.link.take("sets"), [])  # nobody to send them to
        self.join(state={"m/3/v": 0.8, "m/2/p": 0.0, "m/4/v": 0.3})  # Kim moved insert 4 meanwhile
        self.assertEqual(self.pairs(), [("m/2/p", 0.25), ("m/3/v", 0.5)])
        self.tick(5)
        self.assertEqual((self.fl.volume(3), self.fl.pan(2), self.fl.volume(4)), (0.5, 0.25, 0.3))
        self.assertEqual(self.pairs(), [])
        self.shown("FLSync: ansluten igen, 2 ändringar du gjorde offline skickades")
        self.assertEqual(self.client.offline_base, {})

    def test_where_the_others_changed_it_too_the_session_wins(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.fl.set_volume(4, 0.25)
        self.tick(3)
        self.join(state={"m/3/v": 0.6, "m/4/v": 0.8})  # Kim moved insert 3 meanwhile
        self.assertEqual(self.pairs(), [("m/4/v", 0.25)])
        self.tick(5)
        self.assertEqual((self.fl.volume(3), self.fl.volume(4)), (0.6, 0.25))
        self.shown("FLSync: ansluten igen, 1 ändring du gjorde offline skickades, "
                   "1 krockade med de andras och ersattes")
        self.assertTrue(self.logged("där gäller deras värden"))

    def test_only_conflicts(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.fl.set_volume(4, 0.25)
        self.tick(3)
        self.join(state={"m/3/v": 0.6, "m/4/v": 0.1})
        self.assertEqual(self.pairs(), [])
        self.shown("FLSync: ansluten igen, 2 ändringar du gjorde offline krockade med de andras och ersattes")
        self.assertEqual((self.fl.volume(3), self.fl.volume(4)), (0.6, 0.1))

    def test_an_edit_undone_offline_or_one_the_session_has_is_not_sent(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.fl.set_volume(4, 0.25)
        self.tick(3)
        self.fl.set_volume(3, 0.8)  # back to what it was
        self.tick(3)
        self.join(state={"m/3/v": 0.6, "m/4/v": 0.25})  # someone made the same change to insert 4
        self.assertEqual(self.pairs(), [])
        self.tick(5)
        self.assertEqual((self.fl.volume(3), self.fl.volume(4)), (0.6, 0.25))
        self.assertNotIn("offline", " ".join(self.fl.project.hints))

    def test_a_project_load_forgets_them(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.load()
        self.fl.set_volume(5, 0.25)  # an edit of the project open now is one again
        self.tick(3)
        self.join(state={"m/3/v": 0.8, "m/5/v": 0.8})
        self.assertEqual(self.pairs(), [("m/5/v", 0.25)])
        self.tick(5)
        self.assertEqual(self.fl.volume(3), 0.8)

    def test_back_in_a_session_with_another_structure_they_are_forgotten(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.join(state={"m/3/v": 0.8}, fp=OTHER_FP)
        self.tick(5)
        self.assertEqual(self.pairs(), [])
        self.assertEqual(self.fl.volume(3), 0.5)  # paused: the session's values aren't for this project
        self.assertTrue(self.logged("1 ändring du gjorde offline skickas inte"))
        self.assertEqual(self.client.offline_base, {})

    def test_changing_the_structure_offline_forgets_them(self):
        self.join()
        session_fp = self.client.my_fp
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.assertIn("m/3/v", self.client.offline_base)
        self.add_channel()
        self.tick(25)  # the fingerprint is looked at once a second
        self.assertEqual(self.client.offline_base, {})
        self.assertTrue(self.logged("projektets kanaler eller plugins ändrades"))
        self.fl.set_volume(4, 0.25)  # with another structure than the one we left: not remembered
        self.tick(3)
        self.assertEqual(self.client.offline_base, {})
        self.join(state={"m/3/v": 0.8}, fp=session_fp)
        self.assertEqual(self.pairs(), [])

    def test_the_first_fl_back_shares_them_with_everything_else(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.join(seed=True, peers=())
        batches = self.link.take("sets")
        self.assertTrue(batches and all(b.get("seed") for b in batches))
        self.assertIn(["m/3/v", 0.5], [pair for b in batches for pair in b["kv"]])
        self.assertEqual(self.client.offline_base, {})
        self.assertNotIn("offline", " ".join(self.fl.project.hints))

    def test_play_and_stop_are_not_offline_edits(self):
        self.join()
        self.drop()
        self.fl.project.song.press_play()
        self.tick(3)
        self.assertEqual(self.client.offline_base, {})
        self.join(state={"s/play": False, "s/pos": -1})
        self.assertEqual(self.pairs(), [])

    def test_a_fresh_start_has_no_offline_edits(self):
        self.fl.set_volume(3, 0.5)  # before FL ever was in a session
        self.tick(3)
        self.join(state={"m/3/v": 0.8})
        self.tick(3)
        self.assertEqual(self.pairs(), [])
        self.assertEqual(self.fl.volume(3), 0.8)

    def test_a_welcome_without_a_proper_state_counts_as_one_without_those_keys(self):
        self.join()
        self.drop()
        self.fl.set_volume(3, 0.5)
        self.tick(3)
        self.join(state="trasig")
        self.assertEqual(self.pairs(), [("m/3/v", 0.5)])

    # Changes the relay hadn't acknowledged when the connection went.

    def test_unacknowledged_changes_are_sent_again_if_the_session_lacks_them(self):
        self.join()
        self.fl.set_volume(3, 0.5)
        self.fl.set_volume(4, 0.25)
        self.tick()
        self.assertEqual(len(self.link.take("sets")), 1)  # and no ack: the connection is going
        self.tick(220)  # 11 s: no longer blocking others' values, still not acknowledged
        self.drop()
        self.join(state={"m/3/v": 0.8, "m/4/v": 0.25})  # the relay had got only insert 4's
        self.assertEqual(self.pairs(), [("m/3/v", 0.5)])
        self.shown("FLSync: ansluten igen, 1 ändring du gjorde offline skickades")

    def test_an_acknowledged_change_is_the_sessions(self):
        self.join()
        self.fl.set_volume(3, 0.5)
        self.tick()
        first = self.link.take("sets")
        self.fl.set_volume(3, 0.6)
        self.fl.set_volume(4, 0.25)
        self.tick()
        self.link.take("sets")
        self.link.incoming.append({"t": "ack", "l": first[0]["l"], "s": 1})  # only the first got through
        self.tick()
        self.drop()
        self.join(state={"m/3/v": 0.5, "m/4/v": 0.1})  # someone changed insert 4 after ours
        self.assertEqual(self.pairs(), [("m/3/v", 0.6)])
        self.tick(5)
        self.assertEqual(self.fl.volume(4), 0.1)

    def test_acknowledged_changes_are_not_offline_edits(self):
        self.join()
        self.fl.set_volume(3, 0.5)
        self.tick()
        self.link.incoming.extend({"t": "ack", "l": b["l"], "s": 1} for b in self.link.take("sets"))
        self.tick()
        self.drop()
        self.join(state={"m/3/v": 0.6})  # changed after ours, by someone else
        self.assertEqual(self.pairs(), [])
        self.tick(5)
        self.assertEqual(self.fl.volume(3), 0.6)


# --- FL's state for the bridge --------------------------------------------------------

class StatusTest(Phase4Test):
    def st(self):
        return [{k: v for k, v in m.items() if k != "t"} for m in self.link.take("st")]

    def test_the_bridge_hears_whether_fl_is_saved_and_plays(self):
        self.tick()
        kinds = [m["t"] for m in self.link.sent]
        self.assertLess(kinds.index("hello"), kinds.index("st"))
        self.assertEqual(self.st(), [{"dirty": 0, "playing": False}])
        self.tick(20)
        self.assertEqual(self.st(), [])  # only changes
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.assertEqual(self.st(), [{"dirty": 1, "playing": False}])
        self.fl.project.song.press_play()
        self.tick(6)
        self.assertEqual(self.st(), [{"dirty": 1, "playing": True}])
        self.fl.project.changed_flag = 2  # autosaved
        self.tick(6)
        self.assertEqual(self.st(), [{"dirty": 2, "playing": True}])
        self.fl.project.changed_flag = 0  # saved
        self.fl.project.song.press_stop()
        self.tick(6)
        self.assertEqual(self.st(), [{"dirty": 0, "playing": False}])

    def test_it_is_read_a_few_times_a_second(self):
        self.tick()
        self.st()
        self.fl.project.changed_flag = 1
        self.tick(2)
        self.assertEqual(self.st(), [])
        self.tick(4)
        self.assertEqual(len(self.st()), 1)

    def test_a_hello_or_a_new_bridge_hears_it_again(self):
        self.join()
        self.st()
        self.tick(10)
        self.assertEqual(self.st(), [])
        self.drop()
        self.hello()
        self.assertEqual(self.st(), [{"dirty": 0, "playing": False}])
        self.link.up = False  # the bridge goes...
        self.tick(3)
        self.link.up = True   # ...and comes back
        self.tick()
        self.assertEqual(self.st(), [{"dirty": 0, "playing": False}])

    def test_what_this_fl_cant_tell_is_left_out(self):
        general = self.fl.project.modules["general"]
        del general.getChangedFlag
        self.tick()
        self.assertEqual(self.st(), [{"playing": False}])
        self.module.transport = None  # no transport module at all
        self.tick(6)
        self.assertEqual(self.st(), [])  # nothing left to say
        self.module.transport = self.fl.project.modules["transport"]

        def unsafe():
            raise TypeError("Operation unsafe at current time")
        general.getChangedFlag = unsafe
        self.tick(6)
        self.assertEqual(self.st(), [{"playing": False}])
        general.getChangedFlag = lambda: "1"  # not a number
        self.tick(6)
        self.assertEqual(self.st(), [])
        general.getChangedFlag = lambda: True
        self.tick(6)
        self.assertEqual(self.st(), [{"dirty": 1, "playing": False}])


class OpenedTest(Phase4Test):
    def test_every_project_load_is_reported_to_the_bridge(self):
        fp = self.client.local_fp
        self.load()  # not in a session
        self.assertEqual(self.link.take("opened"), [{"t": "opened", "fp": fp}])
        self.join()
        self.load()  # the same project again
        self.assertEqual(self.link.take("opened"), [{"t": "opened", "fp": fp}])
        self.load(change=self.add_channel)
        self.assertNotEqual(self.client.local_fp, fp)
        self.assertEqual(self.link.take("opened"), [{"t": "opened", "fp": self.client.local_fp}])
        self.load(self.midi.PL_LoadError)
        self.assertEqual(self.link.take("opened"), [])

    def test_opened_has_the_fingerprint_fl_reads_here(self):
        self.join(fp=OTHER_FP)
        self.link.incoming.append({"t": "loaded", "v": 3, "fp": OTHER_FP})  # a version, a plugin missing here
        self.tick(3)
        self.load()
        self.assertEqual(self.client.my_fp, OTHER_FP)
        self.assertEqual(self.link.take("opened"), [{"t": "opened", "fp": self.client.local_fp}])
        self.assertNotEqual(self.client.local_fp, OTHER_FP)


# --- holding others' values while a save is merged --------------------------------------

class HoldTest(Phase4Test):
    def save(self):
        """The user changes something and saves: FL's changed flag goes 1, then 0."""
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.fl.project.changed_flag = 0

    def test_others_values_wait_after_a_save_until_the_bridge_says_saved(self):
        self.join()
        self.save()
        self.remote(("m/3/v", 0.4))
        self.tick(20)
        self.assertEqual(self.fl.volume(3), 0.8)  # held: FL stays as it was saved
        self.fl.set_volume(2, 0.5)  # our own changes still go out
        self.tick()
        self.assertEqual(self.pairs(), [("m/2/v", 0.5)])
        self.remote(("m/3/v", 0.45), ("m/2/v", 0.1))  # the newest per key waits; ours is newer than theirs
        self.tick(5)
        self.link.incoming.append({"t": "saved", "outcome": "merged", "v": 4, "with": ["Kim"], "open": "later"})
        self.tick(10)
        self.assertEqual((self.fl.volume(3), self.fl.volume(2)), (0.45, 0.5))
        # The values that waited don't push the save's outcome out of the hint bar.
        self.assertEqual(self.last_hint(), "FLSync: sammanslaget med Kims ändringar, öppnas när du stoppar uppspelningen")
        self.remote(("m/4/v", 0.3))  # and live again
        self.tick(10)
        self.assertEqual(self.fl.volume(4), 0.3)
        self.assertEqual(self.last_hint(), "Kim: Insert 4 volym")

    def test_a_value_fl_already_has_isnt_written(self):
        """Writing it would only mark FL's project changed (FL 2025 does), and
        then a version couldn't be opened without asking about unsaved changes."""
        self.join()
        mixer = self.client.binding_for("m/3/v")
        writes, write = [], mixer.write

        def counted(key, value):
            writes.append((key, value))
            return write(key, value)

        mixer.write = counted
        self.remote(("m/3/v", self.fl.volume(3)), ("m/4/v", 0.25))
        self.tick(10)
        self.assertEqual(writes, [("m/4/v", 0.25)])

    def test_the_flag_settling_after_a_load_is_no_save(self):
        """Measured in FL 2025: right after a project load the changed flag can
        go to 1 and back to 0. That isn't the user saving: nothing is held."""
        self.join()
        self.load()
        self.tick()
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.fl.project.changed_flag = 0
        self.tick(6)
        self.remote(("m/3/v", 0.4))
        self.tick(5)
        self.assertEqual(self.fl.volume(3), 0.4)  # written at once
        self.now += self.module.LOAD_SETTLE  # later, a save holds as before
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.fl.project.changed_flag = 0
        self.tick(6)
        self.remote(("m/3/v", 0.6))
        self.tick(5)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_a_save_is_seen_before_others_values_change_fl_again(self):
        self.join()
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.fl.project.changed_flag = 0
        self.remote(("m/3/v", 0.4))  # arrives in the very tick after the save
        self.tick()
        self.assertEqual(self.fl.volume(3), 0.8)

    def test_without_saved_they_come_after_ten_seconds(self):
        self.join()
        self.save()
        self.remote(("m/3/v", 0.4))
        self.tick(190)  # 9.5 s
        self.assertEqual(self.fl.volume(3), 0.8)
        self.tick(20)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_open_now_holds_until_the_merged_version_is_opened(self):
        self.join()
        self.save()
        self.remote(("m/3/v", 0.4))
        self.tick()
        self.link.incoming.append({"t": "saved", "outcome": "merged", "with": ["Kim"], "open": "now"})
        self.tick(300)  # 15 s: past the usual 10
        self.assertEqual(self.fl.volume(3), 0.8)
        self.assertEqual(self.last_hint(), "FLSync: sammanslaget med Kims ändringar, öppnar den gemensamma versionen")
        self.load()  # the bridge opens it
        self.assertEqual(len(self.link.take("sync")), 1)  # the session's values come anew
        self.assertEqual(self.client.inbox, {})
        self.link.incoming.append({"t": "state", "state": {"m/3/v": 0.4}, "s": 9})
        self.tick(3)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_open_now_gives_up_after_thirty_seconds(self):
        self.join()
        self.save()
        self.remote(("m/3/v", 0.4))
        self.tick()
        self.link.incoming.append({"t": "saved", "outcome": "pulled", "with": ["Kim"], "open": "now"})
        self.tick(580)  # 29 s
        self.assertEqual(self.fl.volume(3), 0.8)
        self.tick(30)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_open_now_holds_even_without_a_save_seen(self):
        self.join()
        self.link.incoming.append({"t": "saved", "outcome": "pulled", "with": ["Kim"], "open": "now"})
        self.remote(("m/3/v", 0.4))
        self.tick(20)
        self.assertEqual(self.fl.volume(3), 0.8)

    def test_a_project_load_is_no_save(self):
        self.join()
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.load()  # FL is unchanged after it
        self.link.take("sync")
        self.remote(("m/3/v", 0.4))
        self.tick(2)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_a_save_outside_a_session_holds_nothing(self):
        self.fl.project.changed_flag = 1
        self.tick(6)
        self.fl.project.changed_flag = 0
        self.tick(6)
        self.join(state={"m/3/v": 0.4})
        self.tick(2)
        self.assertEqual(self.fl.volume(3), 0.4)

    def test_a_lost_connection_ends_the_hold(self):
        self.join()
        self.save()
        self.remote(("m/3/v", 0.4))
        self.tick(3)
        self.drop()
        self.join(state={"m/3/v": 0.4})
        self.tick(2)
        self.assertEqual(self.fl.volume(3), 0.4)


# --- what the bridge says -------------------------------------------------------------------

class SavedTest(Phase4Test):
    def saved(self, fields):
        """The hint `saved` with these fields gives (None: none)."""
        before = len(self.fl.project.hints)
        self.link.incoming.append(dict(fields, t="saved"))
        self.tick(10)
        new = self.fl.project.hints[before:]
        return new[-1] if new else None

    def test_how_a_save_went(self):
        self.join()
        cases = [
            ({"outcome": "shared", "v": 4}, "FLSync: din version är delad"),
            ({"outcome": "merged", "v": 5, "with": ["Kim"], "open": "now", "conflicts": 2, "taken": 3},
             "FLSync: sammanslaget med Kims ändringar (2 krockar, dina behölls), öppnar den gemensamma versionen"),
            ({"outcome": "merged", "with": ["Kim", "Jonas"], "open": "later"},
             "FLSync: sammanslaget med Kims och Jonas ändringar, öppnas när du stoppar uppspelningen"),
            ({"outcome": "merged", "with": ["Kim", "Bo", "Max"], "open": "no", "conflicts": 1},
             "FLSync: sammanslaget med Kims, Bos och Max ändringar (1 krock, din behölls), "
             "spara igen (Ctrl+S) för att öppna den"),
            ({"outcome": "merged", "with": ["Kim"]}, "FLSync: sammanslaget med Kims ändringar"),
            ({"outcome": "pulled", "with": ["Kim"], "open": "now"},
             "FLSync: hämtar Kims version, öppnar den gemensamma versionen"),
            ({"outcome": "pulled", "with": ["Kim"], "open": "no"},
             "FLSync: hämtar Kims version, spara igen (Ctrl+S) för att öppna den"),
            ({"outcome": "unchanged"}, None),
            ({"outcome": "unchanged", "open": "later"},
             "FLSync: inget nytt att dela, öppnas när du stoppar uppspelningen"),
            ({"outcome": "unrelated"},
             "FLSync: projektet hör inte till sessionen, öppna sessionens version i FLSync-fönstret"),
            ({"outcome": "replaced", "with": ["Kim"], "why": "kanaler borttagna"},
             "FLSync: kunde inte slås ihop (kanaler borttagna), din version ersatte Kims"),
            ({"outcome": "failed", "why": "relayn svarar inte"}, "FLSync: kunde inte dela projektet (relayn svarar inte)"),
            ({"outcome": "failed"}, "FLSync: kunde inte dela projektet"),
        ]
        for fields, hint in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.saved(fields), hint)
        self.assertTrue(self.logged("sparningen slogs ihop med Kims ändringar till version 5, 3 delar kom från dem, "
                                    "2 delar hade ni båda ändrat och där behölls dina"))
        self.assertTrue(self.logged("Deras finns kvar i mappen Versioner"))
        self.assertTrue(self.logged("Dela mitt projekt"))
        self.assertTrue(self.logged("spara igen (Ctrl+S) för att få den"))

    def test_garbage_fields(self):
        self.join()
        cases = [
            ({}, None),
            ({"outcome": 5, "with": "Kim", "open": 7, "conflicts": "två", "why": {"x": 1}, "v": "fem"}, None),
            ({"outcome": ["merged"], "open": ["now"], "with": {"Kim": 1}}, None),
            ({"outcome": "merged", "with": [None, 3, "", "  ", "Kim", {"n": 1}], "conflicts": -1, "taken": True,
              "open": "sen", "v": True}, "FLSync: sammanslaget med Kims ändringar"),
            ({"outcome": "merged", "conflicts": True, "taken": "3"}, "FLSync: sammanslaget med de andras ändringar"),
            ({"outcome": "pulled", "with": None}, "FLSync: hämtar de andras version"),
            ({"outcome": "replaced", "why": ["a"], "with": [""]},
             "FLSync: kunde inte slås ihop, din version ersatte de andras"),
            ({"outcome": "failed", "why": "  rad ett\nrad två  "}, "FLSync: kunde inte dela projektet (rad ett rad två)"),
            ({"outcome": "failed", "why": "x" * 500}, "FLSync: kunde inte dela projektet (%s)" % ("x" * 100)),
        ]
        for fields, hint in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.saved(fields), hint)
                self.assertIsNone(self.client.hold_until)  # only a real open: "now" holds
        self.assertTrue(self.logged("bryggan sa något okänt om sparningen: 5"))
        # Other new fields, and an ack nobody can match.
        self.link.incoming.append({"t": "ack", "l": [1], "s": 1})
        self.link.incoming.append({"t": "ack", "l": True, "s": 1})
        self.link.incoming.append({"t": "snapshot", "v": 2, "name": "Beat", "fp": self.client.my_fp, "by": 5,
                                   "from": "c2", "how": ["save"], "merged": "Kim"})
        self.shown("FLSync: 5 delade en ny version av Beat")


class SnapshotHowTest(Phase4Test):
    def notice(self, v, fp, how, by="Kim", sender="c2"):
        msg = {"t": "snapshot", "v": v, "name": "Beat", "fp": fp, "from": sender, "how": how, "merged": ["A"]}
        if by is not None:
            msg["by"] = by
        self.link.incoming.append(msg)

    def test_someone_elses_save_says_press_ctrl_s(self):
        self.join()
        fp = self.client.my_fp
        self.notice(2, fp, "save")
        self.shown("FLSync: Kim har sparat, tryck Ctrl+S för att få med ändringarna")
        self.assertTrue(self.logged("spara (Ctrl+S) för att få med Kims ändringar"))
        self.notice(3, fp, "open")
        self.shown("FLSync: Kim delade en ny version av Beat")
        self.notice(4, fp, "save", by=None)
        self.shown("FLSync: någon har sparat, tryck Ctrl+S för att få med ändringarna")
        self.notice(5, fp, "save", by="A", sender="c1")  # our own: as before
        self.shown("FLSync: din version är delad")

    def test_paused_by_a_saved_version_says_press_ctrl_s(self):
        self.join()
        self.notice(2, OTHER_FP, "save")
        self.shown("FLSync: kanalerna och mixern pausade, tryck Ctrl+S för att få Kims version")
        self.assertTrue(self.logged("pausade tills du sparar (Ctrl+S)"))
        self.notice(3, OTHER_FP, "open", by="Jonas", sender="c3")
        self.shown("FLSync: kanalerna och mixern pausade, öppna Jonas version av projektet")
        self.notice(4, OTHER_FP, None, by="Max", sender="c4")
        self.shown("FLSync: kanalerna och mixern pausade, öppna Max version av projektet")


class AutoSaveTest(Phase4Test):
    """docs/phase5.md: what live sync can't carry goes out in saves FL makes by
    itself, and someone else's version comes in through such a save."""

    FL_OPTIONS = {"fl_version": (26, 1, 7, 5653)}

    def setUp(self):
        super().setUp()
        self.join()
        self.link.incoming.append({"t": "autosave", "ok": True})
        self.tick()
        self.song = self.fl.project.song
        self.asked = 0

    def edit(self):
        """An edit only a saved version carries (a clip placed): an undo step, no live value."""
        self.fl.project.undo += 1

    def saves(self):
        """What the bridge was told about saves FL makes by itself since the last look (and FL was asked to)."""
        taken = self.link.take("autosave")
        self.assertEqual(self.fl.project.save_requests - self.asked, len(taken))
        self.asked = self.fl.project.save_requests
        return taken

    def test_an_edit_live_sync_cant_carry_is_saved_by_itself(self):
        self.edit()
        self.tick(15, 0.1)
        self.assertEqual(self.saves(), [])  # not while the user may still be at it
        self.tick(30, 0.1)
        self.assertEqual(self.saves(), [{"t": "autosave", "content": 1, "pull": False}])
        calls = [c for c in self.song.calls if c[0] == "globalTransport"]
        self.assertEqual(calls, [("globalTransport", 92, 1, 8)])  # FPT_Save, as the main window's Ctrl+S
        self.tick(100, 0.1)
        self.assertEqual(self.saves(), [])  # once: until something else changes

    def test_unsaved_changes_nothing_showed_are_saved_to_be_compared(self):
        self.song.playing = self.song.playing_real = True
        self.fl.project.changed_flag = 1  # an edit FL made no undo step of, nothing live
        self.tick(60, 0.1)
        self.assertEqual(self.saves(), [])  # never while it plays
        self.song.playing = self.song.playing_real = False
        self.tick(50, 0.1)
        self.assertEqual(self.saves(), [{"t": "autosave", "content": 0, "pull": False, "check": True}])
        self.tick(100, 0.1)
        self.assertEqual(self.saves(), [])  # FL still says unsaved: at most every 20 s
        self.tick(100, 0.1)
        self.assertEqual(len(self.saves()), 1)

    def test_fl_refreshing_everything_is_no_edit(self):
        self.client.on_refresh(443735)  # what FL 26.1.7 sends a moment after the script starts or a load
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])
        self.client.on_refresh(131332)  # an edit's (measured): undo, LEDs, mixer controls
        self.tick(40, 0.1)
        self.assertEqual(len(self.saves()), 1)

    def test_a_live_change_is_no_content(self):
        self.edit()
        self.fl.set_volume(2, 0.4)  # the undo step was this fader: the others have it already
        self.tick(60, 0.1)
        self.assertEqual(self.saves(), [])
        self.assertEqual(self.client.content, 0)

    def test_more_patterns_or_a_longer_song_is_content(self):
        self.song.length += 4  # a clip placed after the end
        self.tick(40, 0.1)
        self.assertEqual(len(self.saves()), 1)

    def test_not_while_recording_nor_without_the_bridges_say(self):
        self.song.recording = True
        self.edit()
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])
        self.song.recording = False
        self.link.incoming.append({"t": "autosave", "ok": False})  # e.g. a new project without a file
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])
        self.link.incoming.append({"t": "autosave", "ok": True})
        self.tick(40, 0.1)
        self.assertEqual(len(self.saves()), 1)

    def test_a_save_of_the_users_own_has_it_already(self):
        self.edit()
        self.tick(12, 0.1)
        self.fl.project.changed_flag = 1
        self.tick(2, 0.1)
        self.fl.project.changed_flag = 0  # Ctrl+S
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])

    def test_only_fl_2026_from_26_1_7_saves_by_itself(self):
        can_save = self.module.flsync_content.can_save
        self.assertEqual([can_save(v) for v in ((25, 2, 4), (26, 1, 6), (26, 1, 7), (27, 0, 0), None)],
                         [False, False, True, True, False])

    def test_someone_elses_version_comes_in_once_fl_is_stopped_and_idle(self):
        self.song.playing = self.song.playing_real = True
        self.link.incoming.append({"t": "pull", "by": "Kim", "v": 3})
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])  # never while it plays: loading would stop it
        self.song.playing = self.song.playing_real = False  # stopping is a change here too
        self.tick(25, 0.1)
        self.assertIn("FLSync: hämtar Kims ändringar om 3 s", self.fl.project.hints)
        self.tick(5, 0.1)
        self.fl.set_volume(3, 0.7)  # someone works here: the countdown starts over
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])
        self.tick(20, 0.1)
        self.assertEqual(self.saves(), [{"t": "autosave", "content": 0, "pull": True}])

    def test_an_fl_that_didnt_save_isnt_asked_again(self):
        self.link.incoming.append({"t": "autosave", "ok": False, "broken": True})
        self.tick()
        self.assertFalse(self.client.can_save)
        self.assertIn("FLSync: spara med Ctrl+S för att dela det du gör", self.fl.project.hints)
        self.link.incoming.append({"t": "autosave", "ok": True})
        self.edit()
        self.tick(40, 0.1)
        self.assertEqual(self.saves(), [])


# --- the real script over MIDI ---------------------------------------------------------------

class WireTest(unittest.TestCase):
    """The script in a simulated FL, talking the real MIDI wire format to a
    stand-in for the bridge that only listens (no relay)."""

    def setUp(self):
        self.fl = FakeFL("A")
        self.encoder = wire.Encoder(wire.TO_FL)
        self.decoder = wire.Decoder(wire.FROM_FL)
        self.received = []
        self.fl.to_bridge = self.from_fl

    def tearDown(self):
        self.fl.close()
        self.assertEqual(self.fl.project.invalid, [])

    def from_fl(self, event):
        msg = self.decoder.feed(*wire.unpack(event))
        if isinstance(msg, dict):
            self.received.append(msg)

    def run_until(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.fl.from_bridge.put(self.encoder.heartbeat())  # the bridge is there
            self.fl.idle()
            if condition():
                return True
            time.sleep(0.005)
        return False

    def got(self, kind):
        return [m for m in self.received if m.get("t") == kind]

    def test_fl_tells_the_bridge_its_state_and_what_it_opened(self):
        self.assertTrue(self.run_until(lambda: self.got("st")))
        self.assertEqual(self.got("st"), [{"t": "st", "dirty": 0, "playing": False}])
        self.fl.project.changed_flag = 1
        self.assertTrue(self.run_until(lambda: len(self.got("st")) == 2))
        self.assertEqual(self.got("st")[-1], {"t": "st", "dirty": 1, "playing": False})
        self.fl.load_project({3: 0.5})
        self.assertTrue(self.run_until(lambda: self.got("opened") and self.got("st")[-1].get("dirty") == 0))
        self.assertEqual(self.got("opened"), [{"t": "opened", "fp": self.fl.client.local_fp}])
        self.assertEqual(self.fl.script._errors, 0)


if __name__ == "__main__":
    unittest.main()
