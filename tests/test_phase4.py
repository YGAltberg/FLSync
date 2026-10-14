"""Phase 4 (docs/phase4.md): saves merge, disconnects lose nothing.

Real bridges and a real relay, with minimal FLs speaking the MIDI wire format
(test_snapshots.WireFL), on a song that comes with FL 2025: two people save
different changes and get one project; a save with nothing new opens the
newest; a project that isn't the session's isn't shared until you say so; a
merge that can't be done replaces and keeps a copy; someone saving meanwhile;
FL with unsaved changes or playing; the base across a restart; a newcomer
gets the project opened; a relay that goes quiet.

    python -m unittest discover -s tests -p "test_phase4.py" -v
"""

import asyncio
import json
import os
import struct
import time
import unittest
from unittest import mock

import test_snapshots as snaps
from harness import bridge_mod, free_port, pump, sha256

import flp  # noqa: E402  (the harness put bridge/ on the path)

FL_DIR = os.environ.get("FLSYNC_FL_DIR", r"C:\Program Files (x86)\Image-Line\FL Studio 2025")
SONG = os.path.join(FL_DIR, "Data", "Demo projects", "Demo songs", "Ookay - Thief.flp")
FP_A = snaps.FP_A


# --- editing an .flp at the event level --------------------------------------------------

def decode(data):
    """(header bytes up to the FLdt length, [[id, value bytes], ...])."""
    _, length_at, start, end = flp._chunk(data)
    events, at = [], start
    while at < end:
        eid = data[at]
        vstart = at + 1
        if eid < flp.TEXT:
            vend = vstart + flp.FIXED_SIZE[eid]
        else:
            size = shift = 0
            while True:
                byte = data[vstart]
                vstart += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            vend = vstart + size
        events.append([eid, data[vstart:vend]])
        at = vend
    return data[:length_at], events


def encode(head, events):
    body = b"".join(bytes([eid]) + (snaps.varint(len(value)) if eid >= flp.TEXT else b"") + value
                    for eid, value in events)
    return head + len(body).to_bytes(4, "little") + body


def text(value):
    return (value + "\0").encode("utf-16-le")


def edited(data, change):
    head, events = decode(data)
    change(events)
    return encode(head, events)


def anchor(events, eid, value, before=None):
    """Index of the event `eid` whose value is `value` (a 2-byte number), the
    first one before event id `before` has appeared if given."""
    for i, (e, v) in enumerate(events):
        if before is not None and e == before:
            break
        if e == eid and int.from_bytes(v, "little") == value:
            return i
    raise LookupError((eid, value))


def rename_channel(data, index, name):
    def change(events):
        at = anchor(events, 64, index)
        for e in events[at + 1:]:
            if e[0] == 203:
                e[1] = text(name)
                return
            if e[0] in (64, 65, 99):
                break
        raise LookupError("channel %d has no name" % index)
    return edited(data, change)


def change_note(data, pattern, key):
    """The first note of a pattern gets another key."""
    def change(events):
        at = anchor(events, 65, pattern, before=64)
        notes = events[at + 1]
        assert notes[0] == 224, notes[0]
        value = bytearray(notes[1])
        struct.pack_into("<H", value, 12, key)
        notes[1] = bytes(value)
    return edited(data, change)


def retitle(data, title):
    def change(events):
        for e in events:
            if e[0] == 194:
                e[1] = text(title)
                return
    return edited(data, change)


def touch(data, seconds=1.0):
    """What FL changes on every save: the work time (event 237: two doubles)."""
    def change(events):
        for e in events:
            if e[0] == 237:
                created, worked = struct.unpack("<dd", e[1])
                e[1] = struct.pack("<dd", created, worked + seconds / 86400)
                return
    return edited(data, change)


def set_ppq(data, ppq):
    """The project's time resolution (FLhd), as FL's Project settings change it."""
    return data[:12] + ppq.to_bytes(2, "little") + data[14:]


def set_version(data, version):
    def change(events):
        for e in events:
            if e[0] == 199:
                e[1] = (version + "\0").encode("ascii")
                return
    return edited(data, change)


def add_channel_like(data, source):
    """A copy of channel `source` (its plugin and state) after the last channel,
    as when someone adds that instrument at the end of the channel rack."""
    head, events = decode(data)
    count = sum(1 for e, _ in events if e == 64)
    at = anchor(events, 64, source)
    end = next(i for i in range(at + 1, len(events)) if events[i][0] in (64, 65, 99))
    copy = [[e, v] for e, v in events[at:end]]
    copy[0][1] = count.to_bytes(len(copy[0][1]), "little")
    first_arrangement = next(i for i, (e, _) in enumerate(events) if e == 99)
    events[first_arrangement:first_arrangement] = copy
    head = head[:10] + (count + 1).to_bytes(2, "little") + head[12:]
    return encode(head, events)


def add_sampler_like(data, source, sample=None):
    """A sampler channel made from channel `source` (an audio clip's channel)
    after the last channel, playing `sample` (None: the sample it has)."""
    head, events = decode(add_channel_like(data, source))
    at = anchor(events, 64, sum(1 for e, _ in events if e == 64) - 1)
    end = next(i for i in range(at + 1, len(events)) if events[i][0] in (64, 65, 99))
    for e in events[at:end]:
        if e[0] == 21:
            e[1] = bytes([0])
        elif e[0] == 196 and sample is not None:
            e[1] = text(sample)
    return encode(head, events)


def set_channel_event(data, index, eid, value):
    """Channel `index`'s first event `eid` gets `value` (bytes)."""
    def change(events):
        at = anchor(events, 64, index)
        for e in events[at + 1:]:
            if e[0] == eid:
                e[1] = value
                return
            if e[0] in (64, 65, 99):
                break
        raise LookupError("channel %d has no event %d" % (index, eid))
    return edited(data, change)


def channel_event(data, index, eid):
    """Channel `index`'s first event `eid` (its value bytes)."""
    _, events = decode(data)
    at = anchor(events, 64, index)
    for e, value in events[at + 1:]:
        if e == eid:
            return value
        if e in (64, 65, 99):
            break
    raise LookupError("channel %d has no event %d" % (index, eid))


def preset_sample(path):
    """The sample path a channel preset (.fst) holds."""
    _, events = decode(snaps.read(path))
    return next(value.decode("utf-16-le").rstrip("\0") for eid, value in events if eid == 196)


def channel_names(data):
    return [c.name for c in flp.parse(data).channels]


def first_note_key(data, pattern):
    _, events = decode(data)
    at = anchor(events, 65, pattern, before=64)
    return struct.unpack_from("<H", events[at + 1][1], 12)[0]


# --- the tests ---------------------------------------------------------------------------------

@unittest.skipUnless(os.path.isfile(SONG), "FL Studio 2025:s demolåt finns inte här")
class MergeSession(snaps.Session):
    """Anna and Bo in a session where Anna's project is version 1 (start)."""

    def setUp(self):
        super().setUp()
        self.song = touch(snaps.read(SONG), 0)  # every save of FL's is a new file

    def start(self, bo_status=None):
        """Anna shares the song as version 1; Bo gets it and opens it (his
        bridge then knows his FL's project comes from version 1)."""
        self.project = snaps.save(os.path.join(self.tmp, "Anna", "Beat.flp"), self.song)
        self.anna_fl, self.anna = self.person("Anna", project=self.project)
        self.bo_fl, self.bo = self.person("Bo")
        if bo_status is not None:
            self.bo_fl.send(dict({"t": "st"}, **bo_status))
        snaps.save(self.project, touch(self.song))
        self.wait_for(lambda: self.relay.version == 1)
        self.written = self.received(self.bo)["path"]
        self.bo_content = snaps.read(self.written)  # what Bo's FL has: version 1 (the file may change under it)
        self.open(self.bo, self.bo_fl, self.written)
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"], why="Bos bas blev aldrig version 1")

    def old_file(self, path):
        """Another project of the user's, saved long ago (not a save FL makes now)."""
        snaps.save(path, touch(self.song, 7))
        an_hour_ago = time.time() - 3600
        os.utime(path, (an_hour_ago, an_hour_ago))
        return path

    def open(self, link, fl, path):
        """FL opens `path`: its recent files name it and its script says it loaded a project."""
        link.mru = path
        fl.send({"t": "opened", "fp": fl.fp})

    def saved(self, link, count=1):
        self.wait_for(lambda: len([m for m in link.sent if m.get("t") == "saved" and m["outcome"] != "unchanged"])
                      >= count, timeout=15, why="\n".join(link.logs))
        return [m for m in link.sent if m.get("t") == "saved" and m["outcome"] != "unchanged"][count - 1]

    def newest(self):
        return snaps.read(self.relay.store.path(self.relay.snapshot["flp"]))

    def read_state(self, link):
        """bridge-state.json, read on the bridge's loop: it may be being replaced right now."""
        def read():
            with open(link.bridge.state_path, encoding="utf-8") as f:
                return json.load(f)
        return link.call(read)


