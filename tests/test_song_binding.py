"""The song and playlist bindings (s/, pl/) against the fake FL, and the song
keys end to end through the real sync client with a fake link."""

import unittest

from fake_fl import FakeFL
from fake_song import DEFAULT_COLOR, TICKS_PER_REFRESH

PPQ = 96


class FakeLink:
    """Stands in for the MIDI link: tests feed it messages and read what was sent."""

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


class SongTestCase(unittest.TestCase):
    def setUp(self):
        self.fl = FakeFL("A")
        self.song = self.fl.project.song
        self.playlist = self.fl.project.modules["playlist"]
        self.now = 100.0

    def tearDown(self):
        self.assertEqual(self.fl.project.invalid, [])
        self.fl.close()


class SongBindingTest(SongTestCase):
    def setUp(self):
        super().setUp()
        self.s, = self.fl.bindings("s")

    def read(self, refresh=True):
        """One step of FL and the binding: FL catches up, then the song is read."""
        if refresh:
            self.fl.project.refresh()
        self.now += 0.05
        self.s.tick(self.now)
        unit, = self.s.units()
        return dict(self.s.read(unit))

    def test_reads_the_song_settings(self):
        keys = self.read()
        self.assertEqual(keys, {"s/tempo": 140.0, "s/num": 4, "s/loop": 0, "s/pos": -1, "s/play": False})
        self.assertLess(list(keys).index("s/pos"), list(keys).index("s/play"))
        self.fl.mixer.tempo = 123.456
        self.song.num = 3
        self.song.loop = 1
        self.assertEqual(self.read(), {"s/tempo": 123.456, "s/num": 3, "s/loop": 1, "s/pos": -1,
                                       "s/play": False})

    def test_tempo_is_read_right_however_fl_scales_it(self):
        mixer = self.fl.project.modules["mixer"]
        self.fl.mixer.tempo = 97.5
        self.assertEqual(mixer.getCurrentTempo(), 97500)  # FL 2025.2.4
        self.assertEqual(self.read()["s/tempo"], 97.5)
        # The documented way round, in case a later FL turns it around.
        mixer.getCurrentTempo = lambda asInt=False: (
            int(round(self.fl.mixer.tempo * 1000)) if asInt else self.fl.mixer.tempo)
        self.assertEqual(self.read()["s/tempo"], 97.5)

    def test_swing_is_synced_where_fl_has_it(self):
        self.assertNotIn("s/swing", self.read())  # FL 2025.2.4 (API 40) has no shuffle functions
        self.assertIsNone(self.s.write("s/swing", 0.5))
        channels = self.fl.project.modules["channels"]
        shuffle = {"value": 0.0}
        channels.getShuffle = lambda: shuffle["value"]  # FL 2026 (API 42)

        def set_shuffle(value):
            shuffle["value"] = round(value * 128) / 128  # FL keeps it in steps
        channels.setShuffle = set_shuffle
        self.assertEqual(self.read()["s/swing"], 0.0)
        self.assertEqual(self.s.write("s/swing", 0.3), 0.2969)  # 38 steps of 128
        self.assertEqual(self.s.write("s/swing", 7), 1.0)
        self.assertIsNone(self.s.write("s/swing", "mycket"))
        self.assertEqual(self.s.describe("s/swing"), "Swing")

    def test_swing_has_fl_2026s_names(self):
        channels = self.fl.project.modules["channels"]
        swing = {"value": 0.25}
        channels.getSwing = lambda: swing["value"]  # what FL 2026.1.7's engine has (no getShuffle)
        channels.setSwing = lambda value: swing.update(value=value)
        self.assertEqual(self.read()["s/swing"], 0.25)
        self.assertEqual(self.s.write("s/swing", 0.5), 0.5)

        def refuses():
            raise TypeError("Operation unsafe at current time")
        channels.getSwing = refuses
        values = self.read()
        self.assertNotIn("s/swing", values)  # only swing is left out: tempo and play still sync
        self.assertIn("s/tempo", values)
        self.assertIn("s/play", values)

    def test_a_numerator_fl_does_not_report_whole_is_left_out(self):
        self.fl.project.modules["general"].getRecPPB = lambda: PPQ * 7 // 2
        self.assertNotIn("s/num", self.read())

    def test_writes_land_in_fl_and_return_what_fl_holds(self):
        cases = [("s/tempo", 128.5, 128.5), ("s/tempo", 99.12345, 99.123), ("s/tempo", 1000, 522.0),
                 ("s/tempo", 3, 10.0), ("s/num", 3, 3), ("s/num", 7.0, 7), ("s/loop", 1, 1),
                 ("s/loop", 0, 0)]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.s.write(key, value), expected)
                self.assertEqual(self.read()[key], expected)
        self.assertEqual((self.fl.mixer.tempo, self.song.num, self.song.loop), (10.0, 7, 0))

    def test_tempo_is_set_through_its_rec_event(self):
        # The fake's mixer.setCurrentTempo raises "Range error", as FL 2025.2.4 does.
        self.assertEqual(self.s.write("s/tempo", 133.5), 133.5)
        self.assertEqual(self.fl.mixer.tempo, 133.5)
        modules = self.fl.project.modules
        modules["general"].processRECEvent = lambda event, value, flags: modules["midi"].REC_InvalidID
        self.assertIsNone(self.s.write("s/tempo", 90.25))
        self.assertEqual(self.fl.mixer.tempo, 133.5)

    def test_a_write_fl_refuses_changes_nothing(self):
        def unsafe(*args):
            raise TypeError("Operation unsafe at current time")
        modules = self.fl.project.modules
        modules["transport"].start = modules["general"].processRECEvent = unsafe
        modules["general"].setNumerator = modules["transport"].setLoopMode = unsafe
        self.read()
        self.assertEqual(self.s.write("s/pos", 8), 8.0)
        for key, value in [("s/play", True), ("s/tempo", 120), ("s/num", 3), ("s/loop", 1)]:
            with self.subTest(key=key):
                self.assertIsNone(self.s.write(key, value))
        keys = self.read()
        self.assertEqual(keys, {"s/tempo": 140.0, "s/num": 4, "s/loop": 0, "s/pos": 8.0, "s/play": False})
        self.assertFalse(self.song.playing_real)

    def test_loop_mode_is_toggled_only_when_it_differs(self):
        self.assertEqual(self.s.write("s/loop", 0), 0)
        self.assertEqual(self.song.calls, [])
        self.assertEqual(self.s.write("s/loop", 1), 1)
        self.assertEqual(self.s.write("s/loop", 1), 1)
        self.assertEqual(self.song.calls, [("setLoopMode",)])

    def test_bad_values_and_keys_this_binding_does_not_have(self):
        for key, value in [("s/tempo", "fort"), ("s/tempo", True), ("s/tempo", None),
                           ("s/num", 0), ("s/num", 3.5), ("s/num", 33), ("s/num", "4"), ("s/num", True),
                           ("s/loop", 2), ("s/loop", "song"), ("s/den", 8), ("s/pos", -2),
                           ("s/pos", "start"), ("s/pos", True), ("s/play", "ja"), ("s/play", None),
                           ("s/play", 2), ("s/rec", True), ("s", 1)]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(self.s.write(key, value))
        self.assertEqual(self.song.calls, [])
        self.assertEqual((self.fl.mixer.tempo, self.song.num, self.song.den), (140.0, 4, 4))

    def test_only_play_and_position_are_transient(self):
        self.assertTrue(self.s.transient("s/play"))
        self.assertTrue(self.s.transient("s/pos"))
        for key in ("s/tempo", "s/num", "s/loop"):
            self.assertFalse(self.s.transient(key))

    def test_descriptions(self):
        expected = {"s/tempo": "Tempo", "s/num": "Taktart", "s/loop": "Mönster-/låtläge",
                    "s/play": "Uppspelning", "s/pos": "Startposition"}
        for key, text in expected.items():
            self.assertEqual(self.s.describe(key), text)

    # --- play and stop here -----------------------------------------------------

    def test_pressing_play_gives_where_playback_started_and_keeps_it(self):
        self.song.move_to(16 * PPQ)  # bar 5
        self.read()
        self.song.press_play()
        keys = self.read()  # FL has played a little by the time we look
        self.assertEqual((keys["s/pos"], keys["s/play"]), (16.0, True))
        self.assertEqual(self.song.pos, 16 * PPQ + TICKS_PER_REFRESH)
        for _ in range(200):
            self.assertEqual(self.read()["s/pos"], 16.0)
        self.assertGreater(self.song.pos, 16 * PPQ + 2000)
        self.song.press_stop()
        keys = self.read()
        self.assertEqual((keys["s/pos"], keys["s/play"]), (-1, False))
        self.assertEqual(self.song.calls, [])  # reading never drives the transport

    def test_a_jump_just_before_play_is_not_snapped_back(self):
        self.read()
        self.song.move_to(32 * PPQ)
        self.song.press_play()  # in the same tick: the binding never saw the playhead at 32
        self.assertEqual(self.read()["s/pos"], round((32 * PPQ + TICKS_PER_REFRESH) / PPQ, 4))

    # --- play and stop from someone else ------------------------------------------

    def test_remote_play_seeks_first_then_starts(self):
        self.read()
        self.assertEqual(self.s.write("s/pos", 8), 8.0)
        self.assertEqual(self.song.calls, [])  # the position waits for the play
        self.assertTrue(self.s.write("s/play", True))
        self.assertEqual(self.song.calls, [("setSongPos", 8 * PPQ, 2), ("start",)])
        keys = self.read(refresh=False)  # FL doesn't show the start yet...
        self.assertFalse(self.song.playing)
        self.assertEqual((keys["s/pos"], keys["s/play"]), (8.0, True))  # ...but we started it
        for _ in range(40):
            keys = self.read()
        self.assertEqual((keys["s/pos"], keys["s/play"]), (8.0, True))
        self.assertTrue(self.song.playing)
        self.assertEqual(len(self.song.calls), 2)

    def test_remote_play_while_playing_does_not_pause(self):
        self.song.press_play()
        self.read()
        self.s.write("s/pos", 4)
        self.assertTrue(self.s.write("s/play", True))
        self.assertEqual(self.song.calls, [])
        self.read()
        self.assertTrue(self.song.playing)

    def test_remote_stop_stops_once(self):
        self.s.write("s/pos", 0)
        self.s.write("s/play", True)
        self.read()
        self.assertEqual(self.s.write("s/pos", -1), -1)
        self.assertFalse(self.s.write("s/play", False))
        self.assertFalse(self.s.write("s/play", False))
        self.assertEqual(self.song.calls, [("setSongPos", 0, 2), ("start",), ("stop",)])
        keys = self.read()
        self.assertEqual((keys["s/pos"], keys["s/play"]), (-1, False))
        self.assertFalse(self.song.playing)

    def test_start_and_stop_before_fl_shows_either(self):
        self.s.write("s/pos", 4)
        self.s.write("s/play", True)
        self.s.write("s/pos", -1)
        self.s.write("s/play", False)
        self.assertEqual(self.song.calls, [("setSongPos", 4 * PPQ, 2), ("start",), ("stop",)])
        for _ in range(40):
            keys = self.read()
        self.assertEqual((keys["s/pos"], keys["s/play"]), (-1, False))
        self.assertFalse(self.song.playing)

    def test_a_play_without_a_position_starts_here_and_says_where(self):
        self.song.move_to(4 * PPQ)
        self.read()
        self.assertTrue(self.s.write("s/play", True))
        self.assertEqual(self.song.calls, [("start",)])
        self.assertEqual(self.read()["s/pos"], 4.0)


