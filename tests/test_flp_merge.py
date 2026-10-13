"""bridge/flp_merge.py: three-way merges of FL Studio projects.

A small project built here, laid out like FL 25 writes them, tests the merge
rules anywhere. When FL Studio 2025 or 2026 is installed, the projects bundled
with it are read in place (never copied) and edited at the event level, the
way FL would change them: a note's key, a clip, a channel's name or sample, a
new pattern or channel, a mixer value.

    python -m unittest discover -s tests -p "test_flp_merge.py" -v
"""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import flp  # noqa: E402
import flp_merge  # noqa: E402
from flp_merge import MergeRefused, merge  # noqa: E402

FL_2025 = os.environ.get("FLSYNC_FL_DIR", r"C:\Program Files (x86)\Image-Line\FL Studio 2025")
FL_2026 = r"C:\Program Files (x86)\Image-Line\FL Studio 2026"
DATA = [os.path.join(d, "Data") for d in (FL_2025, FL_2026) if os.path.isdir(os.path.join(d, "Data"))]

THIEF = "Demo projects/Demo songs/Ookay - Thief.flp"                       # 25.2.3, ~300 KB
MIC_CHECK = "Demo projects/Demo songs/Jay Cactus x Confz - Mic Check Ready.flp"  # 25.2.3
AFRICA = "Demo projects/Demo songs/Cuebur & Thandi Draai - Africa's Calling.flp"  # 25.2.0
BIEN_DURO = "Demo projects/Demo songs/Astes - Bien Duro.flp"               # 25.1.0
SHINE = "Demo projects/Demo songs/LeriQ - SHINE.flp"                       # 25.2.5 (FL 2026)
WITH_YOU = ("Demo projects/Demo songs/Mehran Abbasi - With You.flp",      # 24.2, its name in FL 2026
            "Demo projects/Demo songs/02 Mehran Abbasi - With You.flp")   # and in FL 2025
VOCODER = "Templates/Utility/Vocoder/Vocoder.flp"                          # 24.2
SID = "Demo projects/Demo songs/Sid De La Cruz - Secret Agent Rendezvous.flp"  # 24.0, two arrangements
INNER_DAYS = "Demo projects/Demo songs/Electroconductor - Inner Days - SH-1 Rework (feat. Maja Mannila).flp"  # 24.1
ATTHAR = "Demo projects/Demo songs/Atthar Feat Mehran Abbasi & Elise Chantelle - Right Night feeling.flp"  # 20.8
OGUN = "Demo projects/Product demos/Electric Loop - The Ogun Dream.flp"    # 20.0
FPC = "System/Render tests/FPC.flp"                                        # 21.2
LIMITER = "Templates/Minimal/Basic with limiter/Basic with limiter.flp"    # 26.1 in FL 2026
HOUSEWORK = "Demo projects/Performance mode/nucleon - Housework.flp"       # 11.0
FOUR = "Templates/Performance/4x4/4x4.flp"                                 # 20.99, a second group of pattern events

KICK = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Kicks\Kick.wav"
SNARE = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Snares\Snare.wav"
KIMS = r"C:\Users\Kim\Documents\Image-Line\FL Studio\Projects\FLSync\Låt\Samples\Kick.wav"
LOCAL = r"D:\Ljud – Åsa\FLSync\Låt\Samples\Kick kopia.wav"
CLIP_ID = slice(32, 34)


# --- events -------------------------------------------------------------------------

def varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def wide(text):
    return (text + "\0").encode("utf-16-le")