class MergeOnSaveTest(MergeSession):
    def test_two_saves_are_merged_and_opened(self):
        self.start()
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.assertEqual(self.relay.snapshot["parent"], 1)  # a fast forward: it builds on version 1
        bo_key = first_note_key(self.song, 1) + 1
        snaps.save(self.written, touch(change_note(self.bo_content, 1, bo_key), 3))
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["with"], answer["open"]), ("merged", ["Anna"], "now"), answer)
        self.assertEqual(self.relay.version, 3)
        snapshot = self.relay.snapshot
        self.assertEqual((snapshot["parent"], snapshot["merged"], snapshot["by"]), (2, ["Anna"], "Bo"))
        merged = self.newest()
        self.assertEqual(channel_names(merged)[2], "Annas kanal")  # hers
        self.assertEqual(first_note_key(merged, 1), bo_key)         # and his
        self.assertEqual(self.bo.opened, [self.written])  # Bo's FL opens the merged version
        self.assertEqual(snaps.read(self.written), merged)
        # Anna hears of it and gets it; her FL is told it includes her version.
        notice = self.received(self.anna)
        self.assertEqual((notice["v"], notice["merged"], notice["how"]), (3, ["Anna"], "save"))
        # Bo's FL opens it: that's his base now, so his next save is a fast forward.
        self.bo_fl.send({"t": "opened", "fp": snaps.FP_A})
        self.wait_for(lambda: self.bo.bridge.base == snapshot["flp"])
        snaps.save(self.written, touch(retitle(merged, "Bos titel"), 4))
        self.wait_for(lambda: self.relay.version == 4)
        self.assertEqual((self.relay.snapshot["parent"], self.relay.snapshot.get("merged")), (3, None))
        self.assertShort(self.bo)

    def test_a_save_with_nothing_new_opens_the_newest(self):
        self.start()
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        snaps.save(self.written, touch(self.bo_content, 5))  # Bo presses Ctrl+S without changes
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["with"], answer["open"]), ("pulled", ["Anna"], "now"), answer)
        self.settle(0.3)
        self.assertEqual(self.relay.version, 2)  # nothing to share
        self.assertEqual(self.bo.opened, [self.written])
        self.assertEqual(channel_names(snaps.read(self.written))[2], "Annas kanal")

    def test_a_save_that_has_everything_is_shared_as_it_is(self):
        self.start()
        snaps.save(self.project, touch(self.song, 2))  # Anna saves without changes: only her work time
        self.settle(0.5)
        bo = touch(rename_channel(self.bo_content, 3, "Bos kanal"), 3)
        snaps.save(self.written, bo)
        answer = self.saved(self.bo)
        self.assertEqual(answer["outcome"], "shared", answer)
        self.assertEqual(self.newest(), bo)
        self.assertEqual(self.bo.opened, [])

    def test_fl_with_unsaved_changes_isnt_reopened(self):
        self.start()
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": False})  # Bo works on ...
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.received(self.bo, count=2)
        self.settle(0.3)
        self.assertEqual(self.bo.opened, [])  # FL has unsaved changes: Anna's version isn't opened
        snaps.save(self.written, touch(change_note(self.bo_content, 1, 50), 3))
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": False})  # ... and goes on right after saving
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["open"]), ("merged", "no"), answer)
        self.settle(0.3)
        self.assertEqual(self.bo.opened, [])

    def test_playing_waits_for_the_stop(self):
        self.start(bo_status={"dirty": 1, "playing": True})
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": True})  # Bo saves while it plays
        snaps.save(self.written, touch(change_note(self.bo_content, 1, 50), 3))
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["open"]), ("merged", "stop"), answer)  # (Ctrl+S: at the stop)
        self.settle(0.3)
        self.assertEqual(self.bo.opened, [])
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        self.wait_for(lambda: self.bo.opened == [self.written])

    def test_a_newcomer_with_nothing_unsaved_gets_the_project_opened(self):
        self.project = snaps.save(os.path.join(self.tmp, "Anna", "Beat.flp"), self.song)
        anna_fl, anna = self.person("Anna", project=self.project)
        snaps.save(self.project, touch(self.song))
        self.wait_for(lambda: self.relay.version == 1)
        bo_fl, bo = self.person("Bo", join=False)
        bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        bo_fl.hello()
        notice = self.received(bo)
        self.wait_for(lambda: bo.opened == [notice["path"]])
        cy_fl, cy = self.person("Cy", join=False)  # Cy's FL has unsaved work: it stays
        cy_fl.send({"t": "st", "dirty": 1, "playing": False})
        cy_fl.hello()
        self.received(cy)
        self.settle(0.3)
        self.assertEqual(cy.opened, [])

    def test_a_project_that_isnt_the_sessions_is_shared_only_when_you_say_so(self):
        self.start()
        mine = snaps.save(os.path.join(self.tmp, "Cy", "Egen.flp"), set_version(self.song, "25.2.3.5164"))
        an_hour_ago = time.time() - 3600
        os.utime(mine, (an_hour_ago, an_hour_ago))
        cy_fl, cy = self.person("Cy", project=mine)
        self.wait_for(lambda: cy.bridge.identifying is not None and cy.bridge.identifying.done())
        self.assertIsNone(cy.bridge.base)
        snaps.save(mine, touch(retitle(self.song, "Cys låt"), 7))
        answer = self.saved(cy)
        self.assertEqual(answer["outcome"], "unrelated")
        self.settle(0.3)
        self.assertEqual(self.relay.version, 1)
        self.assertTrue(cy.bridge.unrelated)
        self.assertTrue(cy.call(cy.bridge.share_mine))  # "Dela mitt projekt"
        answer = self.saved(cy, 2)
        self.assertEqual(answer["outcome"], "shared", answer)
        self.assertEqual((self.relay.version, self.relay.snapshot["name"]), (2, "Egen"))
        self.assertFalse(cy.bridge.unrelated)

    def test_saving_a_project_that_isnt_the_sessions_again_makes_it_everyones(self):
        self.start()
        mine = snaps.save(os.path.join(self.tmp, "Cy", "Egen.flp"), self.song)
        an_hour_ago = time.time() - 3600
        os.utime(mine, (an_hour_ago, an_hour_ago))
        cy_fl, cy = self.person("Cy", project=mine)
        self.wait_for(lambda: cy.bridge.identifying is not None and cy.bridge.identifying.done())
        snaps.save(mine, touch(retitle(self.song, "Cys låt"), 7))
        answer = self.saved(cy)
        self.assertEqual((answer["outcome"], answer["confirm"]), ("unrelated", 15))
        # The others hear of it, so nobody wonders why nothing came.
        self.wait_for(lambda: any(m.get("t") == "note" and m.get("by") == "Cy" for m in self.anna.sent))
        self.assertTrue(any("som inte är sessionens projekt" in line for line in self.anna.logs))
        snaps.save(mine, touch(retitle(self.song, "Cys låt"), 8))  # Ctrl+S again
        answer = self.saved(cy, 2)
        self.assertEqual(answer["outcome"], "switched", answer)
        snapshot = self.relay.snapshot
        self.assertEqual((self.relay.version, snapshot["name"], snapshot.get("switch")), (2, "Egen", True))
        # Anna's FL has nothing unsaved (it never said otherwise): Cy's project opens.
        self.wait_for(lambda: self.anna.opened, why="\n".join(self.anna.logs))
        self.assertIsNone(self.anna.bridge.base)
        # A save of the old project isn't merged with Cy's or shared: FL opens Cy's.
        self.anna.bridge.activity = lambda: (0.0, True)  # (she just pressed Ctrl+S)
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 9))
        answer = self.saved(self.anna, 2)  # (her first: sharing version 1)
        self.assertEqual((answer["outcome"], answer.get("with"), answer.get("open")), ("old", ["Cy"], "now"), answer)
        self.settle(0.3)
        self.assertEqual(self.relay.version, 2)
        self.assertEqual(len(self.anna.opened), 2)

    def test_versions_from_another_fl_version_are_merged_in_your_own(self):
        self.start()
        snaps.save(self.project, touch(set_version(rename_channel(self.song, 2, "Annas kanal"), "26.1.7.5653"), 2))
        self.wait_for(lambda: self.relay.version == 2)  # Anna saves with FL 2026 now
        snaps.save(self.written, touch(change_note(self.bo_content, 1, 50), 3))  # Bo still has FL 2025
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["with"]), ("merged", ["Anna"]), answer)
        merged = self.newest()
        self.assertEqual((channel_names(merged)[2], first_note_key(merged, 1)), ("Annas kanal", 50))
        self.assertEqual(flp.parse(merged).version, flp.parse(self.song).version)  # Bo's FL opens it without a warning

    def test_a_merge_that_cant_be_done_replaces_and_keeps_a_copy(self):
        self.start()
        snaps.save(self.project, touch(set_ppq(rename_channel(self.song, 2, "Annas kanal"), 192), 2))
        self.wait_for(lambda: self.relay.version == 2)  # Anna changed the project's time resolution
        notice = self.received(self.bo, count=2)
        snaps.save(self.written, touch(change_note(self.bo_content, 1, 50), 3))
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["with"]), ("replaced", ["Anna"]), answer)
        self.assertTrue(answer["why"])
        self.assertEqual(self.relay.version, 3)
        self.assertEqual(first_note_key(self.newest(), 1), 50)
        copies = os.listdir(os.path.join(os.path.dirname(notice["path"]), "Versioner"))
        self.assertIn("Beat v2.flp", copies)  # Anna's, as it was
        # Anna gets Bo's version; hers stays in her Versioner folder too.
        written = self.received(self.anna)["path"]
        self.assertEqual(first_note_key(snaps.read(written), 1), 50)

    def test_someone_saving_meanwhile_is_merged_too(self):
        self.start()
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        bridge = self.bo.bridge
        put, held = bridge._put, {"first": True}

        async def put_later(*args, **kwargs):
            if held.pop("first", False):
                # Anna saves again while Bo's merge is on its way to the relay.
                snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas andra namn"), 4))
                for _ in range(200):
                    if self.relay.version == 3 and bridge.newest["v"] == 3:
                        break
                    await asyncio.sleep(0.02)
            return await put(*args, **kwargs)

        bridge._put = put_later
        snaps.save(self.written, touch(change_note(self.bo_content, 1, 51), 3))
        answer = self.saved(self.bo)
        self.assertEqual(answer["outcome"], "merged", answer)
        self.assertEqual(self.relay.version, 4)
        self.assertEqual(self.relay.snapshot["parent"], 3)
        merged = self.newest()
        self.assertEqual((channel_names(merged)[2], first_note_key(merged, 1)), ("Annas andra namn", 51))
        self.assertTrue(any("nyare version under tiden" in line for line in self.bo.logs), self.bo.logs)

    def test_a_version_copied_over_the_file_fl_has_isnt_a_save(self):
        # A synced folder (or someone's FLSync on this PC) puts an older version
        # into the file FL has open. No save of FL's is a version that exists
        # (FL stamps each with its work time): it isn't shared, FL's base stays.
        self.start(bo_status={"dirty": 1, "playing": False})
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.received(self.bo, count=2)
        v2 = snaps.read(self.written)
        self.open(self.bo, self.bo_fl, self.written)  # Bo's FL opens version 2
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"])
        snaps.save(self.written, self.bo_content)  # version 1 comes back from elsewhere
        self.settle(1.5)
        self.assertEqual(self.relay.version, 2)
        self.assertEqual(self.bo.bridge.base, self.relay.snapshot["flp"])
        self.assertEqual([m for m in self.bo.sent if m.get("t") == "saved"], [])  # FL saved nothing
        snaps.save(self.written, touch(change_note(v2, 1, 50), 3))  # Bo saves his work
        self.wait_for(lambda: self.relay.version == 3, why="\n".join(self.bo.logs))
        newest = self.newest()
        self.assertEqual((channel_names(newest)[2], first_note_key(newest, 1)), ("Annas kanal", 50))

    def test_the_base_survives_a_restart(self):
        self.start()
        self.assertEqual(self.read_state(self.bo)["base"], self.relay.snapshot["flp"])
        again = bridge_mod.Bridge("127.0.0.1", self.port, lambda message: None, logger=lambda text: None,
                                  **{k: v for k, v in self.bo.places.items() if k != "opener"})
        self.assertEqual((again.base, again.base_project), (self.relay.snapshot["flp"], self.written))