class PlaylistBindingTest(SongTestCase):
    def setUp(self):
        super().setUp()
        self.pl, = self.fl.bindings("pl")

    def test_tracks_are_numbered_from_one_as_in_fl(self):
        self.assertEqual(list(self.pl.units()), list(range(1, 501)))
        self.assertEqual(dict(self.pl.read(1)), {"pl/1/n": "Track 1", "pl/1/c": DEFAULT_COLOR,
                                                 "pl/1/mu": False})
        self.assertEqual(dict(self.pl.read(500))["pl/500/n"], "Track 500")

    def test_writes_land_in_fl_and_return_what_fl_holds(self):
        cases = [("pl/3/n", "Trummor", "Trummor"), ("pl/3/c", 0x3366CC, 0x3366CC),
                 ("pl/3/c", -10261391, -10261391), ("pl/3/mu", True, True), ("pl/3/mu", False, False),
                 ("pl/3/n", "Track 3", "Track 3")]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.pl.write(key, value), expected)
                self.fl.project.refresh()
                self.assertEqual(dict(self.pl.read(3))[key], expected)
        self.assertEqual(self.song.tracks[3].name, "")  # back to FL's own default name

    def test_mute_is_set_not_toggled(self):
        self.assertTrue(self.pl.write("pl/5/mu", True))
        self.assertFalse(self.playlist.isTrackMuted(5))  # FL shows it after its next refresh
        self.assertFalse(self.pl.write("pl/5/mu", False))  # a second change before that
        self.fl.project.refresh()
        self.assertFalse(self.playlist.isTrackMuted(5))
        self.assertTrue(self.pl.write("pl/5/mu", 1))
        self.fl.project.refresh()
        self.assertTrue(self.playlist.isTrackMuted(5))

    def test_a_write_fl_refuses_returns_none(self):
        def unsafe(*args):
            raise TypeError("Operation unsafe at current time")
        self.playlist.setTrackName = self.playlist.setTrackColor = self.playlist.muteTrack = unsafe
        for key, value in [("pl/3/n", "Bas"), ("pl/3/c", 0x3366CC), ("pl/3/mu", True)]:
            with self.subTest(key=key):
                self.assertIsNone(self.pl.write(key, value))

    def test_keys_this_fl_does_not_have(self):
        for key, value in [("pl/0/n", "x"), ("pl/501/mu", True), ("pl/-1/n", "x"), ("pl/03/n", "x"),
                           ("pl/x/n", "x"), ("pl/3/zz", 1), ("pl/3", "x"), ("pl/3/n/x", "x"),
                           ("pl/3/n", 5), ("pl/3/n", None), ("pl/3/c", "röd"), ("pl/3/c", True),
                           ("pl/3/c", 1.5), ("pl/3/mu", "ja"), ("pl/3/mu", 2)]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(self.pl.write(key, value))
        self.assertEqual(self.pl.describe("pl/501/n"), "pl/501/n")

    def test_colors_must_be_what_fl_holds_a_color_in(self):
        for value in (2 ** 31, -2 ** 31 - 1, 2 ** 40, None):
            with self.subTest(value=value):
                self.assertIsNone(self.pl.write("pl/3/c", value))
        self.assertEqual(self.song.tracks[3].color, DEFAULT_COLOR)
        for value in (-2 ** 31, 2 ** 31 - 1):
            self.assertEqual(self.pl.write("pl/3/c", value), value)

    def test_a_loaded_project_has_its_own_untouched_tracks(self):
        self.playlist.setTrackName(200, "Sång")
        list(self.pl.read(200))
        self.assertFalse(self.pl.shared_at_start("pl/3/c", DEFAULT_COLOR))
        for track in self.song.tracks[1:]:  # another project, whose tracks have another color
            track.color, track.name = 0x3366CC, ""
        self.pl.on_project_load()
        self.assertFalse(self.pl.shared_at_start("pl/3/c", 0x3366CC))
        self.assertTrue(self.pl.shared_at_start("pl/3/c", DEFAULT_COLOR))
        hot = set()
        for _ in range(16):
            hot.update(self.pl.tick(self.now)[0])
        self.assertEqual(hot, set(range(1, 33)))  # track 200 isn't in use any more

    def test_descriptions(self):
        self.playlist.setTrackName(3, "Drums")
        self.assertEqual(self.pl.describe("pl/3/n"), "Spellistespår 3 (Drums) namn")
        self.assertEqual(self.pl.describe("pl/4/c"), "Spellistespår 4 färg")
        self.assertEqual(self.pl.describe("pl/4/mu"), "Spellistespår 4 mute")

    def test_untouched_tracks_are_not_starting_values(self):
        self.assertFalse(self.pl.shared_at_start("pl/3/n", "Track 3"))
        self.assertFalse(self.pl.shared_at_start("pl/3/c", DEFAULT_COLOR))
        self.assertFalse(self.pl.shared_at_start("pl/3/mu", False))
        self.assertTrue(self.pl.shared_at_start("pl/3/n", "Track 4"))
        self.assertTrue(self.pl.shared_at_start("pl/3/c", 0x3366CC))
        self.assertTrue(self.pl.shared_at_start("pl/3/mu", True))

    def test_tracks_in_use_are_read_more_often(self):
        def hot_tracks(ticks):
            seen = set()
            for _ in range(ticks):
                reread, rebaseline = self.pl.tick(self.now)
                self.assertLessEqual(len(reread), 2)
                self.assertEqual(rebaseline, ())
                seen.update(reread)
            return seen
        self.assertEqual(hot_tracks(16), set(range(1, 33)))
        self.playlist.setTrackName(100, "Sång")
        list(self.pl.read(100))  # found by the slow sweep
        self.assertEqual(hot_tracks(54), set(range(1, 109)))