def ev(eid, value=0):
    """[id, value] of one event; a str is stored as UTF-16 text, an int as the id's size."""
    if isinstance(value, int):
        size = 3 if eid == 172 else (1, 2, 4)[eid // 64]
        value = (value & (1 << 8 * size) - 1).to_bytes(size, "little")
    elif isinstance(value, str):
        value = wide(value)
    return [eid, bytes(value)]


def encode(events):
    return b"".join(bytes([eid]) + (varint(len(v)) if eid >= 192 else b"") + v for eid, v in events)


def walk(body):
    """[[id, value], ...] of every event, read without flp.py."""
    at, events = 0, []
    while at < len(body):
        eid = body[at]
        at += 1
        if eid == 172:
            size = 3
        elif eid < 192:
            size = (1, 2, 4)[eid // 64]
        else:
            size = shift = 0
            while True:
                size |= (body[at] & 0x7F) << shift
                shift += 7
                at += 1
                if body[at - 1] < 0x80:
                    break
        events.append([eid, body[at:at + size]])
        at += size
    assert at == len(body)
    return events


def u16(data, at):
    return int.from_bytes(data[at:at + 2], "little")


def u32(data, at):
    return int.from_bytes(data[at:at + 4], "little")


def put(record, at, value, size):
    record[at:at + size] = value.to_bytes(size, "little")


class Flp:
    """A project as a list of [id, value] events, to edit and write back."""

    def __init__(self, data):
        assert data[:4] == b"FLhd"
        at = 8 + u32(data, 4)
        assert data[at:at + 4] == b"FLdt"
        end = at + 8 + u32(data, at + 4)
        self.prefix, self.suffix = data[:at + 4], data[end:]
        self.events = walk(data[at + 8:end])
        version = self.events[0][1].decode("ascii").rstrip("\0").split(".")
        self.version = (int(version[0]), int(version[1]))

    def copy(self):
        return Flp(self.data())

    def data(self):
        """The bytes, FLdt's length and FLhd's channel count set as FL does."""
        body = encode(self.events)
        prefix = bytearray(self.prefix)
        prefix[10:12] = sum(1 for eid, _ in self.events if eid == 64).to_bytes(2, "little")
        return bytes(prefix) + len(body).to_bytes(4, "little") + body + self.suffix

    # where things are

    def index(self, eid, value=None, start=0, stop=None):
        for i in range(start, len(self.events) if stop is None else stop):
            if self.events[i][0] == eid and (value is None or self.events[i][1] == value):
                return i
        raise LookupError("händelse %d %r finns inte" % (eid, value))

    def has(self, eid, value=None, start=0, stop=None):
        try:
            self.index(eid, value, start, stop)
            return True
        except LookupError:
            return False

    def first_channel(self):
        return self.index(64)

    def span(self, lo, ends):
        hi = lo + 1
        while hi < len(self.events) and self.events[hi][0] not in ends:
            hi += 1
        return lo, hi

    def channel(self, n):
        return self.span(self.index(64, ev(64, n)[1]), (64, 65, 99))

    def channels(self):
        return sum(1 for eid, _ in self.events if eid == 64)

    def notes(self, n):
        """Index of pattern n's 224."""
        lo = self.index(65, ev(65, n)[1], stop=self.first_channel())
        return self.index(224, start=lo, stop=self.span(lo, (65, 64, 226, 227, 230))[1])

    def settings(self, n):
        return self.span(self.index(65, ev(65, n)[1], start=self.first_channel()), (64, 65, 99))

    def playlist(self, arrangement=0):
        return self.index(233, start=self.index(99, ev(99, arrangement)[1]))

    def insert(self, k):
        """(first, after last) of the k-th mixer track: its color, icon and name come before its 236."""
        at = self.index(236)
        while self.events[at - 1][0] in (149, 42, 95, 204):
            at -= 1
        for _ in range(k):
            at = self.index(147, start=self.index(236, start=at)) + 1
        return at, self.index(147, start=self.index(236, start=at)) + 1

    def text(self, i):
        return self.events[i][1].decode("utf-16-le").rstrip("\0")

    def set_text(self, i, text):
        self.events[i][1] = wide(text)

    def first_text(self, eid, lo, hi):
        return self.text(self.index(eid, start=lo, stop=hi))

    # records

    def clip_size(self):
        return 80 if self.version >= (24, 2) else 60 if self.version >= (20, 99) else 32

    def records(self, i, size):
        value = self.events[i][1]
        return [bytearray(value[at:at + size]) for at in range(0, len(value), size)]

    def set_records(self, i, records):
        self.events[i][1] = b"".join(bytes(r) for r in records)

    def note_records(self, n):
        return self.records(self.notes(n), 24)

    def clips(self, arrangement=0):
        return self.records(self.playlist(arrangement), self.clip_size())


def resave(f, by=None):
    """What FL changes on every save without the user doing anything: the work
    time, and the ids of the playlist's clips (from a counter that goes on)."""
    f = f.copy()
    at = f.index(237)
    f.events[at][1] = bytes(reversed(f.events[at][1]))
    if f.clip_size() > 32:
        clips = [i for i, (eid, _) in enumerate(f.events) if eid == 233]
        count = sum(len(f.events[i][1]) // f.clip_size() for i in clips)
        for i in clips:
            records = f.records(i, f.clip_size())
            for r in records:
                put(r, 32, (u16(r, 32) + (by or count)) % 0x10000, 2)
            f.set_records(i, records)
    return f


def sorted_in(records, record):
    """`records` with `record` where FL would put it: by position."""
    at = next((n for n, r in enumerate(records) if u32(r, 0) > u32(record, 0)), len(records))
    return records[:at] + [bytearray(record)] + records[at:]


def plain(clip):
    """A clip without the id FL gives it on every save."""
    return bytes(clip[:32]) + bytes(clip[36:])


def new_clip(f, like, position, pattern=None):
    """A copy of clip `like` at `position` (of `pattern`), with an id of its own as FL gives it."""
    added = bytearray(like)
    put(added, 0, position, 4)
    if pattern is not None:
        put(added, 6, 20480 + pattern, 2)
    if len(added) > 32:
        ids = [u16(c, 32) for i, (eid, _) in enumerate(f.events) if eid == 233 for c in f.records(i, len(added))]
        put(added, 32, max(ids, default=0) + 1, 2)
    return added


def retitle(f, title):
    if f.has(194):
        f.set_text(f.index(194), title)
    else:
        f.events.insert(1, ev(194, title))


def keys(data):
    """The keys of a project's chunks, as flp_merge cuts them."""
    return [c.key for c in flp_merge._Project("mine", data, None).chunks]


def note(position, key, channel=0, length=48, velocity=100):
    return (position.to_bytes(4, "little") + (0x4000).to_bytes(2, "little") + channel.to_bytes(2, "little")
            + length.to_bytes(4, "little") + key.to_bytes(2, "little") + bytes(2)
            + bytes([120, 0, 64, 0, 64, velocity, 128, 128]))


def clip(position, pattern=None, channel=None, track=1, length=384, serial=1):
    """An 80-byte playlist clip (FL 24.2 and later)."""
    item = 20480 + pattern if pattern is not None else channel
    return (position.to_bytes(4, "little") + (20480).to_bytes(2, "little") + item.to_bytes(2, "little")
            + length.to_bytes(4, "little") + (499 - track).to_bytes(2, "little") + bytes(2)
            + bytes([120, 0, 64, 0, 64, 100, 128, 128]) + b"\xff" * 8 + serial.to_bytes(2, "little") + bytes(46))


# --- a small project ------------------------------------------------------------------

NOTES = [note(0, 60), note(96, 62), note(192, 64), note(288, 65)]
CLIPS = [clip(0, pattern=1, track=1, serial=1), clip(0, channel=2, track=2, serial=2),
         clip(384, pattern=1, track=1, serial=3)]
PARAMS = [bytes(4) + bytes([k, 0, 0, 0]) + (k * 100).to_bytes(4, "little") for k in range(6)]
CHANNELS = [("Kick", KICK), ("Snare", SNARE), ("Vox", r"C:\Users\Anna\Splice\Vox 01.wav")]


def slots(plugin=None):
    """The ten slots of a mixer track, a plugin in the first if given."""
    events = []
    if plugin:
        events += [ev(201, plugin), ev(212, bytes(52)), ev(203, plugin), ev(213, b"\x01\x02\x03\x04")]
    return events + [ev(98, n) for n in range(10)]


def trailer(output=-1):
    return [ev(235, b"\x01"), ev(165, 3), ev(166, 1), ev(49, 0), ev(154, -1), ev(147, output)]


def small(title="Låt", tempo=140000, notes=NOTES, clips=CLIPS, channels=CHANNELS, names=("Trummor", "Bas"),
          params=PARAMS, version="25.2.4.5242", ppq=96, arrangements=("Arrangement",),
          tracks=("Spår 1", "Spår 2", "Spår 3"), track_size=70, clip_size=80):
    """A project laid out as FL 25 writes them: one pattern, channels, an
    arrangement with three tracks, a master and two mixer tracks. From FL 26
    on its head has what FL 26.1 adds (169 15, a second 172 and a 0); older
    layouts have shorter tracks and clips (`track_size`, `clip_size`)."""
    fl26 = int(version.split(".")[0]) >= 26
    clips = [c[:clip_size] for c in clips]
    events = [ev(199, (version + "\0").encode("ascii")), ev(159, 5242), ev(169, 15 if fl26 else 7), ev(172, 0x101),
              ev(192, "FL Studio %s.5242" % version), ev(200, "Anna"), ev(156, tempo), ev(67, 1), ev(194, title),
              ev(202, r"C:\Users\Anna\Music")] + ([ev(172, 0x100), ev(0, 0)] if fl26 else []) \
        + [ev(237, bytes(16)), ev(231, "Trummor"), ev(146, -1)]
    if notes is not None:
        events += [ev(65, 1), ev(224, b"".join(notes))]
    events += [ev(226, bytes(20))]
    for n, (name, path) in enumerate(channels):
        events += [ev(64, n), ev(21, 0), ev(201, ""), ev(212, bytes(52)), ev(203, name), ev(22, n % 3), ev(196, path)]
        if n == 0:
            events += [ev(65, 1), ev(193, "Beat"), ev(150, 0x123456), ev(164, 0)]
    for a, arrangement in enumerate(arrangements):
        events += [ev(99, a), ev(241, arrangement), ev(36, 0), ev(233, b"".join(clips) if a == 0 else clips[0])]
        for t, track in enumerate(tracks, 1):
            events += [ev(238, t.to_bytes(4, "little") + bytes(track_size - 4)), ev(43, 1), ev(239, track)]
    events += [ev(100, 0), ev(29, 1), ev(103, 4)]
    events += [ev(42, 0), ev(236, bytes(12))] + slots("Fruity Limiter") + trailer(0)
    events += [ev(149, 0x334455), ev(42, 1), ev(204, names[0]), ev(236, bytes(12))] + slots() + trailer()
    events += [ev(149, 0x556677), ev(42, 1), ev(204, names[1]), ev(236, bytes(12))] + slots() + trailer()
    events += [ev(42, 0), ev(236, bytes(12))] + slots() + trailer()  # "current"
    events += [ev(225, b"".join(params)), ev(133, 300)]
    body = encode(events)
    return (b"FLhd" + (6).to_bytes(4, "little") + bytes(2) + len(channels).to_bytes(2, "little")
            + ppq.to_bytes(2, "little") + b"FLdt" + len(body).to_bytes(4, "little") + body)


class Checks(unittest.TestCase):
    """merge() with the checks every result must pass."""

    def merged(self, base, mine, theirs, expect_keys=None, **kw):
        result = merge(base, mine, theirs, **kw)
        flp.parse(result.data)
        f = Flp(result.data)
        self.assertEqual(u16(result.data, 10), f.channels())
        cut = keys(result.data)
        self.assertEqual(len(cut), len(set(cut)))
        channels = [k[1] for k in cut if k[0] == "channel"]
        self.assertEqual(channels, list(range(len(channels))))
        self.assertIn(result.structure, ("same", "mine", "theirs"))
        if expect_keys is not None:
            self.assertEqual(cut, expect_keys)
        return result

    def assertRefused(self, code, base, mine, theirs, **kw):
        with self.assertRaises(MergeRefused) as caught:
            merge(base, mine, theirs, **kw)
        self.assertEqual(caught.exception.code, code, caught.exception.text)
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def canonical(self, data):
        """What a project holds, ephemeral events aside, for comparing results."""
        p = flp_merge._Project("mine", data, None)
        p.clip_size = flp_merge._clip_size([p, p, p])
        return {c.key: c.canon() for c in p.chunks}


# --- the merge rules, on the small project -------------------------------------------------

class SmallTest(Checks):
    def setUp(self):
        self.base = small()

    def edited(self, data=None):
        return Flp(data or self.base)

    def test_identities(self):
        b = self.base
        r = self.merged(b, b, b)
        self.assertEqual((r.data, r.mine_new, r.theirs_new, r.taken, r.conflicts, r.structure, r.renumbered),
                         (b, False, False, [], [], "same", []))
        m = small(title="Ny titel", notes=NOTES + [note(384, 67)])
        r = self.merged(b, m, b)
        self.assertEqual((r.data, r.mine_new, r.theirs_new), (m, True, False))
        r = self.merged(b, m, m)
        self.assertEqual((r.data, r.mine_new, r.theirs_new, r.conflicts), (m, False, False, []))
        r = self.merged(b, b, m)
        self.assertEqual(self.canonical(r.data), self.canonical(m))
        self.assertEqual((r.mine_new, r.theirs_new), (False, True))

    def test_refused_is_a_value_error_with_a_code(self):
        self.assertTrue(issubclass(MergeRefused, ValueError))
        e = MergeRefused("channels", "ni har båda lagt till kanaler")
        self.assertEqual((e.code, e.text, str(e)), ("channels", "ni har båda lagt till kanaler",
                                                    "channels: ni har båda lagt till kanaler"))

    def test_notes_edited_by_both(self):
        m, t = self.edited(), self.edited()
        notes = m.note_records(1)
        put(notes[0], 12, 61, 2)  # mine moves the first note up
        m.set_records(m.notes(1), notes)
        notes = t.note_records(1)
        put(notes[3], 12, 70, 2)  # theirs another
        del notes[1]              # and removes one
        notes.append(bytearray(note(480, 72)))
        t.set_records(t.notes(1), notes)
        r = self.merged(self.base, m.data(), t.data())
        got = Flp(r.data).note_records(1)
        self.assertEqual([u16(n, 12) for n in got], [61, 64, 70, 72])
        self.assertEqual([u32(n, 0) for n in got], sorted(u32(n, 0) for n in got))
        self.assertEqual((r.taken, r.conflicts, r.mine_new, r.theirs_new),
                         (["noter i mönster 1 (Beat)"], [], True, True))

    def test_the_same_note_edited_differently_keeps_mine(self):
        m, t = self.edited(), self.edited()
        for f, key in ((m, 61), (t, 63)):
            notes = f.note_records(1)
            put(notes[0], 12, key, 2)
            f.set_records(f.notes(1), notes)
        r = self.merged(self.base, m.data(), t.data())
        self.assertEqual([u16(n, 12) for n in Flp(r.data).note_records(1)], [61, 62, 64, 65])
        self.assertEqual(r.conflicts, ["noter i mönster 1 (Beat)"])
        # the same edit on both sides is no conflict
        r = self.merged(self.base, m.data(), m.data())
        self.assertEqual((r.data, r.conflicts), (m.data(), []))

    def test_a_note_removed_by_one_and_edited_by_the_other_stays_edited(self):
        m, t = self.edited(), self.edited()
        notes = m.note_records(1)
        del notes[2]
        m.set_records(m.notes(1), notes)
        notes = t.note_records(1)
        put(notes[2], 8, 96, 4)  # longer
        t.set_records(t.notes(1), notes)
        r = self.merged(self.base, m.data(), t.data())
        got = Flp(r.data).note_records(1)
        self.assertEqual([(u16(n, 12), u32(n, 8)) for n in got], [(60, 48), (62, 48), (64, 96), (65, 48)])
        self.assertEqual(r.conflicts, ["noter i mönster 1 (Beat)"])

    def test_all_notes_gone_leaves_no_notes_chunk(self):
        m, t = self.edited(), self.edited()
        m.set_records(m.notes(1), [n for n in m.note_records(1)][:2])
        t.set_records(t.notes(1), [n for n in t.note_records(1)][2:])
        r = self.merged(self.base, m.data(), t.data())
        self.assertNotIn(("notes", 1), keys(r.data))
        self.assertIn(("pattern", 1, 0), keys(r.data))  # the pattern is still there

    def test_notes_added_where_the_other_removed_them_all(self):
        t = small(notes=NOTES + [note(384, 67)])
        r = self.merged(self.base, small(notes=None), t, expect_keys=keys(t))
        self.assertEqual([u16(n, 12) for n in Flp(r.data).note_records(1)], [67])
        r = self.merged(self.base, t, small(notes=None))
        self.assertEqual([u16(n, 12) for n in Flp(r.data).note_records(1)], [67])

    def test_clips_from_both(self):
        m, t = self.edited(), self.edited()
        m.set_records(m.playlist(), sorted_in(m.clips(), clip(192, pattern=1, track=3, serial=4)))
        clips = t.clips()
        del clips[1]  # theirs removes the channel clip mine didn't touch
        t.set_records(t.playlist(), sorted_in(clips, clip(96, pattern=1, track=2, serial=4)))
        r = self.merged(resave(Flp(self.base)).data(), resave(m, 10).data(), resave(t, 20).data())
        got = Flp(r.data).clips()
        self.assertEqual([(u32(c, 0), u16(c, 6) - 20480, 499 - u16(c, 12)) for c in got],
                         [(0, 1, 1), (96, 1, 2), (192, 1, 3), (384, 1, 1)])
        self.assertEqual(len({bytes(c[CLIP_ID]) for c in got}), 4)  # every clip has an id of its own
        self.assertEqual(r.taken, ["playlisten"])

    def test_clip_ids_alone_are_no_change(self):
        # FL gives the clips new ids on every save
        m, t = resave(Flp(self.base), 3), resave(Flp(self.base), 7)
        r = self.merged(self.base, m.data(), t.data())
        self.assertEqual((r.data, r.mine_new, r.theirs_new), (m.data(), False, False))

    def test_channels_and_their_samples(self):
        m, t = self.edited(), self.edited()
        lo, hi = t.channel(1)
        t.set_text(t.index(203, start=lo, stop=hi), "Virvel")
        lo, hi = m.channel(2)
        m.set_text(m.index(196, start=lo, stop=hi), r"C:\Users\Anna\Splice\Vox 02.wav")
        r = self.merged(self.base, m.data(), t.data())
        p = flp.parse(r.data)
        self.assertEqual([c.name for c in p.channels], ["Kick", "Virvel", "Vox"])
        self.assertIn(r"C:\Users\Anna\Splice\Vox 02.wav", [s.path for s in p.samples])
        self.assertEqual((r.taken, r.conflicts, r.structure), (["kanal 2 (Virvel)"], [], "same"))
        # both rename the same channel: mine's, and a conflict
        lo, hi = m.channel(1)
        m.set_text(m.index(203, start=lo, stop=hi), "Snärt")
        r = self.merged(self.base, m.data(), t.data())
        self.assertEqual([c.name for c in flp.parse(r.data).channels], ["Kick", "Snärt", "Vox"])
        self.assertEqual(r.conflicts, ["kanal 2 (Snärt)"])

    def test_head_per_event(self):
        m = small(tempo=150000)
        t = small(title="Kims titel")
        r = self.merged(self.base, m, t)
        f = Flp(r.data)
        self.assertEqual((u32(f.events[f.index(156)][1], 0), f.text(f.index(194))), (150000, "Kims titel"))
        self.assertEqual((r.taken, r.conflicts), (["titeln"], []))
        r = self.merged(self.base, m, small(tempo=120000))
        self.assertEqual((u32(Flp(r.data).events[Flp(r.data).index(156)][1], 0), r.conflicts), (150000, ["tempot"]))

    def test_mixer_parameters_per_key(self):
        params_m, params_t = list(PARAMS), list(PARAMS)
        params_m[1] = params_m[1][:8] + (7).to_bytes(4, "little")
        params_t[4] = params_t[4][:8] + (9).to_bytes(4, "little")
        params_t[1] = params_t[1][:8] + (8).to_bytes(4, "little")  # both: mine's, not a listed conflict
        r = self.merged(self.base, small(params=params_m), small(params=params_t))
        f = Flp(r.data)
        got = f.records(f.index(225), 12)
        self.assertEqual([u32(p, 8) for p in got], [0, 7, 200, 300, 9, 500])
        self.assertEqual((r.taken, r.conflicts), (["mixerns reglage"], []))

    def test_mixer_tracks_have_their_names_before_their_236(self):
        t = small(names=("Trummor", "Bas och sub"))
        m = self.edited()
        lo, hi = m.insert(1)
        m.events[m.index(165, start=lo, stop=hi)][1] = ev(165, 4)[1]  # mine changes track 1 itself
        r = self.merged(self.base, m.data(), t)
        f = Flp(r.data)
        self.assertEqual([f.first_text(204, *f.insert(k)) for k in (1, 2)], ["Trummor", "Bas och sub"])
        self.assertEqual((r.taken, r.conflicts), (["mixerkanal 2 (Bas och sub)"], []))

    def test_ephemeral_differences_are_no_changes(self):
        m, t = self.edited(), self.edited()
        m.events[m.index(237)][1] = bytes(range(16))   # work time
        m.events[m.index(67)][1] = ev(67, 3)[1]         # current pattern
        lo, hi = m.channel(0)
        m.events[m.index(212, start=lo, stop=hi)][1] = b"\x01" * 52  # the plugin window
        t.events[t.index(237)][1] = bytes(range(16, 32))
        t.events[t.index(146)][1] = ev(146, 2)[1]       # current channel group
        t.events[t.index(200)][1] = wide("Kim")          # licensee
        t.events[t.index(202)][1] = wide(r"D:\Kims musik")  # data folder
        r = self.merged(self.base, m.data(), t.data())
        self.assertEqual((r.data, r.mine_new, r.theirs_new, r.taken, r.conflicts), (m.data(), False, False, [], []))
        r = self.merged(self.base, self.base, t.data())
        self.assertEqual((r.data, r.mine_new, r.theirs_new), (self.base, False, False))

    def test_samples_compare_by_token(self):
        # the same sample at another place on Kim's computer
        t = self.edited()
        lo, hi = t.channel(0)
        t.set_text(t.index(196, start=lo, stop=hi), KIMS)
        canon = {"base": {KICK: "k" * 64}, "mine": {KICK: "k" * 64}, "theirs": {KIMS: "k" * 64}}
        r = self.merged(self.base, self.base, t.data(), canon=canon)
        self.assertEqual((r.data, r.theirs_new, r.mine_new), (self.base, False, False))
        r = self.merged(self.base, self.base, t.data())  # without tokens it is another sample
        self.assertEqual((r.theirs_new, r.taken), (True, ["kanal 1 (Kick)"]))
        # paths differing in case and slashes are the same
        t.set_text(t.index(196, start=lo, stop=hi), KICK.upper().replace("\\", "/"))
        self.assertFalse(self.merged(self.base, self.base, t.data()).theirs_new)

    def test_localize_theirs_samples(self):
        t = self.edited()
        for n in (0, 1):
            lo, hi = t.channel(n)
            t.set_text(t.index(196, start=lo, stop=hi), KIMS)
        lo, hi = t.channel(0)
        t.set_text(t.index(203, start=lo, stop=hi), "Bastrumma")  # only channel 0 is taken
        m = self.edited()
        lo, hi = m.channel(1)
        m.set_text(m.index(203, start=lo, stop=hi), "Virvel")       # channel 1 is mine's
        r = self.merged(self.base, m.data(), t.data(), localize={KIMS: LOCAL, SNARE: "x"})
        p = flp.parse(r.data)
        self.assertEqual([c.name for c in p.channels], ["Bastrumma", "Virvel", "Vox"])
        self.assertEqual((r.taken, r.conflicts), (["kanal 1 (Bastrumma)"], ["kanal 2 (Virvel)"]))
        self.assertEqual([s.path for s in p.samples], [LOCAL, SNARE, r"C:\Users\Anna\Splice\Vox 01.wav"])
        f = Flp(r.data)
        self.assertEqual(f.events[f.index(196)][1], wide(LOCAL))  # UTF-16 like the rest, its length new
        self.assertEqual(u32(r.data, 18), len(r.data) - 22)

    def test_theirs_new_channel(self):
        t = small(channels=CHANNELS + [("Bas", r"C:\Users\Kim\Bas.wav")], notes=NOTES + [note(384, 40, channel=3)])
        m = small(title="Min titel")
        r = self.merged(self.base, m, t)
        p = flp.parse(r.data)
        self.assertEqual([c.name for c in p.channels], ["Kick", "Snare", "Vox", "Bas"])
        self.assertEqual((u16(r.data, 10), r.structure), (4, "theirs"))
        self.assertEqual([u16(n, 6) for n in Flp(r.data).note_records(1)], [0, 0, 0, 0, 3])
        self.assertEqual(r.taken, ["noter i mönster 1 (Beat)", "kanal 4 (Bas)"])
        self.assertEqual(Flp(r.data).text(Flp(r.data).index(194)), "Min titel")
        # mine's new channel: mine's structure
        r = self.merged(self.base, t, m)
        self.assertEqual((u16(r.data, 10), r.structure), (4, "mine"))
        # the same channel added by both is added once
        self.assertEqual(self.merged(self.base, t, t).data, t)

    def test_channels_added_by_both(self):
        e = self.assertRefused("channels", self.base, small(channels=CHANNELS + [("Bas", KICK)]),
                               small(channels=CHANNELS + [("Lead", KICK)]))
        self.assertEqual(e.text, "ni har båda lagt till kanaler")

    def test_channels_removed_moved_or_replaced(self):
        self.assertRefused("channels", self.base, small(channels=CHANNELS[:2]), self.base)
        self.assertRefused("channels", self.base, self.base, small(channels=[CHANNELS[1], CHANNELS[0], CHANNELS[2]]))
        t = self.edited()
        lo, hi = t.channel(1)
        t.events[t.index(201, start=lo, stop=hi)][1] = wide("Sytrus")  # another plugin
        self.assertRefused("channels", self.base, self.base, t.data())

    def test_mixer_refusals(self):
        t = self.edited()
        lo, hi = t.insert(3)
        t.events[hi:hi] = [list(e) for e in t.events[lo:hi]]  # one more mixer track
        self.assertRefused("mixer", self.base, self.base, t.data())
        self.assertRefused("mixer", self.base, small(names=("Bas", "Trummor")), self.base)  # tracks moved

    def test_pattern_refusals(self):
        m = small(notes=None)
        m = Flp(m)
        lo, hi = m.settings(1)
        del m.events[lo:hi]
        m.set_records(m.playlist(), [c for c in m.clips() if u16(c, 6) <= 20480])
        self.assertRefused("patterns", self.base, m.data(), self.base)

    def test_a_pattern_both_added(self):
        m, t = self.edited(), self.edited()
        for f, name, key, position in ((m, "Min", 50, 768), (t, "Kims", 55, 1152)):
            f.events[f.notes(1) + 1:f.notes(1) + 1] = [ev(65, 2), ev(224, note(0, key))]
            lo, hi = f.settings(1)
            f.events[hi:hi] = [ev(65, 2), ev(193, name)]
            f.set_records(f.playlist(), f.clips() + [bytearray(clip(position, pattern=2, track=3, serial=9))])
        r = self.merged(self.base, m.data(), t.data())
        self.assertEqual(r.renumbered, [(2, 3)])
        f = Flp(r.data)
        self.assertEqual([u16(f.note_records(n)[0], 12) for n in (2, 3)], [50, 55])
        self.assertEqual([f.first_text(193, *f.settings(n)) for n in (2, 3)], ["Min", "Kims"])
        self.assertEqual(sorted((u32(c, 0), u16(c, 6) - 20480) for c in f.clips() if u16(c, 6) > 20480),
                         [(0, 1), (384, 1), (768, 2), (1152, 3)])
        cut = keys(r.data)
        self.assertLess(cut.index(("notes", 3)), cut.index(("links",)))
        self.assertEqual(cut.index(("pattern", 3, 0)), cut.index(("pattern", 2, 0)) + 1)
        self.assertEqual(r.taken, ["noter i mönster 3 (Kims)", "mönster 3 (Kims)", "playlisten"])
        # the same new pattern on both sides is one
        r = self.merged(self.base, m.data(), m.data())
        self.assertEqual((r.data, r.renumbered), (m.data(), []))

    def test_format_refusals(self):
        self.assertRefused("format", self.base, small(ppq=192), self.base)
        self.assertRefused("format", small(version="12.5.1.165"), small(version="12.5.1.165"),
                           small(version="12.5.1.165"))
        self.assertRefused("format", small(version="27.1.0.6000"), small(version="27.1.0.6000"),
                           small(version="27.1.0.6000"))
        self.assertRefused("parse", self.base, self.base[:-3], self.base)
        self.assertRefused("parse", self.base, b"RIFF" + bytes(40), self.base)
        broken = self.edited()
        broken.events.insert(5, ev(64, 7))  # a channel in the head
        self.assertRefused("format", self.base, broken.data(), self.base)

    def test_fl_versions_that_lay_out_alike(self):
        # FL 2025 and FL 2026 side by side: the result has mine's version, so mine's FL opens it as its own
        r = self.merged(self.base, small(title="Min titel"), small(version="26.1.0.5528", tempo=150000))
        f = Flp(r.data)
        self.assertEqual((f.events[0][1], f.text(f.index(194)), u32(f.events[f.index(156)][1], 0)),
                         (b"25.2.4.5242\0", "Min titel", 150000))
        self.assertEqual((u32(f.events[f.index(169)][1], 0), f.has(0, stop=f.index(237))), (7, False))
        self.assertEqual((r.taken, r.conflicts, r.structure), (["tempot"], [], "same"))
        r = self.merged(self.base, small(version="26.1.0.5528", title="Min titel"), small(tempo=150000))
        f = Flp(r.data)
        self.assertEqual((f.events[0][1], f.text(f.index(194)), u32(f.events[f.index(156)][1], 0)),
                         (b"26.1.0.5528\0", "Min titel", 150000))
        at = f.index(202)
        self.assertEqual([e for e, _ in f.events[at + 1:at + 3]], [172, 0])  # FL 26.1's own events stay
        # saved by another FL and nothing else: no change
        r = self.merged(self.base, self.base, small(version="26.1.0.5528"))
        self.assertEqual((r.data, r.mine_new, r.theirs_new), (self.base, False, False))
        r = self.merged(self.base, small(version="24.2.99.4844"), self.base)
        self.assertEqual((r.data, r.mine_new, r.theirs_new), (small(version="24.2.99.4844"), False, False))

    def test_fl_versions_that_lay_out_otherwise(self):
        old = dict(version="24.1.2.4394", track_size=66, clip_size=60)  # as FL 24.1 writes tracks and clips
        e = self.assertRefused("version", self.base, small(title="Min titel"), small(clips=[], **old))  # tracks
        self.assertEqual(e.text, "projekten är sparade i olika FL-versioner som sparar på olika sätt "
                                 "(24.1 och 25.2)")
        self.assertRefused("version", self.base, small(title="Min titel"), small(**old))   # clips
        self.assertRefused("version", small(**old), small(**old), self.base)
        e = self.assertRefused("version", small(**old), self.base, small(version="26.1.0.5528"))
        self.assertEqual(e.text, "projekten är sparade i olika FL-versioner som sparar på olika sätt "
                                 "(24.1, 25.2 och 26.1)")

    def test_a_mix_of_both_structures_is_refused(self):
        t = small(channels=CHANNELS + [("Bas", KICK)])
        m = self.edited()
        lo, hi = m.insert(1)
        at = m.index(98, start=lo, stop=hi)
        m.events[at:at] = [ev(201, "Fruity Reeverb 2"), ev(212, bytes(52)), ev(213, b"\x05")]  # an effect
        e = self.assertRefused("structure", self.base, m.data(), t)
        self.assertEqual(e.text, "ni har båda ändrat kanaler eller plugins")
        r = self.merged(self.base, m.data(), self.base)
        self.assertEqual(r.structure, "mine")

    def test_arrangements(self):
        t = small(arrangements=("Arrangement", "Remix"))
        r = self.merged(self.base, small(title="Min titel"), t, expect_keys=keys(t))
        f = Flp(r.data)
        self.assertEqual((f.text(f.index(194)), f.text(f.index(241, start=f.index(99, ev(99, 1)[1])))),
                         ("Min titel", "Remix"))
        self.assertIn("arrangemang 2 (Remix)", r.taken)
        self.assertEqual(len({bytes(c[CLIP_ID]) for a in (0, 1) for c in f.clips(a)}), 4)
        self.assertRefused("arrangements", self.base, small(arrangements=("Arrangement", "Min")), t)
        self.assertRefused("arrangements", t, small(), t)  # one removed
        self.assertEqual(self.merged(self.base, t, t).data, t)

    def test_playlist_tracks_moved(self):
        self.assertRefused("arrangements", self.base, small(tracks=("Spår 2", "Spår 1", "Spår 3")), self.base)
        r = self.merged(self.base, small(tracks=("Trummor", "Spår 2", "Spår 3")),
                        small(tracks=("Spår 1", "Bas", "Spår 3")))
        f = Flp(r.data)
        self.assertEqual([f.text(i) for i, (e, _) in enumerate(f.events) if e == 239], ["Trummor", "Bas", "Spår 3"])
        self.assertEqual(r.taken, ["spår 2 (Bas)"])

    def with_pattern(self, f, n, name, key):
        f.events[f.notes(n - 1) + 1:f.notes(n - 1) + 1] = [ev(65, n), ev(224, note(0, key))]
        lo, hi = f.settings(n - 1)
        f.events[hi:hi] = [ev(65, n), ev(193, name)]
        return f

    def test_patterns_moved(self):
        b = self.with_pattern(self.edited(), 2, "Fill", 50)
        m = b.copy()
        for i, (eid, value) in enumerate(m.events):  # FL's "move pattern up": numbers swapped everywhere
            if eid == 65:
                m.events[i][1] = ev(65, 3 - u16(value, 0))[1]
        m.set_records(m.playlist(), [bytearray(clip(0, pattern=2, track=1)), bytearray(clip(0, channel=2, track=2)),
                                     bytearray(clip(384, pattern=2, track=1, serial=3))])
        self.assertRefused("patterns", b.data(), m.data(), b.data())

    def test_head_events_only_theirs_has(self):
        t = self.edited()
        t.events.insert(t.index(202) + 1, ev(195, "Kims kommentar"))
        m = small(tempo=150000)
        r = self.merged(self.base, m, t.data())
        f = Flp(r.data)
        self.assertEqual(f.index(195), f.index(202) + 1)
        self.assertEqual((r.taken, u32(f.events[f.index(156)][1], 0)), (["kommentarerna"], 150000))

    def test_controller_links_keep_their_formulas(self):
        def linked(*links):
            f = self.edited()
            at = f.index(226) + 1
            for target, formula in links:
                f.events[at:at] = [ev(227, bytes(8) + target + bytes(8)), ev(230, formula)]
                at += 2
            return f.data()
        one, two = (bytes([1, 0x80, 6, 0x73]), "1-Input"), (bytes([1, 0x1F, 0xC5, 0x72]), "Input*2")
        r = self.merged(self.base, small(title="x"), linked(one, two))
        f = Flp(r.data)
        at = f.index(227)
        self.assertEqual([e for e, _ in f.events[at - 1:at + 4]], [226, 227, 230, 227, 230])
        self.assertEqual([f.text(at + 1), f.text(at + 3)], ["1-Input", "Input*2"])
        self.assertEqual(r.taken, ["kontrollkopplingarna"])
        r = self.merged(self.base, linked(one), linked(two))
        self.assertEqual((Flp(r.data).text(Flp(r.data).index(230)), r.conflicts), ("1-Input", ["kontrollkopplingarna"]))

    def test_channel_pairs_in_notes_stay_together(self):
        def pairs(*values):
            f = self.edited()
            at = f.notes(1) + 1
            for n, value in enumerate(values):
                f.events[at + 2 * n:at + 2 * n] = [ev(160, n), ev(161, value)]
            return f
        b, t = pairs(0, 0, 0), pairs(0, -3, 0)
        m = b.copy()
        notes = m.note_records(1)
        put(notes[1], 12, 63, 2)
        m.set_records(m.notes(1), notes)
        r = self.merged(b.data(), m.data(), t.data())
        f = Flp(r.data)
        at = f.notes(1)
        self.assertEqual([(e, u32(v, 0)) for e, v in f.events[at + 1:at + 7]],
                         [(160, 0), (161, 0), (160, 1), (161, 0xFFFFFFFD), (160, 2), (161, 0)])
        self.assertEqual(u16(f.note_records(1)[1], 12), 63)

    def test_damage_never_raises_anything_else(self):
        import random
        rng = random.Random(7)
        for _ in range(300):
            data = bytearray(self.base)
            for _ in range(rng.randint(1, 3)):
                at = rng.randrange(22, len(data))
                data[at] = rng.randrange(256)
            try:
                merge(self.base, bytes(data), self.base)
            except MergeRefused as e:
                self.assertIsNone(e.__cause__, e.text)


class ContentChangesTest(unittest.TestCase):
    """content_changes(): what a save changed that live sync doesn't carry."""

    def changes(self, edit, start=None):
        base = Flp(start or small())
        mine = resave(base)
        edit(mine)
        return [key[0] for key in flp_merge.content_changes(base.data(), mine.data())]

    def test_a_save_without_changes_has_no_content(self):
        self.assertEqual(self.changes(lambda f: None), [])  # FL's work time and clip ids aren't content

    def test_live_values_are_no_content(self):
        def live(f):
            f.set_text(f.index(203), "Kick 2")                     # a channel's name
            f.events[f.index(156)][1] = (128000).to_bytes(4, "little")  # the tempo
            f.set_text(f.index(204), "Trummor 2")                  # a mixer track's name
            f.set_text(f.index(193), "Vers")                       # a pattern's name
            f.set_text(f.index(239), "Melodi")                     # a playlist track's name
            params = f.records(f.index(225), 12)
            put(params[2], 8, 999, 4)                              # a mixer level
            f.set_records(f.index(225), params)
        self.assertEqual(self.changes(live), [])

    def test_clips_notes_samples_and_plugins_are_content(self):
        def clip(f):
            clips = f.clips()
            f.set_records(f.playlist(), sorted_in(clips, new_clip(f, clips[0], 768)))
        self.assertEqual(self.changes(clip), ["playlist"])

        def notes(f):
            f.set_records(f.notes(1), f.note_records(1) + [bytearray(note(384, 67))])
        self.assertEqual(self.changes(notes), ["notes"])

        def sample(f):
            f.set_text(f.index(196), r"C:\Ljud\Ny kick.wav")
        self.assertEqual(self.changes(sample), ["channel"])

        def plugin_state(f):
            f.events[f.index(213)][1] = b"\x09\x09\x09\x09"  # the mixer's Fruity Limiter, set otherwise
        self.assertEqual(self.changes(plugin_state), ["insert"])

    def test_a_new_channel_is_content(self):
        bigger = small(channels=CHANNELS + [("Perc", SNARE)])
        base, mine = small(), Flp(bigger)
        changed = flp_merge.content_changes(base, resave(mine).data())
        self.assertIn(("channel", 3), changed)


# --- FL's own projects -----------------------------------------------------------------

def bundled(rel, only=None):
    """A project bundled with FL 2025 or 2026 (`only`: that one's Data folder);
    `rel` may be a tuple of the names it has in either."""
    for name in (rel,) if isinstance(rel, str) else rel:
        for folder in DATA if only is None else [only]:
            path = os.path.join(folder, *name.split("/"))
            if os.path.isfile(path):
                return path
    return None


def read(rel, only=None):
    path = bundled(rel, only)
    if path is None:
        raise unittest.SkipTest("%s finns inte här" % rel)
    with open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(DATA, "FL Studio 2025 eller 2026 finns inte på den här datorn")
class BundledTest(Checks):
    IDENTITY = [THIEF, MIC_CHECK, AFRICA, SHINE, WITH_YOU, VOCODER, SID, INNER_DAYS, ATTHAR, OGUN, FPC, FOUR]

    def each(self, rels):
        found = 0
        for rel in rels:
            path = bundled(rel)
            if path is not None:
                found += 1
                with open(path, "rb") as f:
                    yield rel, f.read()
        if path := bundled(LIMITER, os.path.join(FL_2026, "Data")):
            with open(path, "rb") as f:
                yield "FL 2026 " + LIMITER, f.read()
        self.assertGreater(found, 0)

    def test_identities(self):
        for rel, b in self.each(self.IDENTITY):
            with self.subTest(rel):
                r = self.merged(b, b, b, expect_keys=keys(b))
                self.assertEqual((r.data, r.mine_new, r.theirs_new, r.taken, r.conflicts, r.structure),
                                 (b, False, False, [], [], "same"))
                m = self.edit(b)
                r = self.merged(b, m, b)
                self.assertEqual((r.data, r.mine_new, r.theirs_new), (m, True, False))
                r = self.merged(b, m, m)
                self.assertEqual((r.data, r.mine_new, r.theirs_new), (m, False, False))
                r = self.merged(b, b, m, expect_keys=keys(m))
                self.assertEqual(self.canonical(r.data), self.canonical(m))
                self.assertEqual((r.mine_new, r.theirs_new), (False, True))
                again = resave(Flp(b), 5).data()
                r = self.merged(b, again, b)
                self.assertEqual((r.data, r.mine_new, r.theirs_new), (again, False, False))

    def edit(self, data):
        """Something FL users do: a new title, a channel renamed, a note moved."""
        f = Flp(data)
        if f.has(194):
            f.set_text(f.index(194), "Ändrad titel")
        else:
            f.events.insert(1, ev(194, "Ändrad titel"))
        if f.has(64):
            lo, hi = f.channel(0)
            if f.has(203, start=lo, stop=hi):
                f.set_text(f.index(203, start=lo, stop=hi), "Omdöpt")
        if f.has(224, stop=f.first_channel()):
            at = f.index(224)
            notes = f.records(at, 24)
            if notes:
                put(notes[0], 0, u32(notes[0], 0) + 12, 4)
                f.set_records(at, notes)
        return f.data()

    def test_notes_on_both_sides_of_a_pattern(self):
        b = read(THIEF)
        m, t = Flp(b), Flp(b)
        notes = m.note_records(7)
        self.assertGreater(len(notes), 10)
        put(notes[0], 12, u16(notes[0], 12) + 1, 2)
        m.set_records(m.notes(7), notes)
        notes = t.note_records(7)
        put(notes[5], 12, u16(notes[5], 12) + 2, 2)
        t.set_records(t.notes(7), notes)
        r = self.merged(b, resave(m).data(), resave(t).data())
        got = Flp(r.data).note_records(7)
        base = Flp(b).note_records(7)
        self.assertEqual(len(got), len(base))
        self.assertIn(bytes(m.note_records(7)[0]), [bytes(n) for n in got])
        self.assertIn(bytes(t.note_records(7)[5]), [bytes(n) for n in got])
        self.assertEqual(r.taken, ["noter i mönster 7"])
        self.assertEqual(r.conflicts, [])
        # the same note, edited differently: mine's
        notes = t.note_records(7)
        put(notes[0], 12, u16(notes[0], 12) + 3, 2)
        t.set_records(t.notes(7), notes)
        r = self.merged(b, m.data(), t.data())
        got = [bytes(n) for n in Flp(r.data).note_records(7)]
        self.assertIn(bytes(m.note_records(7)[0]), got)
        self.assertNotIn(bytes(t.note_records(7)[0]), got)
        self.assertEqual(r.conflicts, ["noter i mönster 7"])

    def test_clips_on_both_sides(self):
        for rel in (THIEF, SID, INNER_DAYS, ATTHAR, OGUN):
            with self.subTest(rel):
                b = read(rel)
                m, t = Flp(b), Flp(b)
                last = max(u32(c, 0) for c in m.clips())
                arrangement = 1 if rel == SID else 0
                clips = m.clips(arrangement)
                added_m = new_clip(m, clips[0], last + 96 * 16)
                m.set_records(m.playlist(arrangement), clips + [added_m])
                clips = t.clips()
                added_t = new_clip(t, clips[1], last + 96 * 32)
                gone = clips.pop(3)
                t.set_records(t.playlist(), clips + [added_t])
                r = self.merged(b, resave(m).data(), resave(t, 9).data())
                f = Flp(r.data)
                got = [plain(c) for c in f.clips()]
                before = [plain(c) for c in Flp(b).clips()]
                self.assertIn(plain(added_t), got)
                self.assertEqual(got.count(plain(gone)), before.count(plain(gone)) - 1)
                self.assertEqual(len(got), len(before) + (arrangement == 0))
                self.assertEqual([u32(c, 0) for c in got], sorted(u32(c, 0) for c in got))
                self.assertIn(plain(added_m), [plain(c) for c in f.clips(arrangement)])
                name = f.first_text(241, *f.span(f.index(99), (233,)))
                self.assertEqual(r.taken, ["playlisten i arrangemang 1 (%s)" % name if rel == SID else "playlisten"])
                if f.clip_size() > 32:
                    ids = [bytes(c[CLIP_ID]) for a in range(1 + (rel == SID)) for c in f.clips(a)]
                    self.assertEqual(len(ids), len(set(ids)))

    def test_a_clip_theirs_removed(self):
        b = read(THIEF)
        t = Flp(b)
        clips = t.clips()
        gone = bytes(clips.pop(100))
        t.set_records(t.playlist(), clips)
        m = Flp(b)
        m.set_text(m.index(194), "Min titel")
        r = self.merged(b, resave(m).data(), resave(t).data())
        got = [plain(c) for c in Flp(r.data).clips()]
        self.assertEqual(len(got), len(clips))
        self.assertEqual(got.count(plain(gone)), [plain(c) for c in Flp(b).clips()].count(plain(gone)) - 1)
        self.assertEqual((r.taken, r.conflicts), (["playlisten"], []))

    def test_channels_renamed_and_resampled(self):
        b = read(THIEF)
        m, t = Flp(b), Flp(b)
        lo, hi = t.channel(3)
        t.set_text(t.index(203, start=lo, stop=hi), "Tom (Kim)")
        lo, hi = m.channel(5)
        m.set_text(m.index(196, start=lo, stop=hi), r"C:\Users\Anna\Splice\Tom.wav")
        r = self.merged(b, resave(m).data(), resave(t).data())
        p = flp.parse(r.data)
        self.assertEqual(p.channels[3].name, "Tom (Kim)")
        self.assertIn(r"C:\Users\Anna\Splice\Tom.wav", [s.path for s in p.samples])
        self.assertEqual((r.taken, r.conflicts, r.structure), (["kanal 4 (Tom (Kim))"], [], "same"))
        lo, hi = m.channel(3)
        m.set_text(m.index(203, start=lo, stop=hi), "Tom (Anna)")
        r = self.merged(b, m.data(), t.data())
        self.assertEqual(flp.parse(r.data).channels[3].name, "Tom (Anna)")
        self.assertEqual(r.conflicts, ["kanal 4 (Tom (Anna))"])

    def append_channel(self, f, name):
        """A copy of the last channel at the end, as FL's Clone does."""
        n = f.channels()
        lo, hi = f.channel(n - 1)
        copy = [list(e) for e in f.events[lo:hi]]
        copy[0][1] = ev(64, n)[1]
        named = [e for e in copy if e[0] == 203]
        if named:
            named[0][1] = wide(name)
        else:
            at = next(i for i, e in enumerate(copy) if e[0] == 201) + 1
            copy.insert(at, ev(203, name))
        f.events[hi:hi] = copy
        return n

    def test_theirs_new_channel_with_notes(self):
        for rel in (THIEF, SHINE, LIMITER):
            with self.subTest(rel):
                b = read(rel, os.path.join(FL_2026, "Data") if rel == LIMITER else None)
                m, t = Flp(b), Flp(b)
                n = self.append_channel(t, "Ny kanal")
                if t.has(224, stop=t.first_channel()):
                    pattern = next(int.from_bytes(t.events[i][1], "little") for i in range(t.first_channel())
                                   if t.events[i][0] == 65)
                    notes = t.note_records(pattern)
                    added = bytearray(notes[0])
                    put(added, 6, n, 2)
                    t.set_records(t.notes(pattern), notes + [added])
                retitle(m, "Min titel")
                r = self.merged(b, resave(m).data(), resave(t).data(), expect_keys=keys(t.data()))
                p = flp.parse(r.data)
                self.assertEqual((len(p.channels), u16(r.data, 10), r.structure), (n + 1, n + 1, "theirs"))
                self.assertEqual(p.channels[n].name, "Ny kanal")
                self.assertEqual(p.title, "Min titel")
                self.assertIn("kanal %d (Ny kanal)" % (n + 1), r.taken)
                self.assertTrue(r.mine_new and r.theirs_new)
                # both add one: refused
                self.append_channel(m, "Min kanal")
                self.assertRefused("channels", b, m.data(), t.data())

    def test_a_channel_removed_or_moved(self):
        b = read(THIEF)
        f = Flp(b)
        lo, hi = f.channel(44)
        del f.events[lo:hi]
        self.assertRefused("channels", b, b, f.data())
        f = Flp(b)
        (lo3, hi3), (lo4, hi4) = f.channel(3), f.channel(4)
        three, four = [list(e) for e in f.events[lo3:hi3]], [list(e) for e in f.events[lo4:hi4]]
        three[0][1], four[0][1] = ev(64, 4)[1], ev(64, 3)[1]
        f.events[lo3:hi4] = four + three
        self.assertRefused("channels", b, f.data(), b)

    def add_pattern(self, f, n, name, key, position):
        """A new pattern n: a note, a name and a clip."""
        last = max(i for i in range(f.first_channel()) if f.events[i][0] == 224)
        added = f.records(last, 24)[0]
        put(added, 12, key, 2)
        end = f.span(last, (65, 64, 226, 227, 230))[1]  # after the last pattern's notes
        f.events[end:end] = [ev(65, n), ev(224, bytes(added))]
        settings = [i for i in range(f.first_channel(), len(f.events)) if f.events[i][0] == 65]
        at = f.span(settings[-1], (64, 65, 99))[1] if settings else f.channel(0)[1]
        f.events[at:at] = [ev(65, n), ev(193, name), ev(150, 0x445566)]
        clips = f.clips()
        f.set_records(f.playlist(), clips + [new_clip(f, next(c for c in clips if u16(c, 6) > 20480), position, n)])

    def test_a_pattern_both_added(self):
        for rel in (THIEF, BIEN_DURO, WITH_YOU):
            with self.subTest(rel):
                b = read(rel)
                f = Flp(b)
                top = max([int.from_bytes(v, "little") for e, v in f.events if e == 65]
                          + [u16(c, 6) - 20480 for c in f.clips() if u16(c, 6) > 20480])
                last = max(u32(c, 0) for c in f.clips()) + 96 * 64
                m, t = Flp(b), Flp(b)
                self.add_pattern(m, top + 1, "Annas", 50, last)
                self.add_pattern(t, top + 1, "Kims", 55, last + 384)
                r = self.merged(b, resave(m).data(), resave(t).data())
                self.assertEqual(r.renumbered, [(top + 1, top + 2)])
                f = Flp(r.data)
                self.assertEqual([u16(f.note_records(n)[0], 12) for n in (top + 1, top + 2)], [50, 55])
                self.assertEqual([f.first_text(193, *f.settings(n)) for n in (top + 1, top + 2)], ["Annas", "Kims"])
                found = {(u32(c, 0), u16(c, 6) - 20480) for c in f.clips() if u32(c, 0) >= last}
                self.assertEqual(found, {(last, top + 1), (last + 384, top + 2)})
                self.assertIn("mönster %d (Kims)" % (top + 2), r.taken)

    def test_a_pattern_removed(self):
        b = read(THIEF)
        f = Flp(b)
        at = f.notes(4)
        del f.events[at - 1:at + 1]
        lo, hi = f.settings(4)
        del f.events[lo:hi]
        f.set_records(f.playlist(), [c for c in f.clips() if u16(c, 6) != 20480 + 4])
        e = self.assertRefused("patterns", b, f.data(), b)
        self.assertEqual(e.text, "mönster 4 har tagits bort eller flyttats i din version")

    def test_mixer(self):
        b = read(THIEF)
        m, t = Flp(b), Flp(b)
        params = t.records(t.index(225), 12)
        put(params[10], 8, u32(params[10], 8) ^ 0x1234, 4)
        t.set_records(t.index(225), params)
        lo, hi = t.insert(2)
        t.set_text(t.index(204, start=lo, stop=hi), "TOMS (Kim)")
        params = m.records(m.index(225), 12)
        put(params[20], 8, u32(params[20], 8) ^ 0x4321, 4)
        m.set_records(m.index(225), params)
        r = self.merged(b, m.data(), t.data())
        f = Flp(r.data)
        got = f.records(f.index(225), 12)
        self.assertEqual((got[10], got[20]), (t.records(t.index(225), 12)[10], m.records(m.index(225), 12)[20]))
        self.assertEqual(f.first_text(204, *f.insert(2)), "TOMS (Kim)")
        self.assertEqual(r.taken, ["mixerkanal 2 (TOMS (Kim))", "mixerns reglage"])
        # a track moved: refused
        f = Flp(b)
        (lo2, hi2), (lo3, hi3) = f.insert(2), f.insert(3)
        f.events[lo2:hi3] = f.events[lo3:hi3] + f.events[lo2:hi2]
        self.assertRefused("mixer", b, b, f.data())

    def test_vocoder_track_names(self):
        b = read(VOCODER)
        t = Flp(b)
        lo, hi = t.insert(1)
        self.assertEqual(t.first_text(204, lo, hi), "Voice in")
        t.set_text(t.index(204, start=lo, stop=hi), "Röst in")
        m = Flp(b)
        lo, hi = m.insert(0)  # the master
        m.events[m.index(165, start=lo, stop=hi)][1] = ev(165, 4)[1]
        r = self.merged(b, m.data(), t.data())
        f = Flp(r.data)
        self.assertEqual(f.first_text(204, *f.insert(1)), "Röst in")
        self.assertEqual(f.events[f.index(165, start=f.insert(0)[0])][1], ev(165, 4)[1])
        self.assertEqual((r.taken, r.conflicts), (["mixerkanal 1 (Röst in)"], []))

    def test_samples_by_token_and_made_local(self):
        b = read(THIEF)
        f = Flp(b)
        lo, hi = f.channel(1)
        path = f.first_text(196, lo, hi)
        t = Flp(b)
        t.set_text(t.index(196, start=lo, stop=hi), KIMS)
        canon = {"base": {path: "a" * 64}, "mine": {path: "a" * 64}, "theirs": {KIMS: "a" * 64}}
        r = self.merged(b, b, t.data(), canon=canon)
        self.assertEqual((r.data, r.theirs_new), (b, False))
        t.set_text(t.index(203, start=lo, stop=hi), "Kick (Kim)")
        r = self.merged(b, b, t.data(), canon=canon, localize={KIMS: LOCAL})
        p = flp.parse(r.data)
        self.assertEqual(p.channels[1].name, "Kick (Kim)")
        self.assertIn(LOCAL, [s.path for s in p.samples])
        self.assertNotIn(KIMS, [s.path for s in p.samples])
        self.assertEqual(r.taken, ["kanal 2 (Kick (Kim))"])

    def test_a_resave_of_a_real_project_has_no_content(self):
        for rel, data in self.each(self.IDENTITY):
            with self.subTest(rel):
                self.assertEqual(flp_merge.content_changes(data, resave(Flp(data)).data()), [])

    def test_a_project_from_before_fl_20(self):
        b = read(HOUSEWORK)
        e = self.assertRefused("format", b, b, b)
        self.assertEqual(e.text, "projektet är sparat i en för gammal FL-version (11.0)")

    def test_fl_2025_and_fl_2026(self):
        # the same template as FL 2025 (25.1.99) and FL 2026 (26.1.0) save it
        old, new = read(LIMITER, os.path.join(FL_2025, "Data")), read(LIMITER, os.path.join(FL_2026, "Data"))
        if Flp(old).version == Flp(new).version:
            self.skipTest("mallen är sparad av samma FL-version i båda")
        mine = Flp(old)
        lo, hi = mine.channel(1)
        mine.set_text(mine.index(203, start=lo, stop=hi), "Annas klapp")
        r = self.merged(old, mine.data(), new)
        p, f = flp.parse(r.data), Flp(r.data)
        self.assertEqual((p.version, p.channels[1].name, p.channels[4].name),
                         (flp.parse(old).version, "Annas klapp", flp.parse(new).channels[4].name))
        self.assertEqual(u32(f.events[f.index(169)][1], 0), u32(Flp(old).events[Flp(old).index(169)][1], 0))
        self.assertFalse(f.has(172))  # FL 25.1 writes none
        self.assertIn("kanal 5 (%s)" % p.channels[4].name, r.taken)
        self.assertEqual((r.mine_new, r.theirs_new, r.structure), (True, True, "same"))
        # the other way round: FL 2026's version and head stay
        mine, theirs = Flp(new), Flp(old)
        lo, hi = mine.channel(1)
        mine.set_text(mine.index(203, start=lo, stop=hi), "Annas klapp")
        lo, hi = theirs.channel(2)
        theirs.set_text(theirs.index(203, start=lo, stop=hi), "Kims hihat")
        r = self.merged(old, mine.data(), theirs.data())
        p, f = flp.parse(r.data), Flp(r.data)
        self.assertEqual((p.version, p.channels[1].name, p.channels[2].name),
                         (flp.parse(new).version, "Annas klapp", "Kims hihat"))
        self.assertEqual([e for e, _ in f.events[:40] if e in (172, 192, 0)], [172, 192, 172, 0])
        self.assertEqual(r.taken, ["kanal 3 (Kims hihat)"])
        self.assertEqual(self.merged(old, new, old).data, new)
        r = self.merged(old, old, new)
        self.assertEqual((flp.parse(r.data).version, r.mine_new, r.theirs_new), (flp.parse(old).version, False, True))

    def test_speed(self):
        b = read(THIEF)
        m, t = Flp(b), Flp(b)
        notes = m.note_records(7)
        put(notes[0], 12, u16(notes[0], 12) + 1, 2)
        m.set_records(m.notes(7), notes)
        lo, hi = t.channel(3)
        t.set_text(t.index(203, start=lo, stop=hi), "Tom (Kim)")
        clips = t.clips()
        put(clips[0], 0, u32(clips[0], 0) + 96, 4)
        t.set_records(t.playlist(), clips)
        m, t = resave(m).data(), resave(t).data()
        started = time.perf_counter()
        merge(b, m, t)
        self.assertLess(time.perf_counter() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