class PauseTest(MergeSession):
    """Someone's version comes into FL (which reloads the project) at a pause:
    the sharer stopped a while, and the user here too (docs/protocol_v2.md)."""

    def start_clean(self):
        self.start(bo_status={"dirty": 0, "playing": False})  # Bo's FL has nothing unsaved
        return len(self.bo.opened)

    def anna_saves(self, v, seconds=None):
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + v), seconds or v))
        self.wait_for(lambda: self.relay.version == v)

    def test_a_version_opens_once_the_sharer_pauses_after_a_word_in_the_hint_bar(self):
        opened = self.start_clean()
        self.bo.bridge.quiet_sharer, self.bo.bridge.open_warning = 0.8, 0.4
        self.anna_saves(2)
        self.assertEqual(self.received(self.bo, count=2)["what"], ["noter"])  # what it brings, for the window
        self.settle(0.3)
        self.assertEqual(len(self.bo.opened), opened)  # Anna may go on: not yet
        self.wait_for(lambda: {"t": "opening", "by": "Anna", "in": 0.4} in self.bo.sent)
        self.assertEqual(len(self.bo.opened), opened)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)

    def test_not_while_the_user_here_works(self):
        opened = self.start_clean()
        idle = {"s": 2.0}
        self.bo.bridge.activity = lambda: (idle["s"], True)  # FL in front, a key pressed 2 s ago
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.settle(0.5)
        self.assertEqual(len(self.bo.opened), opened)
        idle["s"] = bridge_mod.IDLE_INPUT + 1
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)

    def test_at_once_when_fl_isnt_the_program_in_front(self):
        opened = self.start_clean()
        self.bo_fl.window_open = True
        self.bo.bridge.activity = lambda: (0.5, False)  # typing in another program
        self.anna_saves(2)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)

    def test_never_into_an_fl_that_was_closed(self):
        # (Opening the file would start FL again.)
        opened = self.start_clean()
        self.bo.bridge.activity = lambda: (0.5, False)  # no FL window in front: FL's is gone
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.settle(0.6)
        self.assertEqual(len(self.bo.opened), opened)
        self.bo_fl.window_open = True  # (it was only busy)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)

    def at_work(self):
        """Bo uses FL until the test says he pauses (the returned function)."""
        idle = {"s": 1.0}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        return lambda: idle.update(s=bridge_mod.IDLE_INPUT + 1)

    def test_not_into_an_fl_outside_the_session(self):
        opened = self.start_clean()
        pause = self.at_work()
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.bo.call(self.bo.bridge.close_session)  # Bo leaves ("Lämna") before it opens
        pause()
        self.settle(0.6)
        self.assertEqual(len(self.bo.opened), opened)

    def test_a_version_opened_by_hand_isnt_opened_again(self):
        opened = self.start_clean()
        pause = self.at_work()
        self.anna_saves(2)
        notice = self.received(self.bo, count=2)
        self.open(self.bo, self.bo_fl, notice["path"])  # "Öppna senaste versionen" before the pause
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"])
        pause()
        self.settle(0.6)
        self.assertEqual(len(self.bo.opened), opened)

    def test_a_project_the_user_opens_isnt_replaced(self):
        opened = self.start_clean()
        pause = self.at_work()
        self.anna_saves(2)
        self.wait_for(lambda: self.bo.bridge.to_launch is not None)  # it waits for a pause
        other = self.old_file(os.path.join(self.tmp, "Bo", "Annat.flp"))
        self.open(self.bo, self.bo_fl, other)  # File > Open (FL had nothing unsaved)
        self.wait_for(lambda: self.bo.bridge.to_launch is None)
        pause()
        self.settle(0.6)
        self.assertEqual(len(self.bo.opened), opened, "\n".join(self.bo.logs))

    def test_a_version_written_while_fl_opens_the_one_before_comes_after_it(self):
        # (The same file: FL has what it read as it began opening it, not what was written over it since.)
        opened = self.start_clean()
        self.anna_saves(2)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)
        path, v2 = self.bo.opened[-1], self.relay.snapshot["flp"]
        pause = self.at_work()
        self.anna_saves(3)  # while Bo's FL opens version 2
        self.wait_for(lambda: self.bo.bridge.to_launch is not None and self.bo.bridge.to_launch["v"] == 3)
        self.open(self.bo, self.bo_fl, path)  # FL is done opening version 2
        self.wait_for(lambda: self.bo.bridge.base == v2, why="\n".join(self.bo.logs))
        self.assertEqual(self.bo.bridge.to_launch["v"], 3)  # (still to come)
        pause()
        self.wait_for(lambda: len(self.bo.opened) == opened + 2)

    def test_the_word_in_the_hint_bar_is_taken_back_when_playback_starts(self):
        opened = self.start_clean()
        self.bo.bridge.open_warning = 1.0
        self.anna_saves(2)
        self.wait_for(lambda: {"t": "opening", "by": "Anna", "in": 1.0} in self.bo.sent)
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": True})
        self.wait_for(lambda: {"t": "opening", "cancel": True} in self.bo.sent)
        self.settle(1.2)
        self.assertEqual(len(self.bo.opened), opened)

    def test_a_project_slow_to_open_waits_for_a_longer_pause(self):
        opened = self.start_clean()
        idle = {"s": bridge_mod.IDLE_INPUT + 1}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        self.bo_fl.send({"t": "opened", "fp": self.bo_fl.fp, "took": bridge_mod.SLOW_LOAD + 5})
        self.wait_for(lambda: self.bo.bridge.last_load == bridge_mod.SLOW_LOAD + 5)
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.settle(0.5)
        self.assertEqual(len(self.bo.opened), opened)
        idle["s"] = bridge_mod.SLOW_IDLE + 1
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)

    def test_fl_saving_by_itself_opens_the_merge_at_a_pause(self):
        self.start()
        self.bo.bridge.activity = lambda: (2.0, True)
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})  # Bo's FL saves his note by itself
        snaps.save(self.written, touch(change_note(self.bo_content, 1, first_note_key(self.song, 1) + 1), 3))
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["open"]), ("merged", "later"), answer)
        self.settle(0.5)
        self.assertEqual(self.bo.opened, [])  # Bo is at work: not right after his edit

    def test_a_merge_waiting_for_a_pause_is_the_others_version(self):
        self.start()
        idle = {"s": 2.0}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        self.bo.bridge.open_warning = 0.2
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})
        snaps.save(self.written, touch(change_note(self.bo_content, 1, first_note_key(self.song, 1) + 1), 3))
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        self.assertEqual(self.saved(self.bo)["open"], "later")
        idle["s"] = bridge_mod.IDLE_INPUT + 1
        self.wait_for(lambda: {"t": "opening", "by": "Anna", "in": 0.2} in self.bo.sent)  # (not Bo's own)
        self.wait_for(lambda: self.bo.opened == [self.written])

    def test_ctrl_s_opens_the_merge_at_once(self):
        self.start()
        self.bo.bridge.activity = lambda: (0.0, True)  # the user just pressed Ctrl+S
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        snaps.save(self.written, touch(change_note(self.bo_content, 1, first_note_key(self.song, 1) + 1), 3))
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["open"]), ("merged", "now"), answer)
        self.assertEqual(self.bo.opened, [self.written])

    def test_the_pull_waits_for_a_pause_too(self):
        self.start()
        idle = {"s": 2.0}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        self.bo_fl.send({"t": "st", "dirty": 1})
        self.show(self.bo, self.written)
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.settle(0.6)
        self.assertFalse(any(m.get("t") == "pull" for m in self.bo.sent))
        idle["s"] = bridge_mod.IDLE_INPUT + 1
        self.wait_for(lambda: {"t": "pull", "by": "Anna", "v": 2} in self.bo.sent)

    def test_a_pull_is_taken_back_when_the_pause_ends_before_fl_saves(self):
        self.start()
        idle = {"s": bridge_mod.IDLE_INPUT + 1}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": False})
        self.show(self.bo, self.written)
        self.anna_saves(2)
        pull = {"t": "pull", "by": "Anna", "v": 2}
        self.wait_for(lambda: pull in self.bo.sent)
        idle["s"] = 1.0  # Bo moves the mouse
        self.wait_for(lambda: {"t": "pull", "cancel": True} in self.bo.sent)
        idle["s"] = bridge_mod.IDLE_INPUT + 1
        self.wait_for(lambda: self.bo.sent.count(pull) == 2)  # at the next pause

    def test_a_pull_fl_saved_for_isnt_asked_again(self):
        self.start()
        idle = {"s": bridge_mod.IDLE_INPUT + 1}
        self.bo.bridge.activity = lambda: (idle["s"], True)
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": False})
        self.show(self.bo, self.written)
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        pull = {"t": "pull", "by": "Anna", "v": 2}
        self.wait_for(lambda: pull in self.bo.sent)
        # FL saves for it: it has Anna's change already (live), and a change of Bo's she lacks.
        self.bo_fl.send({"t": "autosave", "content": 0, "pull": True})
        snaps.save(self.written, touch(retitle(rename_channel(self.bo_content, 2, "Annas kanal"), "Bos"), 10))
        self.bo_fl.send({"t": "st", "dirty": 0, "playing": False})
        self.wait_for(lambda: {"t": "saved", "outcome": "unchanged"} in self.bo.sent, why="\n".join(self.bo.logs))
        idle["s"] = 1.0  # Bo moves the mouse...
        self.settle(0.4)
        idle["s"] = bridge_mod.IDLE_INPUT + 1  # ...and pauses again
        self.settle(0.6)
        self.assertEqual(self.bo.sent.count(pull), 1)  # not a save at every pause
        self.assertNotIn({"t": "pull", "cancel": True}, self.bo.sent)

    def test_no_pull_while_fl_plays(self):
        self.start()
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": True})  # Bo listens, hands off
        self.show(self.bo, self.written)
        self.anna_saves(2)
        self.received(self.bo, count=2)
        self.settle(0.6)
        self.assertFalse(any(m.get("t") == "pull" for m in self.bo.sent))
        self.bo_fl.send({"t": "st", "dirty": 1, "playing": False})
        self.wait_for(lambda: {"t": "pull", "by": "Anna", "v": 2} in self.bo.sent)

    def test_a_save_that_has_the_newest_already_moves_the_base(self):
        # Bo's FL got Anna's notes in another way (FLSync hämta noter): nothing to share or open.
        self.start()
        self.anna_saves(2)
        snaps.save(self.written, touch(change_note(self.bo_content, 1, first_note_key(self.song, 1) + 2), 9))
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"], why="\n".join(self.bo.logs))
        self.settle(0.3)
        self.assertEqual(self.relay.version, 2)
        self.assertEqual(self.bo.opened, [])

    def show(self, link, path):
        link.title = "%s - FL Studio 2026" % os.path.basename(path)