class SongClientTest(SongTestCase):
    """The real client with all bindings, fed protocol messages by a fake link."""

    def setUp(self):
        super().setUp()
        self.link = FakeLink()
        self.client = self.fl.client
        self.client.link = self.link

    def tick(self, count=1, seconds=0.05):
        """What FL does between two screen updates, on a test clock."""
        for _ in range(count):
            project = self.fl.project
            project.refresh()
            dirty, project.dirty_mixer = project.dirty_mixer, []
            for track in dirty:
                self.client.on_dirty_mixer_track(track)
            dirty, project.dirty_channels = project.dirty_channels, []
            for index, flag in dirty:
                self.client.on_dirty_channel(index, flag)
            flags, project.refresh_flags = project.refresh_flags, []
            for flag in flags:
                self.client.on_refresh(flag)
            self.now += seconds
            self.client.tick(self.now)

    def join(self, seed=False, state=None):
        self.tick()
        self.assertTrue(self.link.take("hello"))
        self.link.incoming.append({"t": "welcome", "proto": self.fl.script.flsync_client.PROTO, "id": "c1", "name": "A", "seed": seed,
                                   "state": state or {}, "s": 0,
                                   "peers": [{"id": "c2", "name": "Kim", "app": "fl"}]})
        self.tick()

    def remote(self, *pairs):
        self.link.incoming.append({"t": "sets", "from": "c2", "kv": [list(p) for p in pairs], "s": 9})

    def batches(self):
        return [b["kv"] for b in self.link.take("sets")]

    def ack_all(self):
        sent = self.link.take("sets")
        self.link.incoming.extend({"t": "ack", "l": b["l"], "s": 1} for b in sent)
        self.tick()
        return [b["kv"] for b in sent]

    def test_pressing_play_sends_the_position_then_play_in_one_batch(self):
        self.join()
        self.song.move_to(16 * PPQ)
        self.tick()
        self.song.press_play()
        self.tick()
        self.assertEqual(self.ack_all(), [[["s/pos", 16.0], ["s/play", True]]])
        self.tick(200)  # ten seconds of playing: the position moves, s/pos doesn't
        self.assertGreater(self.song.pos, 16 * PPQ + 2000)
        self.assertEqual(self.batches(), [])
        self.song.press_stop()
        self.tick()
        self.assertEqual(self.batches(), [[["s/pos", -1], ["s/play", False]]])

    def test_a_remote_tempo_change_is_written_and_not_sent_back(self):
        self.join()
        self.remote(("s/tempo", 128.5))
        self.tick()
        self.assertEqual(self.fl.mixer.tempo, 128.5)
        self.assertEqual(self.fl.project.hints[-1], "Kim: Tempo")
        self.tick(40)
        self.assertEqual(self.batches(), [])

    def test_a_remote_play_starts_from_its_position_and_a_stop_stops(self):
        self.join()
        self.remote(("s/pos", 8.0), ("s/play", True))
        self.tick()
        self.assertEqual(self.song.calls, [("setSongPos", 8 * PPQ, 2), ("start",)])
        self.assertEqual(self.fl.project.hints[-1], "Kim: Uppspelning")
        self.tick(40)
        self.assertTrue(self.song.playing)
        self.remote(("s/pos", -1), ("s/play", False))
        self.tick(40)
        self.assertFalse(self.song.playing)
        self.assertEqual(self.song.calls[2:], [("stop",)])
        self.assertEqual(self.batches(), [])

    def test_joining_a_playing_session_does_not_start_playback(self):
        self.join(state={"s/play": True, "s/pos": 8.0, "s/tempo": 100.0, "s/loop": 1})
        self.tick(40)
        self.assertFalse(self.song.playing_real)
        self.assertEqual(self.song.calls, [("setLoopMode",)])  # nothing but the loop mode
        self.assertEqual(self.fl.mixer.tempo, 100.0)
        self.link.incoming.append({"t": "state", "state": {"s/play": True, "s/pos": 4.0}, "s": 10})
        self.tick(40)
        self.assertFalse(self.song.playing_real)
        self.assertEqual(self.batches(), [])

    def test_first_fl_shares_song_settings_and_only_the_tracks_in_use(self):
        self.playlist.setTrackName(3, "Drums")
        self.playlist.setTrackColor(7, 0x3366CC)
        self.playlist.muteTrack(9, 1)
        self.tick(20)  # seen by the rotation through the first tracks
        self.join(seed=True)
        sent = self.ack_all()
        pairs = dict(pair for batch in sent for pair in batch)
        self.assertEqual({k: v for k, v in pairs.items() if k.startswith("s/")},
                         {"s/tempo": 140.0, "s/num": 4, "s/loop": 0})
        self.assertEqual({k: v for k, v in pairs.items() if k.startswith("pl/")},
                         {"pl/3/n": "Drums", "pl/7/c": 0x3366CC, "pl/9/mu": True})

    def test_playlist_changes_are_sent_and_remote_ones_not_echoed(self):
        self.join()
        self.playlist.muteTrack(5, 1)
        self.tick(20)
        self.assertEqual(self.ack_all(), [[["pl/5/mu", True]]])
        self.playlist.setTrackName(300, "Ute")  # far past the tracks in use: the slow sweep
        self.tick(260)
        self.assertEqual(self.ack_all(), [[["pl/300/n", "Ute"]]])
        self.remote(("pl/4/mu", True), ("pl/4/n", "Bas"))
        self.tick(40)
        self.assertTrue(self.playlist.isTrackMuted(4))
        self.assertEqual(self.playlist.getTrackName(4), "Bas")
        self.assertEqual(self.batches(), [])

    def test_a_tick_reads_only_a_few_playlist_tracks(self):
        self.join()
        self.tick(5)
        before = self.song.track_reads
        self.tick()
        self.assertLessEqual(self.song.track_reads - before, 4)


if __name__ == "__main__":
    unittest.main()