class FetchStepsTest(unittest.TestCase):
    """bridge.fetch_steps: where "FLSync hämta noter" is in the piano roll's Tools
    menu, counted from its end (FL's own scripts and the user's, by name)."""

    def setUp(self):
        self.tmp = snaps.tempfile.mkdtemp(prefix="flsync-steps-")
        self.addCleanup(snaps.shutil.rmtree, self.tmp, True)
        self.dirs = {"user_dir": os.path.join(self.tmp, "Image-Line"), "factory_dir": os.path.join(self.tmp, "FL")}

    def scripts(self, user=(), factory=(), folder=None):
        for names, base in ((user, bridge_mod.piano_roll_scripts(self.dirs)),
                            (factory, os.path.join(self.dirs["factory_dir"], "System", "Config", "Piano roll scripts"))):
            for name in names:
                snaps.save(os.path.join(base, folder or "", name + ".pyscript"), b"#")

    def test_it_is_counted_from_the_end_of_the_menu(self):
        self.scripts(user=["FLSync hämta noter", "FLSync skicka noter"], factory=["Arpeggiator", "Humanize", "Euclidean"])
        snaps.save(os.path.join(bridge_mod.piano_roll_scripts(self.dirs), "FLSync", "context.json"), b"{}")  # FLSync's
        self.assertEqual(bridge_mod.fetch_steps(self.dirs), 3)  # Humanize, skicka, hämta: up three times

    def test_without_fls_own_scripts_it_cant_be_told(self):
        # They are in the menu too: counting only the user's would run another script.
        self.scripts(user=["FLSync hämta noter", "FLSync skicka noter"])
        self.assertIsNone(bridge_mod.fetch_steps(self.dirs))  # FL's folder isn't there
        self.assertIsNone(bridge_mod.fetch_steps(dict(self.dirs, factory_dir=None)))  # nor known
        os.makedirs(os.path.join(self.dirs["factory_dir"], "System", "Config", "Piano roll scripts"))
        self.assertEqual(bridge_mod.fetch_steps(self.dirs), 2)

    def test_a_menu_it_cant_be_sure_of_gives_none(self):
        self.assertIsNone(bridge_mod.fetch_steps(self.dirs))  # not installed
        self.scripts(user=["FLSync hämta noter", "Humanize"], factory=["Humanize"])
        self.assertIsNone(bridge_mod.fetch_steps(self.dirs))  # one name twice

    def test_scripts_in_folders_of_their_own_give_none(self):
        self.scripts(user=["FLSync hämta noter"], factory=["Arpeggiator"])
        self.scripts(user=["Mina"], folder="Egna")  # FL may show a submenu for them
        self.assertIsNone(bridge_mod.fetch_steps(self.dirs))


class ChangeWordsTest(unittest.TestCase):
    def test_every_kind_of_change_has_a_word(self):
        self.assertEqual(bridge_mod.change_words([("notes", 1), ("playlist", 0), ("notes", 2)]), ["noter", "klipp"])
        self.assertEqual(bridge_mod.change_words([("markers", 0)]), ["markörer"])  # (the playlist's time markers)


class NotesLiveTest(MergeSession):
    """A version that changed only notes: Bo's FL gets them into its piano rolls
    (its script runs "FLSync hämta noter"), without opening the version."""

    def start_bo(self):
        self.start(bo_status={"dirty": 0, "playing": False})
        scripts = bridge_mod.piano_roll_scripts(self.bo.bridge.fl_dirs)
        snaps.save(os.path.join(scripts, "FLSync hämta noter.pyscript"), b"# the fetch script")
        snaps.save(os.path.join(scripts, "Mina noter.pyscript"), b"# one of the user's")
        factory = os.path.join(self.bo.bridge.fl_dirs["factory_dir"], "System", "Config", "Piano roll scripts")
        snaps.save(os.path.join(factory, "Humanize.pyscript"), b"# one of FL's")
        return len(self.bo.opened)

    def offered(self):
        self.wait_for(lambda: any(m.get("t") == "notes_in" for m in self.bo.sent), why="\n".join(self.bo.logs))
        return next(m for m in self.bo.sent if m.get("t") == "notes_in")

    def fetch(self, offer, i, ok=True, why=None):
        """What Bo's FL and its fetch script do for piano roll `i`."""
        self.bo_fl.send({"t": "notes_at", "nonce": offer["nonce"], "i": i})
        self.wait_for(lambda: {"t": "notes_go", "nonce": offer["nonce"], "i": i} in self.bo.sent)
        notes_dir = self.bo.bridge.notes_dir
        with open(os.path.join(notes_dir, "context.json"), encoding="utf-8") as f:
            context = json.load(f)
        snaps.save(os.path.join(notes_dir, "fetched.json"),
                   json.dumps({"nonce": "%s-%d" % (offer["nonce"], i), "ok": ok, "why": why}).encode())
        self.wait_for(lambda: {"t": "notes_done", "nonce": offer["nonce"], "i": i, "ok": ok} in self.bo.sent)
        return context

    def test_notes_come_into_the_piano_roll_without_opening_the_version(self):
        opened = self.start_bo()
        key = first_note_key(self.song, 1) + 3
        theirs = touch(change_note(self.song, 1, key), 2)
        snaps.save(self.project, theirs)  # Anna moves a note
        self.wait_for(lambda: self.relay.version == 2)
        offer = self.offered()
        self.assertEqual(offer["up"], 3)  # (FLSync hämta noter, Humanize, Mina noter: up three times)
        p, c = offer["items"][0][:2]
        context = self.fetch(offer, 0)
        self.assertEqual((context["pattern"], context["channel"]), (p, c))
        self.assertEqual(context["auto"], {"nonce": "%s-0" % offer["nonce"]})
        self.assertEqual(context["session"]["notes"],
                         bridge_mod.flp_merge.pattern_notes(theirs, p)[c])  # hers, as the script writes them
        self.assertEqual(context["session"]["expect"], bridge_mod.flp_merge.pattern_notes(self.song, p)[c])
        self.bo_fl.send({"t": "notes_end", "nonce": offer["nonce"], "ok": True})
        # Bo's FL saves its new notes by itself: it has version 2 now, nothing to share or open.
        self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})
        snaps.save(self.written, touch(change_note(self.bo_content, 1, key), 3))
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"], why="\n".join(self.bo.logs))
        self.settle(0.3)
        self.assertEqual(self.relay.version, 2)
        self.assertEqual(len(self.bo.opened), opened)  # never reloaded
        self.assertIsNone(self.bo.bridge.notes_live)

    def test_notes_bo_changed_himself_come_with_the_version(self):
        opened = self.start_bo()
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 3), 2))
        self.wait_for(lambda: self.relay.version == 2)
        offer = self.offered()
        self.fetch(offer, 0, ok=False, why="ändrade här")  # the script's `expect`: Bo changed them since
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)  # so the version is opened (or merged) as always
        self.assertFalse(self.bo.bridge.notes_off)  # (nothing wrong with the way itself)

    def test_more_than_notes_comes_with_the_version(self):
        opened = self.start_bo()
        snaps.save(self.project, touch(retitle(change_note(self.song, 1, first_note_key(self.song, 1) + 3),
                                               "Annas titel"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)
        self.assertFalse(any(m.get("t") == "notes_in" for m in self.bo.sent))

    def test_without_the_fetch_script_the_version_is_opened(self):
        self.start(bo_status={"dirty": 0, "playing": False})
        opened = len(self.bo.opened)
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 3), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)
        self.assertFalse(any(m.get("t") == "notes_in" for m in self.bo.sent))

    def at_work(self):
        self.bo.bridge.activity = lambda: (1.0, True)  # Bo is at work: the notes wait for a pause

    def anna_moves_a_note(self, v, by=3):
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + by), v))
        self.wait_for(lambda: self.relay.version == v)

    def test_notes_for_a_project_fl_no_longer_has_are_dropped(self):
        opened = self.start_bo()
        self.at_work()
        self.anna_moves_a_note(2)
        self.wait_for(lambda: self.bo.bridge.notes_live is not None, why="\n".join(self.bo.logs))
        other = self.old_file(os.path.join(self.tmp, "Bo", "Annat.flp"))
        self.open(self.bo, self.bo_fl, other)  # Bo opens another song
        self.wait_for(lambda: self.bo.bridge.notes_live is None)
        self.bo.bridge.activity = lambda: (None, None)
        self.settle(0.6)
        self.assertFalse(any(m.get("t") == "notes_in" for m in self.bo.sent))  # never into that one
        self.assertEqual(len(self.bo.opened), opened)  # nor is the session's opened over it

    def test_a_while_without_a_pause_doesnt_turn_it_off(self):
        opened = self.start_bo()
        self.at_work()
        self.anna_moves_a_note(2)
        self.wait_for(lambda: self.bo.bridge.notes_live is not None)
        self.bo.call(lambda: self.bo.bridge.notes_live.update(made=time.monotonic() - 1000))
        self.wait_for(lambda: self.bo.bridge.notes_live is None)
        self.assertFalse(self.bo.bridge.notes_off)
        self.bo.bridge.activity = lambda: (None, None)
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)  # (that one is opened as always)
        self.bo_fl.send({"t": "opened", "fp": self.bo_fl.fp})
        self.anna_moves_a_note(3, by=5)
        offer = self.offered()  # and the next one comes in the piano roll again
        self.bo_fl.send({"t": "notes_end", "nonce": offer["nonce"], "ok": False, "why": "FL var upptaget"})
        self.wait_for(lambda: self.bo.bridge.notes_live is None)
        self.assertFalse(self.bo.bridge.notes_off)  # (Bo pressed play: nothing wrong with the way)

    def test_a_newer_version_takes_the_place_of_notes_coming_in(self):
        self.start_bo()
        self.anna_moves_a_note(2)
        first = self.offered()
        self.bo_fl.send({"t": "notes_at", "nonce": first["nonce"], "i": 0})
        self.wait_for(lambda: {"t": "notes_go", "nonce": first["nonce"], "i": 0} in self.bo.sent)
        self.anna_moves_a_note(3, by=5)
        self.wait_for(lambda: {"t": "notes_in", "cancel": True} in self.bo.sent)  # FL's script stops that one
        self.wait_for(lambda: any(m.get("t") == "notes_in" and m.get("nonce") not in (None, first["nonce"])
                                  for m in self.bo.sent), why="\n".join(self.bo.logs))

    def test_turning_auto_sync_off_drops_the_notes(self):
        self.start_bo()
        self.at_work()
        self.anna_moves_a_note(2)
        self.wait_for(lambda: self.bo.bridge.notes_live is not None)
        self.bo.call(self.bo.bridge.set_auto_sync, False)
        self.assertIsNone(self.bo.bridge.notes_live)
        self.bo.bridge.activity = lambda: (None, None)
        self.settle(0.6)
        self.assertFalse(any(m.get("t") == "notes_in" for m in self.bo.sent))

    def test_the_piano_roll_fl_opened_is_the_one_while_it_fetches(self):
        self.start_bo()
        self.anna_moves_a_note(2)
        offer = self.offered()
        p, c = offer["items"][0][:2]
        self.bo_fl.send({"t": "notes_at", "nonce": offer["nonce"], "i": 0})
        self.wait_for(lambda: {"t": "notes_go", "nonce": offer["nonce"], "i": 0} in self.bo.sent)
        self.bo_fl.send({"t": "pr", "p": p, "c": c + 1, "n": "Annan"})  # FL says another channel is selected
        self.wait_for(lambda: self.bo.bridge.pr[:2] == (p, c + 1))
        self.bo.call(self.bo.bridge._write_context)
        with open(os.path.join(self.bo.bridge.notes_dir, "context.json"), encoding="utf-8") as f:
            context = json.load(f)
        self.assertEqual((context["pattern"], context["channel"]), (p, c))
        self.assertEqual(context["auto"], {"nonce": "%s-0" % offer["nonce"]})

    def test_fl_opening_another_project_meanwhile_opens_nothing(self):
        opened = self.start_bo()
        self.anna_moves_a_note(2)
        offer = self.offered()
        self.bo_fl.send({"t": "notes_end", "nonce": offer["nonce"], "ok": False, "why": "ett annat projekt"})
        self.wait_for(lambda: self.bo.bridge.notes_live is None)
        self.settle(0.6)
        self.assertEqual(len(self.bo.opened), opened)
        self.assertFalse(self.bo.bridge.notes_off)

    def test_an_fl_that_couldnt_run_the_script_isnt_asked_again(self):
        opened = self.start_bo()
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 3), 2))
        self.wait_for(lambda: self.relay.version == 2)
        offer = self.offered()
        self.bo_fl.send({"t": "notes_end", "nonce": offer["nonce"], "ok": False, "why": "menyn"})
        self.wait_for(lambda: len(self.bo.opened) == opened + 1)
        self.assertTrue(self.bo.bridge.notes_off)


class AutoSyncTest(MergeSession):
    """docs/phase5.md: FL saves by itself for what live sync can't carry, a save
    with nothing new isn't shared, and others' versions come in through a save."""

    def show(self, link, path):
        link.title = "%s - FL Studio 2026" % os.path.basename(path)  # FL's title names its project's file

    def said(self, link, msg):
        return msg in link.sent

    def test_fl_may_save_by_itself_once_its_title_names_the_sessions_file(self):
        self.start()
        self.settle(0.3)
        self.assertFalse(self.said(self.bo, {"t": "autosave", "ok": True}))  # no title: maybe a new project
        self.show(self.bo, self.written)
        self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": True}))

    def test_a_save_fl_makes_by_itself_is_shared_like_ctrl_s(self):
        self.start()
        self.anna_fl.send({"t": "autosave", "content": 2, "pull": False})
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 1), 2))
        self.wait_for(lambda: self.relay.version == 2)

    def test_a_save_with_only_live_values_new_isnt_shared(self):
        self.start()
        self.anna_fl.send({"t": "autosave", "content": 1, "pull": False})  # FL thought it was content
        snaps.save(self.project, touch(self.song, 2))                     # but the save has nothing new
        self.wait_for(lambda: {"t": "saved", "outcome": "unchanged"} in self.anna.sent)
        self.settle(0.3)
        self.assertEqual(self.relay.version, 1)

    def test_an_instrument_someone_added_comes_in_live(self):
        self.start()
        self.anna_fl.report(snaps.FP_B)  # Anna adds an instrument at the end of the channel rack...
        song = add_channel_like(self.song, 28)  # (FLEX, Piano Dark)
        snaps.save(self.project, touch(song, 2))  # ...and her FL saves
        self.wait_for(lambda: self.relay.version == 2)
        # Bo's FL gets it as a plugin preset, without loading anything.
        self.wait_for(lambda: any(path.endswith(".fst") for path in self.bo.opened), why="\n".join(self.bo.logs))
        preset = [path for path in self.bo.opened if path.endswith(".fst")]
        self.assertEqual(os.path.basename(preset[0]), "Piano Dark.fst")
        self.assertIn({"t": "adding", "fp": snaps.FP_B}, self.bo.sent)
        data, name = bridge_mod.flp_merge.channel_preset(song, 45)
        self.assertEqual(snaps.read(preset[0]), data)
        self.bo_fl.report(snaps.FP_B)  # FL added it: its structure is the session's again
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"])
        self.assertIn({"t": "added", "by": "Anna", "what": ["Piano Dark"]}, self.bo.sent)
        self.assertNotIn(self.written, self.bo.opened)  # the project wasn't opened again
        self.settle(0.3)
        self.assertFalse(any(m.get("t") == "pull" for m in self.bo.sent))

    def test_a_sampler_someone_added_comes_in_live_with_its_sample(self):
        self.start()
        sample = snaps.save(os.path.join(self.tmp, "Anna", "Ljud", "Min kick.wav"), b"RIFF" + bytes(60) + b"kick")
        self.anna_fl.report(snaps.FP_B)  # Anna drags her own sample into the channel rack...
        snaps.save(self.project, touch(add_sampler_like(self.song, 1, sample), 2))  # ...and her FL saves
        self.wait_for(lambda: self.relay.version == 2)
        self.wait_for(lambda: any(path.endswith(".fst") for path in self.bo.opened), why="\n".join(self.bo.logs))
        preset = [path for path in self.bo.opened if path.endswith(".fst")][0]
        here = preset_sample(preset)  # Bo's copy of the sample, in the session project's folder
        self.assertTrue(here.startswith(os.path.dirname(self.written)), here)
        self.assertEqual(snaps.read(here), snaps.read(sample))
        self.bo_fl.report(snaps.FP_B)
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"])
        self.assertIn({"t": "added", "by": "Anna", "what": ["Grv Kick 27"]}, self.bo.sent)
        self.assertNotIn(self.written, self.bo.opened)

    def live_channel_at_bo(self):
        """Anna adds an instrument (channel 45); it comes into Bo's FL live. Her version 2."""
        self.start()
        self.anna_fl.report(snaps.FP_B)
        song = add_channel_like(self.song, 28)
        snaps.save(self.project, touch(song, 2))
        self.wait_for(lambda: any(path.endswith(".fst") for path in self.bo.opened))
        self.bo_fl.report(snaps.FP_B)
        self.wait_for(lambda: self.bo.bridge.base == self.relay.snapshot["flp"])
        return song

    def test_bos_next_instrument_comes_live_to_anna(self):
        song = self.live_channel_at_bo()
        self.bo_fl.report("dddddd" + snaps.FP_B[6:])  # Bo adds an instrument too; his FL saves
        self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})
        snaps.save(self.written, touch(add_channel_like(song, 0), 3))
        self.wait_for(lambda: self.relay.version == 3, why="\n".join(self.bo.logs))
        self.wait_for(lambda: any(path.endswith(".fst") for path in self.anna.opened), why="\n".join(self.anna.logs))

    def test_a_cut_group_an_fl_numbered_otherwise_goes_out_once(self):
        # FL 26 numbers a new channel's cut groups itself, the same in the same
        # project. Should Bo's FL have given it another, that is shared once.
        song = self.live_channel_at_bo()
        self.bo_fl.send({"t": "autosave", "content": 0, "pull": False, "check": True})
        snaps.save(self.written, touch(set_channel_event(song, 45, 132, (9).to_bytes(4, "little")), 3))
        self.wait_for(lambda: self.relay.version == 3, why="\n".join(self.bo.logs))
        self.assertEqual(channel_event(self.newest(), 45, 132), (9).to_bytes(4, "little"))

    def test_a_cut_group_anna_set_on_a_channel_that_came_live_gets_into_bos_fl(self):
        song = self.live_channel_at_bo()
        self.bo_fl.send({"t": "st", "dirty": 1})
        self.show(self.bo, self.written)
        snaps.save(self.project, touch(set_channel_event(song, 45, 132, (3).to_bytes(4, "little")), 3))  # only that
        self.wait_for(lambda: self.said(self.bo, {"t": "pull", "by": "Anna", "v": 3}), why="\n".join(self.bo.logs))
        opened = len(self.bo.opened)
        self.bo_fl.send({"t": "autosave", "content": 0, "pull": True})
        snaps.save(self.written, touch(song, 4))
        self.bo_fl.send({"t": "st", "dirty": 0})
        self.assertEqual(self.saved(self.bo)["outcome"], "pulled")
        self.wait_for(lambda: len(self.bo.opened) > opened)
        self.assertEqual(channel_event(snaps.read(self.written), 45, 132), (3).to_bytes(4, "little"))

    def test_a_cut_group_the_sharer_set_on_a_channel_that_came_live_isnt_lost(self):
        song = self.live_channel_at_bo()
        # In one save Anna puts that channel in a cut group and adds another one.
        self.anna_fl.report("cccccc" + snaps.FP_B[6:])
        snaps.save(self.project, touch(add_channel_like(set_channel_event(song, 45, 132, (3).to_bytes(4, "little")),
                                                        0), 3))
        self.wait_for(lambda: self.relay.version == 3)
        self.settle(0.5)
        self.assertEqual(len([p for p in self.bo.opened if p.endswith(".fst")]), 1)  # not live: it comes with v3

    def test_two_channels_at_once_come_with_the_version(self):
        self.start()
        self.anna_fl.report(snaps.FP_B)
        snaps.save(self.project, touch(add_channel_like(add_channel_like(self.song, 28), 0), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.settle(0.5)
        self.assertFalse(any(path.endswith(".fst") for path in self.bo.opened))

    def test_a_plugin_with_samples_of_its_own_comes_with_the_version(self):
        # FPC, DirectWave: their state names the sharer's files, which only
        # loading the version finds again (by name, next to the project).
        self.start()
        self.anna_fl.report(snaps.FP_B)
        own = "C:\\Users\\Anna\\Ljud\\pad.wav".encode("utf-16-le")
        song = add_channel_like(self.song, 28)
        _, events = decode(song)
        at = anchor(events, 64, 45)
        state = next(v for e, v in events[at + 1:] if e == 213)
        song = set_channel_event(song, 45, 213, state + b"\x1a\x00" + own + b"\x00\x00")
        snaps.save(self.project, touch(song, 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.settle(0.5)
        self.assertFalse(any(path.endswith(".fst") for path in self.bo.opened))

    def test_a_sampler_whose_sample_isnt_here_comes_with_the_version(self):
        self.start()
        self.anna_fl.report(snaps.FP_B)
        snaps.save(self.project, touch(add_sampler_like(self.song, 1), 2))  # one of FL's samples Bo's FL hasn't got
        self.wait_for(lambda: self.relay.version == 2)
        self.settle(0.5)
        self.assertFalse(any(path.endswith(".fst") for path in self.bo.opened))  # FL would ask where it is

    def test_an_instrument_fl_didnt_add_comes_with_the_version(self):
        self.start()
        self.anna_fl.report(snaps.FP_B)
        with mock.patch.object(bridge_mod, "LIVE_CONFIRM", 0.5):
            snaps.save(self.project, touch(add_channel_like(self.song, 28), 2))
            self.wait_for(lambda: any(path.endswith(".fst") for path in self.bo.opened))
            self.wait_for(lambda: any("FL lade inte till" in line for line in self.bo.logs))
        self.assertNotEqual(self.bo.bridge.base, self.relay.snapshot["flp"])  # FL gets the version as usual

    def test_more_than_new_channels_comes_with_the_version(self):
        self.start()
        self.anna_fl.report(snaps.FP_B)
        song = add_channel_like(change_note(self.song, 1, first_note_key(self.song, 1) + 1), 28)
        snaps.save(self.project, touch(song, 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.settle(0.5)
        self.assertFalse(any(path.endswith(".fst") for path in self.bo.opened))  # a note too: not live

    def test_a_save_fl_wasnt_sure_about_is_shared_only_with_content(self):
        self.start()
        self.anna_fl.send({"t": "autosave", "content": 0, "pull": False, "check": True})
        snaps.save(self.project, touch(self.song, 2))  # unsaved changes were only live values
        self.wait_for(lambda: {"t": "saved", "outcome": "unchanged"} in self.anna.sent)
        self.assertEqual(self.relay.version, 1)
        self.anna_fl.send({"t": "autosave", "content": 0, "pull": False, "check": True})
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 1), 3))
        self.wait_for(lambda: self.relay.version == 2)  # a note FL couldn't tell us about

    def test_someone_elses_version_comes_in_through_a_save(self):
        self.start()
        self.bo_fl.send({"t": "st", "dirty": 1})  # Anna's values written into Bo's FL: it has unsaved changes
        self.show(self.bo, self.written)
        snaps.save(self.project, touch(rename_channel(self.song, 2, "Annas kanal"), 2))
        self.wait_for(lambda: self.relay.version == 2)
        self.wait_for(lambda: self.said(self.bo, {"t": "pull", "by": "Anna", "v": 2}), why="\n".join(self.bo.logs))
        self.assertEqual(self.bo.opened, [])  # FL has unsaved changes: opening would ask about them
        # Bo's FL saves once it is stopped and idle, with nothing new of its own.
        self.bo_fl.send({"t": "autosave", "content": 0, "pull": True})
        snaps.save(self.written, touch(self.bo_content, 3))
        self.bo_fl.send({"t": "st", "dirty": 0})
        answer = self.saved(self.bo)
        self.assertEqual((answer["outcome"], answer["open"]), ("pulled", "now"), answer)
        self.wait_for(lambda: self.bo.opened == [self.written])
        self.assertEqual(channel_names(snaps.read(self.written))[2], "Annas kanal")
        self.settle(0.3)
        self.assertEqual(self.relay.version, 2)  # nothing went back: no ping-pong of versions

    def test_an_fl_that_didnt_save_when_it_said_so_is_asked_again_later(self):
        self.start()
        self.show(self.bo, self.written)
        self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": True}))
        with mock.patch.object(bridge_mod, "AUTOSAVE_CHECK", 0.3), mock.patch.object(bridge_mod, "AUTOSAVE_RETRY", 0.5):
            self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})  # then a dialog came instead
            self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": False, "broken": True, "retry": True}))
            self.assertTrue(any("försöker igen" in line for line in self.bo.logs))
            self.bo.sent.clear()
            self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": True}))  # the dialog may be gone
            # A save by itself that happens counts again from nothing.
            self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})
            snaps.save(self.written, touch(change_note(self.bo_content, 1, 52), 3))
            self.wait_for(lambda: self.relay.version == 2)
            self.assertEqual(self.bo.bridge.autosave_failures, 0)

    def test_an_fl_that_never_saves_when_it_said_so_isnt_asked_any_more(self):
        self.start()
        self.show(self.bo, self.written)
        self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": True}))
        with mock.patch.object(bridge_mod, "AUTOSAVE_CHECK", 0.2), mock.patch.object(bridge_mod, "AUTOSAVE_RETRY", 0.2):
            for attempt in range(bridge_mod.AUTOSAVE_TRIES):
                self.wait_for(lambda: self.said(self.bo, {"t": "autosave", "ok": True}))
                self.bo.sent.clear()
                self.bo_fl.send({"t": "autosave", "content": 1, "pull": False})
                broken = {"t": "autosave", "ok": False, "broken": True, "retry": attempt < bridge_mod.AUTOSAVE_TRIES - 1}
                self.wait_for(lambda: self.said(self.bo, broken))
            self.bo.sent.clear()
            self.settle(0.6)
        self.assertFalse(self.said(self.bo, {"t": "autosave", "ok": True}))
        self.assertTrue(any("ber inte om det mer" in line for line in self.bo.logs))

    def backup(self, link, name, age=0.0):
        """A copy in FL's Backup folder, as FL makes one for each save; `age` seconds old."""
        path = snaps.save(os.path.join(link.bridge.fl_dirs["user_dir"], "FL Studio", "Projects", "Backup", name),
                          snaps.make_project(title="kopia"))
        when = time.time() - age  # (snaps.save gives files a time of its own)
        os.utime(path, (when, when))
        return path

    def test_fls_backup_copies_of_saves_by_itself_are_thinned(self):
        # FL keeps its newest 100 copies of all projects: FL saving by itself
        # every few seconds would push out the copies of everything else.
        self.start()
        anna = self.anna
        mine = self.backup(anna, "Beat (overwritten on 2026-10-01 at 12h00).flp", age=3600)  # a save of the user's own
        other = self.backup(anna, "Annat (overwritten on 2026-10-13 at 19h37).flp")         # another project's
        copies = []
        for v in (2, 3, 4):
            copies.append(self.backup(anna, "Beat (overwritten on 2026-10-13 at 19h37)%s.flp"
                                      % ("_%d" % v if v > 2 else "")))
            self.anna_fl.send({"t": "autosave", "content": 1, "pull": False})
            snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + v), v))
            self.wait_for(lambda: self.relay.version == v)
        first, second, newest = copies
        self.assertTrue(os.path.isfile(first))   # one copy of a save by itself per BACKUP_EVERY stays...
        self.assertTrue(os.path.isfile(newest))  # ...and the newest (FL's Revert to last autosave opens it)
        self.assertFalse(os.path.exists(second))
        self.assertTrue(all(os.path.isfile(path) for path in (mine, other)))
        self.assertTrue(any("Backup-mapp" in line for line in anna.logs))

    def test_fls_backup_copy_of_a_save_of_the_users_own_stays(self):
        self.start()
        kept = self.backup(self.anna, "Beat (overwritten on 2026-10-13 at 19h37).flp")
        stat = os.stat(kept)
        self.anna.bridge.backups_kept[bridge_mod.file_key(self.project)] = {  # a save by itself just now
            "every": (time.time(), "x.flp"), "newest": (kept, stat.st_mtime, stat.st_size)}
        copy = self.backup(self.anna, "Beat (overwritten on 2026-10-13 at 19h38).flp")
        snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + 1), 2))  # Ctrl+S
        self.wait_for(lambda: self.relay.version == 2)
        self.assertTrue(os.path.isfile(copy))
        self.assertTrue(os.path.isfile(kept))  # only a save by itself makes the one before it go

    def test_a_ctrl_s_right_after_a_save_by_itself_keeps_its_copy(self):
        self.start()
        anna = self.anna
        copies = []
        for v in (2, 3, 4):
            ours = self.backup(anna, "Beat (overwritten on 2026-10-13 at 19h4%d).flp" % v)
            mine = self.backup(anna, "Beat (overwritten on 2026-10-13 at 19h4%d)_2.flp" % v, age=-3)  # Ctrl+S, 3 s later
            copies.append((ours, mine))
            self.anna_fl.send({"t": "autosave", "content": 1, "pull": False})
            snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + v), v))
            self.wait_for(lambda: self.relay.version == v)
        self.assertFalse(os.path.exists(copies[1][0]))  # FLSync's own one in the middle went
        self.assertTrue(all(os.path.isfile(mine) for _, mine in copies))  # the user's own stay

    def test_a_copy_that_changed_since_it_was_seen_stays(self):
        self.start()
        anna = self.anna
        copies = []
        for v in (2, 3, 4):
            copies.append(self.backup(anna, "Beat (overwritten on 2026-10-13 at 19h5%d).flp" % v))
            if v == 4:
                snaps.save(copies[1], snaps.make_project(title="något annat"))  # replaced meanwhile
            self.anna_fl.send({"t": "autosave", "content": 1, "pull": False})
            snaps.save(self.project, touch(change_note(self.song, 1, first_note_key(self.song, 1) + v), v))
            self.wait_for(lambda: self.relay.version == v)
        self.assertTrue(os.path.isfile(copies[1]))


class KeepaliveTest(snaps.Session):
    def test_a_relay_that_goes_quiet_is_left(self):
        """A relay that answers the hello and then says nothing (a dropped Wi-Fi
        never closes the connection): FL hears the link is down within RELAY_TIMEOUT."""
        saved = bridge_mod.RELAY_TIMEOUT, bridge_mod.PING_INTERVAL
        bridge_mod.RELAY_TIMEOUT, bridge_mod.PING_INTERVAL = 0.6, 0.1
        self.addCleanup(lambda: setattr(bridge_mod, "RELAY_TIMEOUT", saved[0]))
        self.addCleanup(lambda: setattr(bridge_mod, "PING_INTERVAL", saved[1]))
        welcome = {"t": "welcome", "proto": 2, "id": "c1", "name": "Anna", "seed": False, "state": {}, "peers": [],
                   "s": 0, "fp": None, "snapshot": None, "notes": [], "pen": None}

        stop = asyncio.Event()

        async def silent(reader, writer):
            try:
                await reader.readline()
                writer.write((json.dumps(welcome) + "\n").encode())
                await stop.wait()  # and never another byte
            except (asyncio.CancelledError, OSError):
                pass  # ending normally keeps Python 3.12.1's stream callback quiet
            writer.close()

        async def serve():
            server = await asyncio.start_server(silent, "127.0.0.1", 0)
            return server, server.sockets[0].getsockname()[1]

        async def close():
            stop.set()
            server.close()
            await asyncio.sleep(0.05)

        server, port = self.loop.run(serve())
        try:
            fl, anna = self.person("Anna", join=False)
            anna.bridge.relay_port = port
            fl.hello()
            self.wait_for(lambda: anna.bridge.my_id == "c1")
            self.wait_for(lambda: fl.got("link"), timeout=5)
            self.assertIn("har inte hörts av", fl.got("link")[-1]["msg"])
            self.assertIsNone(anna.bridge.my_id)
        finally:
            self.loop.run(close())

    def test_the_relay_answers_pings(self):
        fl, anna = self.person("Anna")
        client = self.raw("M")
        client.send({"t": "ping"})
        self.assertEqual(client.recv_kind("pong"), {"t": "pong"})
        anna.call(lambda: anna.bridge.writer.write(b'{"t":"ping"}\n'))  # the bridge drops the pong: FL never hears it
        self.settle(0.3)
        self.assertEqual(fl.got("pong"), [])



class DisconnectTest(snaps.EndToEnd):
    """The real FLSync script in simulated FL Studios: what B changes while its
    connection is down reaches A when it is back, except where A changed the
    same thing meanwhile (the session's value wins there)."""

    def drop(self, fl):
        """`fl`'s connection to the relay breaks, and joining again fails until restore()."""
        link = self.links[fl]
        port = link.bridge.relay_port
        link.bridge.relay_port = free_port()  # nothing listens there
        link.call(lambda: link.bridge.writer.transport.abort())
        return port

    def restore(self, fl, port):
        self.links[fl].bridge.relay_port = port

    def test_changes_made_while_disconnected_reach_the_others(self):
        a, b = self.fl("A"), self.fl("B")
        self.ready(a, b)
        a.set_volume(3, 0.4)
        self.wait_for(lambda: b.volume(3) == 0.4)
        port = self.drop(b)
        self.wait_for(lambda: not b.client.joined)
        b.set_volume(5, 0.7)  # B, while away
        b.set_volume(7, 0.9)
        a.set_volume(7, 0.1)  # A changes 7 too: the session's
        a.set_volume(6, 0.2)
        pump(self.fls, lambda: False, 0.5)
        self.restore(b, port)
        self.wait_for(lambda: b.client.joined and a.volume(5) == 0.7, timeout=20, why="B:s ändring kom inte fram")
        self.wait_for(lambda: b.volume(6) == 0.2 and b.volume(7) == 0.1, why="B fick inte sessionens värden")
        self.assertEqual((a.volume(7), self.relay.state["m/7/v"]), (0.1, 0.1))
        hint = [h for h in b.project.hints if "ansluten igen" in h]
        self.assertTrue(hint, b.project.hints)
        self.assertIn("1 ändring", hint[-1])
        self.assertIn("1 krockade", hint[-1])


class WhereFlSavesTest(snaps.Session):
    """FL writes its recent-files list to the registry only when it closes, so
    the project FL has is found from its window title (the name) and the .flp
    files being written when FL says it saved (flpwatch), or the versions
    written here."""

    def fl_with_title(self, title, **options):
        self.writes = []
        fl, link = self.person("Anna", join=False, flp_writes=lambda: list(self.writes), mru_all=lambda: [],
                               **options)
        link.title = title
        return fl, link

    def fl_saves(self, fl, path, data=None):
        """FL saves `path`: the file is written, and its changed flag goes to 0."""
        fl.send({"t": "st", "dirty": 1, "playing": False})
        snaps.save(path, data or snaps.make_project(title="sparad"))
        self.writes.append((time.monotonic(), path))
        fl.send({"t": "st", "dirty": 0, "playing": False})

    def test_where_fl_saves_is_where_its_project_is(self):
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025")
        self.settle(0.2)
        self.assertIsNone(link.bridge.watched)  # a name, but no such file anywhere known: nothing yet
        path = os.path.join(self.tmp, "Någonstans", "Beat.flp")
        self.fl_saves(fl, path)
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(path))

    def test_a_project_opened_from_fls_projects_folder_is_found_by_its_name(self):
        # Opened in FL's browser: no write, no recent file yet (FL writes those when it closes).
        fl, link = self.fl_with_title(None)
        projects = os.path.join(link.bridge.fl_dirs["user_dir"], "FL Studio", "Projects")
        path = snaps.save(os.path.join(projects, "Låtar", "Beat", "Beat.flp"), snaps.make_project())
        snaps.save(os.path.join(projects, "Backup", "Beat.flp"), snaps.make_project())  # FL's autosaves don't count
        link.title = "Beat.flp - FL Studio 2026"
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(path))

    def test_two_projects_of_that_name_are_neither(self):
        fl, link = self.fl_with_title(None)
        projects = os.path.join(link.bridge.fl_dirs["user_dir"], "FL Studio", "Projects")
        for folder in ("Gammal", "Ny"):
            snaps.save(os.path.join(projects, folder, "Beat.flp"), snaps.make_project())
        link.title = "Beat.flp - FL Studio 2026"
        self.settle(0.3)
        self.assertIsNone(link.bridge.watched)  # FL's next save shows which one

    def test_another_programs_flp_of_that_name_doesnt_count(self):
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025")
        fl.send({"t": "st", "dirty": 1, "playing": False})
        fl.send({"t": "st", "dirty": 0, "playing": False})  # FL saved ...
        self.settle(0.2)
        other = os.path.join(self.tmp, "Annat", "Beat.flp")
        snaps.save(other, snaps.make_project())
        self.writes.append((time.monotonic() + bridge_mod.SAVED_WINDOW + 1, other))  # ... long before this was written
        self.settle(0.3)
        self.assertIsNone(link.bridge.watched)

    def test_the_flp_written_as_fl_saved_is_its_save(self):
        # FL writes its file, then says it saved: a file of that name written
        # moments later (someone else's FLSync on this PC, a synced folder) isn't FL's
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025")
        path = os.path.join(self.tmp, "Någonstans", "Beat.flp")
        self.fl_saves(fl, path)
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(path))
        now = time.monotonic()
        for n, later in enumerate((1.0, bridge_mod.SAVED_LATE + 1)):
            other = os.path.join(self.tmp, "Annat%d" % n, "Beat.flp")
            snaps.save(other, snaps.make_project())
            self.writes.append((now + later, other))
        self.settle(0.5)
        self.assertEqual(link.bridge.watched, os.path.abspath(path))

    def test_a_version_written_into_the_file_fl_has_isnt_a_save(self):
        fl, link = self.fl_with_title("Annat.flp - FL Studio 2025")
        bo_fl, bo = self.person("Bo", project=os.path.join(self.tmp, "Bo", "Beat.flp"))
        fl.hello()
        self.wait_for(lambda: link.bridge.in_session())
        snaps.save(os.path.join(self.tmp, "Bo", "Beat.flp"), snaps.make_project(title="v1"))
        written = self.received(link)["path"]
        link.title = "Beat.flp - FL Studio 2025"  # FL opens the version
        fl.send({"t": "opened", "fp": snaps.FP_A})
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(written))
        snaps.save(os.path.join(self.tmp, "Bo", "Beat.flp"), snaps.make_project(title="v2"))
        self.assertEqual(self.received(link, count=2)["path"], written)  # the next one in its place
        self.settle(1.5)
        self.assertEqual([m for m in link.sent if m.get("t") == "saved"], [])  # FL saved nothing

    def test_after_a_restart_the_project_fl_had_is_found(self):
        # The launcher restarts while FL keeps its project open: the title names
        # it, bridge-state.json says where it is. FL's recent files (as of its
        # last exit) name an older file of that name: not that one.
        state = os.path.join(self.tmp, "bridge-state.json")
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025", state_path=state)
        fl.hello()
        self.wait_for(lambda: link.bridge.in_session())
        self.raw("Bo")  # the session goes on meanwhile
        path = os.path.join(self.tmp, "Någonstans", "Beat.flp")
        self.fl_saves(fl, path)
        self.wait_for(lambda: self.relay.version == 1 and link.bridge.base == self.relay.snapshot["flp"],
                      why="\n".join(link.logs))
        old = os.path.join(self.tmp, "Gammalt", "Beat.flp")
        snaps.save(old, snaps.make_project(title="gammal"))
        link.stop()
        long_ago = time.time() - 3600
        for f in (path, old):
            os.utime(f, (long_ago, long_ago))
        fl2, again = self.person("Anna", join=False, state_path=state, flp_writes=lambda: [], mru_all=lambda: [old])
        again.title = "Beat.flp - FL Studio 2025"
        fl2.hello()
        self.wait_for(lambda: again.bridge.watched == os.path.abspath(path), why="\n".join(again.logs))
        self.settle(0.5)
        self.assertEqual(again.bridge.base, self.relay.snapshot["flp"])

    def test_a_file_fl_was_asked_to_open_counts_once_fl_loads_one(self):
        # FL may not take a file it is asked to open (busy right after a save):
        # until its script says it loaded a project, it has the one it had
        path = snaps.save(os.path.join(self.tmp, "Någonstans", "Beat.flp"), snaps.make_project())
        fl, link = self.person("Anna", join=False, flp_writes=lambda: [], mru_all=lambda: [path])
        link.title = "Beat.flp - FL Studio 2025"
        fl.send({"t": "st", "dirty": 0, "playing": False})  # FL tells when it saves (and loads)
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(path))
        other = snaps.save(os.path.join(self.tmp, "Versioner", "Beat.flp"), snaps.make_project(title="ihop"))
        link.call(lambda: setattr(link.bridge, "launched", (other, time.monotonic(), None)))
        self.settle(0.5)
        self.assertEqual(link.bridge.watched, os.path.abspath(path))  # nothing loaded yet
        fl.send({"t": "opened", "fp": snaps.FP_A})
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(other), why="\n".join(link.logs))
        fl.send({"t": "opened", "fp": snaps.FP_A})  # FL loads a project again: not the one asked for any more
        self.wait_for(lambda: link.bridge.launched is None)

    def test_where_fl_saved_doesnt_count_once_it_loaded_another_file(self):
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025")
        path = os.path.join(self.tmp, "Någonstans", "Beat.flp")
        self.fl_saves(fl, path)
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(path))
        other = snaps.save(os.path.join(self.tmp, "Versioner", "Beat.flp"), snaps.make_project(title="ihop"))
        link.call(lambda: setattr(link.bridge, "launched", (other, time.monotonic(), None)))
        fl.send({"t": "opened", "fp": snaps.FP_A})  # FL (clean after its save) opens the merged version
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(other), why="\n".join(link.logs))

    def test_an_flp_written_before_fl_ever_saved_isnt_its_save(self):
        fl, link = self.fl_with_title("Beat.flp - FL Studio 2025")
        fl.send({"t": "st", "dirty": 0, "playing": False})  # FL reports when it saves, and hasn't
        other = os.path.join(self.tmp, "Annat", "Beat.flp")
        snaps.save(other, snaps.make_project())
        self.writes.append((time.monotonic(), other))
        self.settle(0.3)
        self.assertIsNone(link.bridge.watched)

    def test_a_version_written_here_is_found_by_its_name(self):
        fl, link = self.fl_with_title("Annat.flp - FL Studio 2025")
        bo_fl, bo = self.person("Bo", project=os.path.join(self.tmp, "Bo", "Beat.flp"))
        fl.hello()
        self.wait_for(lambda: link.bridge.in_session())
        snaps.save(os.path.join(self.tmp, "Bo", "Beat.flp"), snaps.make_project(title="v1"))
        written = self.received(link)["path"]
        link.title = "Beat.flp - FL Studio 2025"  # FL opens the version (its recent files don't say)
        fl.send({"t": "opened", "fp": snaps.FP_A})
        self.wait_for(lambda: link.bridge.watched == os.path.abspath(written))
        self.wait_for(lambda: link.bridge.base == self.relay.snapshot["flp"], why="\n".join(link.logs))

    def test_a_new_unsaved_project_has_no_file(self):
        fl, link = self.fl_with_title("FL Studio 2025")
        link.mru = os.path.join(self.tmp, "Gammal.flp")
        snaps.save(link.mru, snaps.make_project())
        self.settle(0.2)
        self.assertEqual(link.bridge.watched, os.path.abspath(link.mru))  # no name in the title: as before


class OpenInFlTest(unittest.TestCase):
    """Windows opens .flp files with the newest FL installed, which may not be
    the one in the session: a project opens in the FL that runs instead."""

    def test_a_project_opens_in_the_fl_that_runs(self):
        calls = []

        def startfile(path):
            calls.append(("startfile", path))

        def popen(args, **options):
            calls.append(args)

        fl = r"C:\FL Studio 2025\FL64.exe"
        with mock.patch.object(bridge_mod, "running_fl", return_value=fl), \
                mock.patch.object(bridge_mod.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(bridge_mod.os, "startfile", side_effect=startfile, create=True):
            bridge_mod.open_file(r"C:\Projekt\Beat.flp")
            bridge_mod.open_file(r"C:\Projekt")  # a folder: Explorer
        with mock.patch.object(bridge_mod, "running_fl", return_value=None), \
                mock.patch.object(bridge_mod.os, "startfile", side_effect=startfile, create=True):
            bridge_mod.open_file(r"C:\Projekt\Beat.flp")  # no FL running: as Windows opens .flp
        self.assertEqual(calls, [[fl, r"C:\Projekt\Beat.flp"], ("startfile", r"C:\Projekt"),
                                 ("startfile", r"C:\Projekt\Beat.flp")])


if __name__ == "__main__":
    unittest.main()
