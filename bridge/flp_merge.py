"""Three-way merge of FL Studio projects (.flp), for saving in a session.

When FL saves while someone else's newer version is in the session, the
bridge merges the two (docs/phase4.md, "The merge"): `base` is the version
both started from, `mine` the file FL just wrote, `theirs` the session's
newest. Pure (bytes in, bytes out, no I/O), standard library only.

An .flp is a header and a flat list of events (flp.py). merge() cuts the
events into chunks at anchor events, each keyed by what its anchor says. The
grammar, measured on the 162 projects saved by FL 20 to 26 that come with FL
2025 and 2026, is checked on every file merged; anything else is refused:

    head            the events before all of the below: version, tempo,
                    title, channel groups, ...
    notes n         65 n before the first channel: pattern n's notes (224),
                    controller events (223) and 160/161 pairs
    links           from the first 226/227/230 before the first channel (after
                    the notes, or at the end of the head without them):
                    controller links; a 230 is the formula of the 227 before it
    channel n       64 n and its events; n = 0, 1, ... in order
    pattern n i     65 n after the first channel: pattern n's name, color,
                    length, time markers. FL writes these after channel 0; its
                    performance templates have a second group (i = 1) with
                    other events of some patterns after a later channel
    arrangement n   99 n and its name, up to
    playlist n      233: its clips
    markers n       the events after 233 up to the first track: time markers
    track n t       238 (track t, from 1, in its first 4 bytes), 43, 239
    arrtail         the events after the last track: current arrangement,
                    mixer track count (103), ...
    insert k        the k-th mixer track (0 the master): the events after the
                    previous track's last one (a track's color, icon and name,
                    149 42 95 204, come before its 236), 236, its slots (a
                    plugin's events, then 98 with the slot's index), 235 165
                    166 49 154 and 147, the track's last event
    mixer           225 (mixer parameters) and the events after it

docs/phase4.md had a mixer track start at its 236 with its name after its
slots. FL's projects say otherwise: with the 149 42 95 204 after a track's
147 taken as the next track's, 416 of their channels are routed (event 22)
to a mixer track of their own name, against 2 the other way; the Vocoder
template's "Voco out" holds the Vocodex, its "Voice in" nothing. The last
mixer track is FL's "current" one (its plugins' 212 call it 501).

Each key is merged three-way on the chunks' canonical form (sample paths by
token, EPHEMERAL events left out): unchanged in theirs, mine's; unchanged in
mine, theirs'; changed the same, mine's; changed differently, mine's and a
conflict. Added by one side: included; removed by one side and unchanged in
the other: removed; removed by one and changed by the other: the changed one
and a conflict. Finer merges:

    head, links, arrtail, notes n, mixer
        per event id, an id's events together (a 230 with the 227 before it,
        a 161 with its 160); MINE_ALWAYS ids always mine's.
    224 notes and 233 clips
        a multiset of records: each one's count is base's plus both sides'
        changes, a change both made counted once. A record both sides
        replaced (one note edited by both) keeps mine's replacement. Sorted
        by position again if all three were.
    225 mixer parameters
        12-byte records keyed by bytes 4-8, three-way per key.

A pattern both sides added with different content keeps its number in mine;
theirs' gets the next free one, in its chunks and clips.

Versions saved by different FLs merge when they lay their records out alike;
the result has mine's version (MINE_ALWAYS), so the saver's FL opens it as its
own. Every event of a RECORDS id that comes from theirs must have a size mine
has for that id in the same kind of chunk, and clips one record size, mine's.
Measured on FL's projects: FL 24.2, 25.1, 25.2 and 26.1 write every record
alike; 24.2 lengthened tracks (238: 66 to 70 bytes) and clips (60 to 80), FL
21 clips (32 to 60) and 215. An event only one version writes is let through,
as FL lets it through when it opens a newer project.

MergeRefused when a safe merge can't be told: a file that doesn't parse or
follow the grammar, another PPQ, FL versions with other record layouts, FL
before 20 or after LATEST; channels deleted, moved, replaced or added by both;
mixer tracks added, removed or moved; patterns or arrangements gone or moved;
playlist tracks moved; an arrangement both added; a result whose channels and
plugins are neither side's.
"""

import collections
import dataclasses
import itertools
import ntpath

import flp

# --- event ids ---------------------------------------------------------------------

TEXT = 192             # ids from here on have a length and bytes
TYPE = 21              # channel type
CHANNEL = 64
PATTERN = 65
SLOT = 98              # a mixer slot's index, its last event
ARRANGEMENT = 99
INSERT_END = 147       # a mixer track's output, its last event
OLD_NAME = 192         # channel name before FL 12; the version text in the head
PATTERN_NAME = 193
SAMPLE = 196
VERSION = 199
PLUGIN = 201
NAME = 203
INSERT_NAME = 204
STATE = 213
NOTES = 224
PARAMS = 225
PLAYLIST = 233
INSERT = 236
TRACK = 238
TRACK_NAME = 239
ARRANGEMENT_NAME = 241

LINKS = frozenset((226, 227, 230))     # controller links, before the first channel
TRACK_EVENTS = frozenset((43, 239))    # a playlist track's events after its 238
INSERT_HEAD = frozenset((149, 42, 95, 204))  # a mixer track's events before its 236
ANCHORS = frozenset((CHANNEL, PATTERN, ARRANGEMENT, PLAYLIST, TRACK))  # never in the mixer
ALL_FIXED = frozenset(range(TEXT))     # flp._events yields every event with these

# Per computer, per view or per save, never a change: work and creation time
# (237), current pattern (67), current arrangement (100), current channel group
# (146), plugin windows (212: position, size, page, open), FL's version, build
# and what its version writes (199, 159, 169: 1 in 24.1, 7 in 24.2 to 25.2.5, 15
# in 25.2.99 and 26.1; 172 and the event after it: "FL Studio 25.2.3..." in a
# 192, and in 26.1 a 0), its registration (200 licensee, 28 licensed), the
# project data folder (202, per computer) and the channel rack's height (133).
EPHEMERAL = frozenset((237, 67, 100, 146, 212, 199, 159, 169, 172, 200, 28, 202, 133))
HEAD_EPHEMERAL = EPHEMERAL | {OLD_NAME}
# Those of them outside channels and plugin slots: always mine's, since they
# describe the FL that saved and will open the result.
MINE_ALWAYS = HEAD_EPHEMERAL - {212}
ATTACHED = {230: 227, 161: 160}        # event -> the event before it that it belongs to
EXTENDED = 172         # 3 bytes, then an event that goes with it (FL 25.2.3 and later)
# Events with one size per FL version (in every one of FL's projects): fixed
# layouts, whose size changes when FL changes the layout. Channel 209, 212, 215,
# 218, 219, 221, 228, 229; mixer 236; track 238; links 226, 227; head 237;
# pattern 252, 253; arrtail 242; mixer 243.
RECORDS = frozenset((209, 212, 215, 218, 219, 221, 228, 229, 236, 238, 226, 227, 237, 252, 253, 242, 243))

NOTE_SIZE = 24         # position u32, flags u16, channel u16, length u32, key u16, ...
PARAM_SIZE = 12        # mixer parameter: 4 bytes, the key (4) and the value (4)
PARAM_KEY = slice(4, 8)
PATTERN_BASE = 20480   # a clip's item is this plus a pattern number, or a channel
CLIP_SIZES = (((24, 2), 80), ((20, 99), 60), ((20, 0), 32))  # clip size from FL version
SERIAL = slice(32, 36)  # in 60- and 80-byte clips: an id FL gives every clip anew on each save
LAST_TRACK = 499        # a clip keeps 499 - its track
MAX_KEY = 131
MAX_PATTERN = 999
# A record both sides replaced counts as one edited by both when a replacement
# has the same (slice) and one of the others: a note's channel, and position or
# key; a clip's item, and position or track.
NOTE_ID = (slice(6, 8), (slice(0, 4), slice(12, 14)))
CLIP_ID = (slice(6, 8), (slice(0, 4), slice(12, 14)))
OLDEST = (20, 0)       # the grammar was measured on FL 20 to 26
LATEST = 26

WHO = {"base": "grundversionen", "mine": "din version", "theirs": "den andra versionen"}
PER_ID = frozenset(("head", "links", "arrtail", "notes", "mixer"))
# What live sync carries anyway (docs/protocol_v2.md, Keys), per kind of chunk:
# a save that changed nothing else needn't become a version (content_changes).
LIVE_EVENTS = {
    "head": frozenset((11, 17, 18, 66, 93, 156)),        # main swing, time signature, tempo (old and fine)
    "channel": frozenset((0, 22, 104, 128, 203, 219, 229)),  # on/off, mixer track (22; 104 in FL 2026), color, name, levels
    "insert": frozenset((95, 149, 204, 235, 236)),       # icon, color, name, routing, the track's settings
    "pattern": frozenset((150, 193)),                    # color, name
}
LIVE_KINDS = frozenset(("mixer", "track"))               # mixer levels, playlist tracks: all of it is live
# A channel's cut groups (132) are content: FL 26 gives each channel it adds
# (from the browser or a preset) the next number of its own, so two FLs with
# the same project give a new channel the same (measured in FL 26.1.7).
HEAD_PARTS = {194: "titeln", 156: "tempot", 195: "kommentarerna", 206: "genren", 207: "artisterna",
              197: "webbadressen", 231: "kanalgrupperna", 216: "kontrollkopplingarna"}


class MergeRefused(ValueError):
    """The versions can't be merged safely; `text` says why, in Swedish."""

    def __init__(self, code, text):
        super().__init__("%s: %s" % (code, text))
        self.code = code   # parse, format, version, channels, mixer, patterns, arrangements, structure, invalid
        self.text = text


@dataclasses.dataclass
class Merged:
    data: bytes          # the merged project
    mine_new: bool       # mine has real changes theirs lacks
    theirs_new: bool     # theirs has real changes mine lacks
    taken: list          # what came from theirs: "kanal 5 (Serum)", "playlisten", ...
    conflicts: list      # what both changed differently; mine's was kept
    structure: str       # "same", "mine" or "theirs": whose channels and plugins it has
    renumbered: list     # (old, new) numbers given to theirs' new patterns


def merge(base, mine, theirs, canon=None, localize=None):
    """Merges `mine` and `theirs`, two projects saved since `base`, into one
    (all bytes of .flp files). Raises MergeRefused when that can't be done
    safely.

    `canon` = {"base": {...}, "mine": {...}, "theirs": {...}}: per version, a
    token (the sample's sha256) for sample paths as stored in it. Paths
    compare by token where there is one, else by their text (case and
    slashes ignored). `localize` = {theirs' path: local path} for the sample
    paths in the parts taken from theirs."""
    try:
        return _Merge(base, mine, theirs, canon or {}, localize or {}).run()
    except MergeRefused:
        raise
    except Exception as e:  # noqa: BLE001 - a bug must not stop a save: newest wins instead
        raise MergeRefused("invalid", "sammanslagningen misslyckades (%s)" % e.__class__.__name__) from e


def content_changes(base, mine, canon=None):
    """What `mine` changed since `base` that live sync doesn't carry (clips,
    notes, samples, plugins and their state, channels, patterns...): the keys
    of those chunks, empty if every change is a live value that the others
    have already. `canon` as for merge(). MergeRefused if either can't be read."""
    canon = canon or {}
    b, m = _Project("base", base, canon.get("base")), _Project("mine", mine, canon.get("mine"))
    size = _clip_size((b, m, m))
    b.clip_size = m.clip_size = size
    changed = []
    for key in _unique([c.key for c in b.chunks] + [c.key for c in m.chunks]):
        before, after = b.get(key), m.get(key)
        if before is None or after is None:
            changed.append(key)  # added or removed: a channel, a pattern, a mixer track...
        elif key[0] not in LIVE_KINDS and _content(before) != _content(after):
            changed.append(key)
    return changed


PRESET_FORMAT = 0x20   # FLhd's format word of a channel preset (FL's own Data\Patches\Channel presets)
PRESET_TYPES = (0, 2)  # a sampler, a plugin's channel; layers, audio clips and automation point at more


def channel_preset(project, n):
    """Channel n of `project` as a channel preset: (.fst bytes, the channel's
    name). FL, asked to open it, adds a channel just like it at the end of the
    channel rack, with all its settings and its sample or its plugin's whole
    state, without loading the project again (measured in FL 26.1.7 with a
    sampler, FLEX and Serum 2: everything equal but what FL sets by itself,
    its mixer track too, which live sync brings). The preset is the channel's events
    after FL's version, as FL writes its own. None if the project has no such
    channel, or it is of another kind than PRESET_TYPES, or a plugin's channel
    without the plugin's state."""
    p = _Project("theirs", project, None)
    chunk = p.get(("channel", n))
    if chunk is None or p.events[0][0] != VERSION:
        return None
    kind, plugin = _generator(chunk)
    kind = None if kind is None else int.from_bytes(kind, "little")
    if kind not in PRESET_TYPES:
        return None
    if kind == 2 and (not plugin or not any(p.events[i][0] == STATE for i in range(chunk.lo, chunk.hi))):
        return None  # a plugin that didn't load where it was saved: nothing to bring
    body = p.data[p.events[0][1]:p.events[0][3]] + chunk.raw()
    data = (b"FLhd" + (6).to_bytes(4, "little") + PRESET_FORMAT.to_bytes(2, "little") + (1).to_bytes(2, "little")
            + p.ppq.to_bytes(2, "little") + b"FLdt" + len(body).to_bytes(4, "little") + body)
    return data, _channel_name(chunk)


def channel_samples(project, n):
    """The samples channel n of `project` plays: (its sample path, "" if none;
    the paths in its plugin's state, as for FPC or DirectWave), or None if
    there is no such channel."""
    p = _Project("theirs", project, None)
    chunk = p.get(("channel", n))
    if chunk is None:
        return None
    inside = [path for i in range(chunk.lo, chunk.hi) if p.events[i][0] == STATE
              for path in flp._plugin_paths(p.value(i))]
    return chunk.first_text(SAMPLE), inside


def appended_channels(base, theirs, canon=None):
    """The channels `theirs` added after all of `base`'s, when that is the
    only change of its channels' structure (none removed, moved or given
    another plugin): their numbers, or [] (also when they can't be told)."""
    canon = canon or {}
    try:
        b, t = _Project("base", base, canon.get("base")), _Project("theirs", theirs, canon.get("theirs"))
    except MergeRefused:
        return []
    old, new = b.kind("channel"), t.kind("channel")
    if len(new) <= len(old) or any(_generator(new[n]) != _generator(c) for n, c in enumerate(old)):
        return []
    if _moved(_channel_labels(old), _channel_labels(new)):
        return []
    return list(range(len(old), len(new)))


def _content(chunk):
    """A chunk's canonical events without those live sync carries."""
    skip = LIVE_EVENTS.get(chunk.key[0], frozenset())
    return tuple(event for event in chunk.canon() if event[0] not in skip)


# --- one version ---------------------------------------------------------------------

class _Chunk:
    """Events lo..hi-1 of a project, under `key`."""
    __slots__ = ("key", "project", "lo", "hi", "_canon")

    def __init__(self, key, project, lo, hi):
        self.key, self.project, self.lo, self.hi = key, project, lo, hi
        self._canon = None

    def raw(self):
        events = self.project.events
        return self.project.data[events[self.lo][1]:events[self.hi - 1][3]]

    def canon(self):
        """The events that count, as (id, value); see _Project.canon_event."""
        if self._canon is None:
            skip = HEAD_EPHEMERAL if self.key[0] == "head" else EPHEMERAL
            p = self.project
            self._canon = tuple(p.canon_event(i) for i in range(self.lo, self.hi) if p.events[i][0] not in skip)
        return self._canon

    def first_text(self, eid):
        p = self.project
        for i in range(self.lo, self.hi):
            if p.events[i][0] == eid:
                return p.text(i)
        return ""


class _Project:
    """One of the three versions (or the result), read and cut into chunks."""

    def __init__(self, role, data, canon, clip_size=None, check=True):
        self.role = role
        self.data = data = bytes(data)
        try:
            if check:
                flp.parse(data)  # every text whole: the events were read in step
            ppq, length_at, start, end = flp._chunk(data)
            self.events = list(flp._events(data, start, end, ALL_FIXED))
        except flp.FlpError as e:
            raise MergeRefused("parse", "%s gick inte att läsa (%s)" % (self.who(), e)) from None
        self.ppq = ppq
        self.prefix = data[:length_at]  # FLhd, and "FLdt" without its length
        self.suffix = data[end:]        # FL writes nothing after the events; kept if there is
        if not self.events or self.events[0][0] != VERSION:
            raise MergeRefused("format", "%s saknar FL-version" % self.who())
        self.version = self.value(0).decode("ascii", "replace").split("\0", 1)[0]
        version = self.major_minor()
        if version < OLDEST:
            raise MergeRefused("format", "projektet är sparat i en för gammal FL-version (%d.%d)" % version)
        if version[0] > LATEST:
            raise MergeRefused("format", "FLSync känner inte till FL %d.%d än" % version)
        self.wide = bool(flp._unicode(self.version))
        self.canon_map = canon or {}
        self.clip_size = clip_size
        self.chunks = _cut(self)  # the grammar holds from FL 20 on
        self.by_key = {c.key: c for c in self.chunks}
        self.kinds = collections.defaultdict(list)
        for c in self.chunks:
            self.kinds[c.key[0]].append(c)
        self._patterns = None

    def who(self):
        return WHO.get(self.role, "resultatet")

    def get(self, key):
        return self.by_key.get(key)

    def kind(self, kind):
        return self.kinds.get(kind, [])

    def major_minor(self):
        parts = self.version.split(".")
        try:
            return int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            raise MergeRefused("format", "%s har en okänd FL-version (%s)" % (self.who(), self.version)) from None

    def value(self, i):
        e = self.events[i]
        return self.data[e[2]:e[3]]

    def text(self, i):
        return flp._name(self.value(i), self.wide)[0]

    def canon_event(self, i):
        """(id, value) as compared: a sample path by token, notes and clips as
        multisets, mixer parameters by key."""
        eid = self.events[i][0]
        value = self.value(i)
        if eid == SAMPLE:
            return eid, self.sample_key(i)
        if eid == NOTES:
            notes = _records(value, NOTE_SIZE)
            return eid, (tuple(sorted(notes)) if notes is not None else value)
        if eid == PLAYLIST:
            clips = self.clips(i)
            return eid, (tuple(sorted(_masked(c) for c in clips)) if clips is not None else value)
        if eid == PARAMS:
            table = _params(value)
            return eid, (tuple(sorted(table.items())) if table is not None else value)
        return eid, value

    def sample_key(self, i):
        text = flp._text(self.value(i), self.wide, self.events[i][1])[0]
        token = self.canon_map.get(text)
        if token is not None:
            return "token", token
        return "path", text.casefold().replace("/", "\\")

    def clips(self, i):
        """The clips of the 233 at event i, or None if they can't be told apart."""
        return _records(self.value(i), self.clip_size) if self.clip_size else None

    def patterns(self):
        """Every pattern number the project uses: in notes, settings or clips
        (once the clip size is known)."""
        if self._patterns is None:
            found = {c.key[1] for c in self.kind("notes") + self.kind("pattern")}
            for c in self.kind("playlist"):
                for clip in self.clips(c.lo) or ():
                    item = _u16(clip, 6)
                    if item > PATTERN_BASE:
                        found.add(item - PATTERN_BASE)
            if self.clip_size is None:
                return found
            self._patterns = found
        return set(self._patterns)


def _cut(p):
    """The chunks of project `p` (see the module docstring), in file order.
    MergeRefused for anything the grammar doesn't allow."""
    events = p.events
    chunks = []
    seen = {("head",)}
    key, lo = ("head",), 0
    state = "head"
    channels = arrangement = inserts = 0
    occurrences = collections.Counter()
    master = next((i for i, e in enumerate(events) if e[0] == INSERT), None)
    while master and events[master - 1][0] in INSERT_HEAD:
        master -= 1

    def refuse(i, what):
        raise MergeRefused("format", "%s har en uppbyggnad som FLSync inte känner igen (%s, händelse %d)"
                           % (p.who(), what, i))

    def begin(new_key, i):
        nonlocal key, lo
        if i > lo:
            chunks.append(_Chunk(key, p, lo, i))
        if new_key in seen:
            refuse(i, "%s två gånger" % " ".join(map(str, new_key)))
        seen.add(new_key)
        key, lo = new_key, i

    for i, (eid, _, vstart, vend) in enumerate(events):
        number = int.from_bytes(p.data[vstart:vend], "little") if eid < TEXT else None
        if i == master:
            begin(("insert", inserts), i)
            state = "insert head"
        if state == "insert done" and eid != PARAMS:
            inserts += 1
            begin(("insert", inserts), i)
            state = "insert head"
        if state in ("insert head", "insert"):
            if eid in ANCHORS or eid == PARAMS or (eid == INSERT and state == "insert"):
                refuse(i, "händelse %d i mixerkanal %d" % (eid, inserts))
            if eid == INSERT:
                state = "insert"
            elif eid == INSERT_END and state == "insert":
                state = "insert done"
        elif state == "mixer":
            if eid in ANCHORS or eid in (INSERT, PARAMS):
                refuse(i, "händelse %d efter mixerns parametrar" % eid)
        elif eid == PARAMS:
            begin(("mixer",), i)
            state = "mixer"
        elif eid == INSERT:
            refuse(i, "mixerkanal")  # never: the first 236 starts the master
        elif eid == CHANNEL:
            if state not in ("head", "notes", "links", "rack") or number != channels:
                refuse(i, "kanal %d" % number)
            begin(("channel", number), i)
            channels += 1
            state = "rack"
        elif eid == PATTERN:
            if state in ("head", "notes"):
                begin(("notes", number), i)
                state = "notes"
            elif state == "rack":
                begin(("pattern", number, occurrences[number]), i)
                occurrences[number] += 1
            else:
                refuse(i, "mönster %d" % number)
        elif eid in LINKS and state in ("head", "notes"):
            begin(("links",), i)
            state = "links"
        elif eid == ARRANGEMENT:
            if state not in ("head", "notes", "links", "rack", "playlist", "markers", "track"):
                refuse(i, "arrangemang %d" % number)
            arrangement = number
            begin(("arrangement", number), i)
            state = "arrangement"
        elif eid == PLAYLIST:
            if state != "arrangement":
                refuse(i, "playlist")
            begin(("playlist", arrangement), i)
            state = "playlist"
        elif eid == TRACK:
            if state not in ("playlist", "markers", "track") or vend - vstart < 4:
                refuse(i, "spår")
            begin(("track", arrangement, _u32(p.data, vstart)), i)
            state = "track"
        elif state == "playlist":
            begin(("markers", arrangement), i)
            state = "markers"
        elif state == "track" and eid not in TRACK_EVENTS:
            begin(("arrtail",), i)
            state = "arrtail"
    if state in ("insert head", "insert"):
        refuse(len(events), "mixerkanal %d utan slut" % inserts)
    begin(None, len(events))  # closes the last chunk
    return chunks


# --- records -------------------------------------------------------------------------

def _u16(data, at):
    return int.from_bytes(data[at:at + 2], "little")


def _u32(data, at):
    return int.from_bytes(data[at:at + 4], "little")


def _records(value, size):
    """`value` cut into records of `size` bytes, or None if it doesn't divide."""
    if not size or len(value) % size:
        return None
    return [value[i:i + size] for i in range(0, len(value), size)]


def _masked(clip):
    """A clip without the id FL gives it anew on every save."""
    if len(clip) < SERIAL.stop:
        return clip
    return clip[:SERIAL.start] + bytes(SERIAL.stop - SERIAL.start) + clip[SERIAL.stop:]


def _params(value):
    """The mixer parameters of a 225 by key, or None if a key repeats."""
    records = _records(value, PARAM_SIZE)
    if records is None:
        return None
    table = {r[PARAM_KEY]: r for r in records}
    return table if len(table) == len(records) else None


def _by_position(records):
    positions = [_u32(r, 0) for r in records]
    return all(a <= b for a, b in zip(positions, positions[1:]))


def _event(eid, value):
    if eid >= TEXT:
        return bytes([eid]) + flp._varint(len(value)) + value
    return bytes([eid]) + value


def _merge_records(base, mine, theirs, identity):
    """Mine's and theirs' records merged as a multiset: each one's count is
    base's plus both sides' changes, a change both made counted once. Where
    both replaced the same base record (an edit, told by `identity`) with
    different ones, theirs' replacement is left out. Returns ([(record,
    "mine"/"theirs")], conflicts): mine's records in mine's order, then
    theirs' additions in theirs' order."""
    cb, cm, ct = (collections.Counter(s) for s in (base, mine, theirs))
    counts = {}
    for r in set(cb) | set(cm) | set(ct):
        dm, dt = cm[r] - cb[r], ct[r] - cb[r]
        if dm > 0 and dt > 0:
            change = max(dm, dt)
        elif dm < 0 and dt < 0:
            change = min(dm, dt)
        else:
            change = dm + dt
        counts[r] = max(0, cb[r] + change)
    conflicts = 0
    both_removed = (cb - cm) & (cb - ct)
    if both_removed:
        added_m, added_t = _Additions(cm - cb, identity), _Additions(ct - cb, identity)
        for r, n in both_removed.items():
            for _ in range(n):
                a, b = added_m.replacement(r), added_t.replacement(r)
                if a is not None and b is not None and a != b:
                    conflicts += 1
                    if counts[b] > cm[b]:  # theirs' edit is in the result: mine's wins
                        counts[b] -= 1
                elif (a is None) != (b is None):
                    conflicts += 1  # removed by one, edited by the other: the edit stays
    out = []
    for side, records in (("mine", mine), ("theirs", theirs)):
        for r in records:
            if counts.get(r, 0) > 0:
                counts[r] -= 1
                out.append((r, side))
    return out, conflicts


class _Additions:
    """A side's added records, to find the one that replaced a base record."""

    def __init__(self, counts, identity):
        self.counts = counts
        self.same, self.either = identity
        self.index = collections.defaultdict(list)
        for r in counts:
            for n, s in enumerate(self.either):
                self.index[n, r[self.same], r[s]].append(r)

    def replacement(self, record):
        """The added record most like `record` edited: the same `same` field
        and one of `either`, the most bytes alike. Taken; None if none."""
        best = None
        for n, s in enumerate(self.either):
            for candidate in self.index.get((n, record[self.same], record[s]), ()):
                if self.counts[candidate] > 0:
                    score = sum(a == b for a, b in zip(candidate, record))
                    if best is None or score > best[0]:
                        best = (score, candidate)
        if best is None:
            return None
        self.counts[best[1]] -= 1
        return best[1]


def _clip_size(projects):
    """The clip record size that fits every playlist in all three versions:
    each clip's item a pattern (20480 + number) or an existing channel, its
    track at most 499. None (a playlist then merges as one piece) if no size
    fits, or several and the versions' FL doesn't tell which."""
    fitting = [size for _, size in CLIP_SIZES if all(_clips_fit(p, size) for p in projects)]
    if len(fitting) == 1:
        return fitting[0]
    version = projects[1].major_minor()
    expected = next((size for since, size in CLIP_SIZES if version >= since), None)
    return expected if expected in fitting else None


def _clips_fit(p, size):
    channels = len(p.kind("channel"))
    for c in p.kind("playlist"):
        clips = _records(p.value(c.lo), size)
        if clips is None:
            return False
        for clip in clips:
            item = _u16(clip, 6)
            if _u16(clip, 4) != PATTERN_BASE or _u16(clip, 12) > LAST_TRACK \
                    or item > PATTERN_BASE + MAX_PATTERN or channels <= item <= PATTERN_BASE:
                return False
    return True


def _notes_fit(p, notes):
    """Whether 24-byte records read as notes: a channel the project has, a key
    FL can play."""
    channels = len(p.kind("channel"))
    return all(_u16(n, 6) < channels and _u16(n, 12) <= MAX_KEY for n in notes)


# --- the merge -------------------------------------------------------------------------

class _Merge:
    def __init__(self, base, mine, theirs, canon, localize):
        self.canon = canon
        self.localize = localize
        self.base = _Project("base", base, canon.get("base"))
        self.mine = _Project("mine", mine, canon.get("mine"))
        self.theirs = _Project("theirs", theirs, canon.get("theirs"))
        self.taken, self.conflicts, self.renumbered = [], [], []
        self.mine_new = self.theirs_new = False
        self.fresh = None  # ids for clips from theirs
        self.versions = []  # the FL versions (major, minor) that saved the three

    @property
    def projects(self):
        return self.base, self.mine, self.theirs

    def run(self):
        self.check_versions()
        size = _clip_size(self.projects)
        for p in self.projects:
            p.clip_size = size
        if len(self.versions) > 1:
            self.check_clips(size)
        _check_channels(*self.projects)
        _check_mixer(*self.projects)
        _check_patterns(*self.projects)
        _check_arrangements(*self.projects)
        self.renumber()
        self.fresh = _fresh_ids(self.mine)
        layout = self.layout()
        merged = {}
        for key in layout:
            pieces = self.merge_key(key)
            if pieces:
                merged[key] = pieces
        order = [key for key in layout if key in merged]
        if len(self.versions) > 1:
            self.check_records(merged)
        data = self.assemble(order, merged)
        result = self.validate(data, order)
        return Merged(data, self.mine_new, self.theirs_new, _unique(self.taken), _unique(self.conflicts),
                      self.structure(result), self.renumbered)

    # --- before ------------------------------------------------------------------

    def check_versions(self):
        if len({p.ppq for p in self.projects}) > 1:
            raise MergeRefused("format", "versionerna har olika tidsupplösning (PPQ)")
        if len({p.prefix[4:10] for p in self.projects}) > 1:
            raise MergeRefused("format", "versionerna har olika filhuvuden")
        self.versions = sorted({p.major_minor() for p in self.projects})

    def other_layout(self):
        names = ["%d.%d" % v for v in self.versions]
        return MergeRefused("version", "projekten är sparade i olika FL-versioner som sparar på olika sätt (%s)"
                            % " och ".join([", ".join(names[:-1]), names[-1]]))

    def check_clips(self, size):
        """Between FL versions, clips merge with one record size: the one mine's
        FL writes (told by mine's clips, or without any by its version)."""
        if any(p.value(c.lo) for p in self.projects for c in p.kind("playlist")):
            if size is None or size != _clip_size((self.mine,) * 3):
                raise self.other_layout()

    def check_records(self, merged):
        """Between FL versions, every RECORDS event the result takes from theirs
        must have a size mine has for that event in the same kind of chunk; one
        mine never writes there goes through (FL skips what it doesn't know)."""
        sizes = collections.defaultdict(set)
        for c in self.mine.chunks:
            for i in range(c.lo, c.hi):
                eid, _, vstart, vend = self.mine.events[i]
                if eid in RECORDS:
                    sizes[c.key[0], eid].add(vend - vstart)
        for key, pieces in merged.items():
            for piece in pieces:
                if isinstance(piece, bytes) or piece[0] is not self.theirs:
                    continue  # mine's, or records merged one by one (notes, clips, mixer parameters)
                p, lo, hi = piece
                for eid, _, vstart, vend in p.events[lo:hi]:
                    known = sizes.get((key[0], eid))
                    if known and vend - vstart not in known:
                        raise self.other_layout()

    def renumber(self):
        """Theirs' new patterns that mine also has, with other content, get the
        next free numbers, in theirs' notes, settings and clips."""
        base, mine, theirs = self.projects
        free = max(base.patterns() | mine.patterns() | theirs.patterns() | {0}) + 1
        moves = {}
        for n in sorted((mine.patterns() & theirs.patterns()) - base.patterns()):
            keys = {c.key for p in (mine, theirs) for c in p.chunks if c.key[0] in ("notes", "pattern")
                    and c.key[1] == n}
            if all(_same(mine.get(k), theirs.get(k)) for k in keys):
                continue  # the same pattern
            if free > MAX_PATTERN:
                raise MergeRefused("patterns", "det finns inga lediga mönsternummer")
            moves[n] = free
            free += 1
        if not moves:
            return
        data = bytearray(theirs.data)
        for c in theirs.chunks:
            if c.key[0] in ("notes", "pattern") and c.key[1] in moves:
                _, _, vstart, vend = theirs.events[c.lo]
                data[vstart:vend] = moves[c.key[1]].to_bytes(vend - vstart, "little")
            elif c.key[0] == "playlist":
                clips = theirs.clips(c.lo)
                if clips is None:
                    raise MergeRefused("patterns", "playlistens klipp går inte att numrera om")
                start = theirs.events[c.lo][2]
                for at, clip in enumerate(clips):
                    new = moves.get(_u16(clip, 6) - PATTERN_BASE)
                    if new is not None:
                        where = start + at * theirs.clip_size + 6
                        data[where:where + 2] = (PATTERN_BASE + new).to_bytes(2, "little")
        self.theirs = _Project("theirs", data, self.canon.get("theirs"), theirs.clip_size, check=False)
        self.renumbered = sorted(moves.items())

    def layout(self):
        """Every key in the order the result has them: mine's order; a key mine
        lacks after the one before it in theirs (or base), a renumbered
        pattern's after the last of its kind, as FL numbers them up."""
        order = [c.key for c in self.mine.chunks]
        placed = set(order)
        moved = {new for _, new in self.renumbered}
        for p in (self.theirs, self.base):
            previous = None
            for c in p.chunks:
                k = c.key
                if k not in placed:
                    same_kind = [n for n, o in enumerate(order) if k[0] in ("notes", "pattern") and k[1] in moved
                                 and o[0] == k[0] and o[2:] == k[2:] and o[1] < k[1]]
                    if same_kind:
                        at = same_kind[-1] + 1
                    else:
                        at = order.index(previous) + 1 if previous is not None else len(order)
                    order.insert(at, k)
                    placed.add(k)
                previous = k
        return order

    # --- per key ------------------------------------------------------------------

    def merge_key(self, key):
        """The merged chunk as pieces (bytes, or (project, lo, hi) for a
        project's events as they are); None or [] for none."""
        b, m, t = (p.get(key) for p in self.projects)
        if key[0] in PER_ID:
            return self.by_id(key, b, m, t)
        if key[0] == "playlist" and self.mine.clip_size:
            return self.clips(key, b, m, t)
        return self.whole(key, b, m, t)

    def whole(self, key, b, m, t):
        """A chunk merged as one piece."""
        conflict = False
        if m is not None and t is not None:
            if b is not None and _same(t, b):
                chosen = m
            elif b is not None and _same(m, b):
                chosen = t
            elif _same(m, t):
                chosen = m
            else:
                chosen, conflict = m, True
                if b is None and key[0] in ("channel", "arrangement", "playlist"):
                    # the checks refuse these; never a guess
                    raise MergeRefused("invalid", "%s finns i båda versionerna" % self.describe(key))
        elif m is None and t is None:
            chosen = None
        elif m is None:  # mine removed it, or theirs added it
            chosen = None if b is not None and _same(t, b) else t
            conflict = b is not None and chosen is not None
        else:            # theirs removed it, or mine added it
            chosen = None if b is not None and _same(m, b) else m
            conflict = b is not None and chosen is not None
        self.count(key, chosen is not None and chosen is t and not _same(t, m), conflict,
                   not _same(chosen, m), not _same(chosen, t))
        return [(chosen.project, chosen.lo, chosen.hi)] if chosen is not None else None

    def count(self, key, taken, conflict, theirs_new, mine_new, eid=None):
        if taken:
            self.taken.append(self.describe(key, eid, self.theirs))  # named as in the result
        if conflict:
            self.conflicts.append(self.describe(key, eid))
        self.theirs_new |= theirs_new
        self.mine_new |= mine_new

    def by_id(self, key, b, m, t):
        """A chunk merged per event id."""
        groups = [_groups(c) for c in (b, m, t)]
        result = {}  # id -> its items: (project, lo, hi) or bytes
        for eid in dict.fromkeys(itertools.chain(groups[1], groups[2], groups[0])):
            gb, gm, gt = (g.get(eid, []) for g in groups)
            if eid in MINE_ALWAYS or eid in EPHEMERAL:
                result[eid] = gm
                continue
            if key[0] == "notes" and eid == PATTERN:
                result[eid] = gm or gt  # the anchor, of whichever side has the chunk: the same 65 n
                continue
            cb, cm, ct = (_group_canon(g) for g in (gb, gm, gt))
            merged = None
            if eid == NOTES:
                merged = self.notes(key, gb, gm, gt)
            elif eid == PARAMS:
                merged = self.params(gb, gm, gt)
            conflict = False
            if merged is not None:
                chosen, cr = merged
            elif ct == cb or cm == ct:
                chosen, cr = gm, cm
            elif cm == cb:
                chosen, cr = gt, ct
            else:  # mine's, and a conflict (but mixer parameters are synced live anyway)
                chosen, cr, conflict = gm, cm, eid != PARAMS
            self.count(key, cr != cm, conflict, cr != cm, cr != ct, eid)
            result[eid] = chosen
        if key[0] == "notes" and not any(items for eid, items in result.items() if eid != PATTERN):
            return None  # FL writes no notes chunk for a pattern without notes
        return self.id_order(groups[1], groups[2], result)

    def id_order(self, gm, gt, result):
        """The merged items in mine's order: an id's merged items where mine has
        its first; an id mine lacks after the id before it in theirs."""
        out = []  # [(id, item)]
        placed = set()
        for eid, item in _in_order(gm):
            chosen = result.get(eid, [])
            if chosen is gm.get(eid):
                out.append((eid, item))
            elif eid not in placed:
                out += [(eid, x) for x in chosen]
            placed.add(eid)
        previous = None
        for eid, _ in _in_order(gt):
            if eid not in placed:
                placed.add(eid)
                at = 0
                if previous is not None:
                    at = 1 + max(n for n, (e, _) in enumerate(out) if e == previous)
                out[at:at] = [(eid, x) for x in result.get(eid, [])]
            if any(e == eid for e, _ in out):
                previous = eid
        return [item for _, item in out]

    def notes(self, key, gb, gm, gt):
        """224 events merged note by note: (items, canonical form), or None
        when they don't read as notes (then they merge as one)."""
        sets = []
        for group in (gb, gm, gt):
            notes = []
            for p, lo, hi in group:
                for i in range(lo, hi):
                    found = _records(p.value(i), NOTE_SIZE)
                    if found is None or not _notes_fit(p, found):
                        return None
                    notes += found
            sets.append(notes)
        merged, conflicts = _merge_records(*sets, identity=NOTE_ID)
        notes = [n for n, _ in merged]
        if conflicts:
            self.conflicts.append(self.describe(key, NOTES))
        if collections.Counter(notes) == collections.Counter(sets[1]):
            return gm, _group_canon(gm)
        if all(_by_position(s) for s in sets):
            notes.sort(key=lambda n: _u32(n, 0))
        if not notes:
            return [], ()
        return [_event(NOTES, b"".join(notes))], ((NOTES, tuple(sorted(notes))),)

    def params(self, gb, gm, gt):
        """A 225 merged per mixer parameter: (items, canonical form), or None
        when its records can't be keyed (then it merges as one)."""
        tables = []
        for group in (gb, gm, gt):
            if not group:
                tables.append({})
                continue
            p, lo, hi = group[0]
            table = _params(p.value(lo)) if len(group) == 1 and hi == lo + 1 else None
            if table is None:
                return None
            tables.append(table)
        tb, tm, tt = tables
        result = {}
        for k in dict.fromkeys(itertools.chain(tm, tt)):
            b, m, t = tb.get(k), tm.get(k), tt.get(k)
            chosen = t if m == b and t != b else m
            if chosen is not None:
                result[k] = chosen
        if result == tm:
            return gm, _group_canon(gm)
        order = _theirs_after(list(k for k in tm if k in result), [k for k in tt if k in result])
        return [_event(PARAMS, b"".join(result[k] for k in order))], ((PARAMS, tuple(sorted(result.items()))),)

    def clips(self, key, b, m, t):
        """A playlist merged clip by clip."""
        if m is None and t is None:
            return None
        sets = [c.project.clips(c.lo) if c is not None else [] for c in (b, m, t)]
        masked = [[_masked(c) for c in s] for s in sets]
        merged, conflicts = _merge_records(*masked, identity=CLIP_ID)
        plain = [c for c, _ in merged]
        canon = ((PLAYLIST, tuple(sorted(plain))),)
        if m is not None and collections.Counter(plain) == collections.Counter(masked[1]):
            self.count(key, False, conflicts > 0, False, not _same(m, t))
            return [(m.project, m.lo, m.hi)]
        # mine's clips as they are, theirs' with ids none of mine has
        originals = {"mine": collections.defaultdict(list), "theirs": collections.defaultdict(list)}
        for side, s, ms in (("mine", sets[1], masked[1]), ("theirs", sets[2], masked[2])):
            for clip, plain_clip in zip(s, ms):
                originals[side][plain_clip].append(clip)
        out = []
        for plain_clip, side in merged:
            clip = originals[side][plain_clip].pop(0)
            out.append(clip if side == "mine" else self.fresh_id(clip))
        if all(_by_position(s) for s in sets):
            out.sort(key=lambda c: _u32(c, 0))
        self.count(key, True, conflicts > 0, True, t is None or canon != t.canon())
        return [_event(PLAYLIST, b"".join(out))]

    def fresh_id(self, clip):
        """A clip from theirs with an id no clip of mine has."""
        if len(clip) < SERIAL.stop:
            return clip
        return clip[:SERIAL.start] + next(self.fresh).to_bytes(2, "little") + clip[SERIAL.start + 2:]

    # --- the result ----------------------------------------------------------------

    def assemble(self, order, merged):
        """The merged project's bytes, with FLhd's channel count and FLdt's
        length set."""
        body = bytearray()
        for key in order:
            for piece in merged[key]:
                body += piece if isinstance(piece, bytes) else self.events_bytes(*piece)
        channels = sum(k[0] == "channel" for k in order)
        if len(body) > 0xFFFFFFFF or channels > 0xFFFF:
            raise MergeRefused("invalid", "projektet blir för stort")
        prefix = bytearray(self.mine.prefix)
        prefix[10:12] = channels.to_bytes(2, "little")
        return bytes(prefix) + len(body).to_bytes(4, "little") + bytes(body) + self.mine.suffix

    def events_bytes(self, p, lo, hi):
        """Events lo..hi-1 of `p` as they are, theirs' sample paths made local."""
        events = p.events
        if p.role != "theirs" or not self.localize:
            return p.data[events[lo][1]:events[hi - 1][3]]
        out = bytearray()
        copied = events[lo][1]
        for i in range(lo, hi):
            eid, at, vstart, vend = events[i]
            if eid != SAMPLE:
                continue
            text, wide = flp._text(p.data[vstart:vend], p.wide, at)
            local = self.localize.get(text)
            if local is None or local == text:
                continue
            try:
                value = flp._encode(local, wide)
            except (flp.FlpError, TypeError) as e:
                raise MergeRefused("invalid", "en samplesökväg går inte att skriva (%s)" % e) from None
            out += p.data[copied:at] + _event(SAMPLE, value)
            copied = vend
        out += p.data[copied:events[hi - 1][3]]
        return bytes(out)

    def validate(self, data, order):
        """The merged project read back: it must parse, follow the grammar and
        hold what was merged, in that order."""
        try:
            result = _Project("result", data, self.canon.get("mine"), self.mine.clip_size)
        except MergeRefused as e:
            raise MergeRefused("invalid", "sammanslagningen blev ingen giltig projektfil (%s)" % e.text) from None
        if [c.key for c in result.chunks] != order:
            raise MergeRefused("invalid", "sammanslagningen blev ingen giltig projektfil (delarna)")
        if _u16(data, 10) != len(result.kind("channel")):
            raise MergeRefused("invalid", "sammanslagningen blev ingen giltig projektfil (kanalerna)")
        if len(result.kind("insert")) != len(self.mine.kind("insert")):
            raise MergeRefused("invalid", "sammanslagningen blev ingen giltig projektfil (mixern)")
        notes = [n for c in result.kind("notes") for i in range(c.lo, c.hi) if result.events[i][0] == NOTES
                 for n in _records(result.value(i), NOTE_SIZE) or ()]
        if self.mine.clip_size and not _clips_fit(result, self.mine.clip_size) or not _notes_fit(result, notes):
            raise MergeRefused("invalid", "sammanslagningen blev ingen giltig projektfil (noter eller klipp)")
        return result

    def structure(self, result):
        """Whose channels and plugins the result has, as FL's structure
        fingerprint sees them: channel types and plugins, mixer tracks and the
        plugins in their slots."""
        merged = _structure(result)
        if merged == _structure(self.mine):
            return "same" if merged == _structure(self.theirs) else "mine"
        if merged == _structure(self.theirs):
            return "theirs"
        raise MergeRefused("structure", "ni har båda ändrat kanaler eller plugins")

    # --- words -------------------------------------------------------------------

    def describe(self, key, eid=None, first=None):
        """A part of the project in Swedish, as the user knows it; names from
        the project `first` if it has them, else mine's, theirs' or base's."""
        def find(k, name):
            for p in (first, self.mine, self.theirs, self.base):
                c = p.get(k) if p is not None else None
                if c is not None and name(c):
                    return name(c)
            return ""

        kind = key[0]
        if kind == "head":
            return HEAD_PARTS.get(eid, "projektets inställningar")
        if kind == "links":
            return "kontrollkopplingarna"
        if kind == "arrtail":
            return "playlistens inställningar"
        if kind == "mixer":
            return "mixerns reglage" if eid == PARAMS else "mixerns inställningar"
        if kind == "notes":
            return "noter i " + _label("mönster %d" % key[1], find(("pattern", key[1], 0), _pattern_name))
        if kind == "pattern":
            return _label("mönster %d" % key[1], find(("pattern", key[1], 0), _pattern_name))
        if kind == "channel":
            return _label("kanal %d" % (key[1] + 1), find(key, _channel_name))
        if kind == "insert":
            return _label("mixerkanal %d" % key[1] if key[1] else "mixerns master", find(key, _insert_name))
        if kind == "arrangement":
            return _label("arrangemang %d" % (key[1] + 1), find(key, _arrangement_name))
        where = ""
        if any(k[0] == "arrangement" and k[1] for p in self.projects for k in p.by_key):
            where = " i " + _label("arrangemang %d" % (key[1] + 1), find(("arrangement", key[1]), _arrangement_name))
        if kind == "track":
            return _label("spår %d" % key[2], find(key, _track_name)) + where
        if kind == "playlist":
            return "playlisten" + where
        if kind == "markers":
            return "markörerna" + where
        return " ".join(map(str, key))


def _label(what, name):
    return "%s (%s)" % (what, name) if name else what


def _unique(items):
    return list(dict.fromkeys(items))


def _same(a, b):
    """Whether two chunks (or None) hold the same, EPHEMERAL events aside."""
    if a is None or b is None:
        return a is b
    return a.raw() == b.raw() or a.canon() == b.canon()


def _groups(chunk):
    """{id: [(project, lo, hi), ...]}: a chunk's events by id, in order; an
    ATTACHED event, or the one after a 172, in one item with the event before
    it."""
    groups = {}
    if chunk is None:
        return groups
    p = chunk.project
    item = None
    for i in range(chunk.lo, chunk.hi):
        eid = p.events[i][0]
        if item is not None and item[2] == i and (ATTACHED.get(eid) == item[0]
                                                  or item[0] == EXTENDED and item[2] == item[1] + 1):
            item[2] = i + 1
        else:
            item = [eid, i, i + 1]
            groups.setdefault(eid, []).append(item)
    return {eid: [(p, lo, hi) for _, lo, hi in items] for eid, items in groups.items()}


def _group_canon(group):
    return tuple(p.canon_event(i) for p, lo, hi in group for i in range(lo, hi))


def _in_order(groups):
    """(id, item) of every item in `groups`, in the file's order."""
    return sorted(((eid, item) for eid, items in groups.items() for item in items), key=lambda pair: pair[1][1])


def _theirs_after(order, theirs):
    """`order` with the keys of `theirs` it lacks, each after the key before it
    in `theirs` (at the start without one)."""
    order = list(order)
    where = {k: n for n, k in enumerate(order)}
    previous = None
    for k in theirs:
        if k not in where:
            at = where[previous] + 1 if previous is not None else 0
            order.insert(at, k)
            where = {k2: n for n, k2 in enumerate(order)}
        previous = k
    return order


def _fresh_ids(mine):
    """Clip ids that no clip of mine has: up from mine's highest, then from 1."""
    used = set()
    if mine.clip_size and mine.clip_size >= SERIAL.stop:
        for c in mine.kind("playlist"):
            used.update(_u16(clip, SERIAL.start) for clip in mine.clips(c.lo))
    top = max(used, default=0)
    for n in itertools.chain(range(top + 1, 0x10000), range(1, top + 1)):
        if n not in used:
            yield n
    raise MergeRefused("invalid", "för många klipp i playlisten")


# --- what the chunks say ---------------------------------------------------------------

def _generator(chunk):
    """A channel's type and plugin (its first 201; a VST by its own name)."""
    p = chunk.project
    kind = plugin = None
    for i in range(chunk.lo, chunk.hi):
        eid = p.events[i][0]
        if eid == TYPE and kind is None:
            kind = p.value(i)
        elif eid == PLUGIN and plugin is None:
            plugin = p.text(i)
            if plugin.lower() == flp.WRAPPER:
                state = next((j for j in range(i + 1, chunk.hi) if p.events[j][0] == STATE), None)
                if state is not None:
                    plugin = flp._wrapped_name(p.value(state)) or plugin
    return kind, plugin


def _own_name(chunk):
    """The name the user gave a channel (192 before FL 12, else 203)."""
    return chunk.first_text(OLD_NAME) or chunk.first_text(NAME)


def _channel_name(chunk):
    """The name FL shows for a channel: its own, its plugin's or its sample's."""
    name = _own_name(chunk) or _generator(chunk)[1]
    if name:
        return name
    path = chunk.first_text(SAMPLE)
    return ntpath.splitext(ntpath.basename(path))[0] if path else ""


def _pattern_name(chunk):
    return chunk.first_text(PATTERN_NAME)


def _insert_name(chunk):
    return chunk.first_text(INSERT_NAME)


def _track_name(chunk):
    return chunk.first_text(TRACK_NAME)


def _arrangement_name(chunk):
    return chunk.first_text(ARRANGEMENT_NAME)


def _slots(chunk):
    """((slot, plugin), ...) of a mixer track's slots that hold a plugin."""
    p = chunk.project
    found = []
    plugin = state = None
    inside = False
    for i in range(chunk.lo, chunk.hi):
        eid = p.events[i][0]
        if eid == INSERT:
            inside = True
        elif not inside:
            continue
        elif eid == PLUGIN:
            plugin, state = p.text(i), None
        elif eid == STATE and plugin is not None and state is None:
            state = p.value(i)
        elif eid == SLOT:
            if plugin:
                if plugin.lower() == flp.WRAPPER and state is not None:
                    plugin = flp._wrapped_name(state) or plugin
                found.append((_u16(p.value(i), 0), plugin))
            plugin = state = None
    return tuple(found)


def _structure(p):
    return (tuple(_generator(c) for c in p.kind("channel")), tuple(_slots(c) for c in p.kind("insert")))


# --- refusals ------------------------------------------------------------------------

def _moved(base_labels, side_labels):
    """Whether something with a label (a name) in base is at another index in
    a side, while its own index there has something else."""
    where = collections.defaultdict(set)
    for n, label in side_labels.items():
        if label:
            where[label].add(n)
    for n, label in base_labels.items():
        if label and side_labels.get(n) != label:
            if any(other != n and base_labels.get(other) != label for other in where.get(label, ())):
                return True
    return False


def _check_channels(base, mine, theirs):
    """Base's channels must be the first ones in both sides, with the same
    type and plugin, none moved; only one side may add channels."""
    old = base.kind("channel")
    for p in (mine, theirs):
        new = p.kind("channel")
        for n, c in enumerate(old):
            if n >= len(new) or _generator(new[n]) != _generator(c):
                raise MergeRefused("channels", "kanal %d (%s) har tagits bort, flyttats eller fått ett annat "
                                   "plugin i %s" % (n + 1, _channel_name(c) or "utan namn", p.who()))
        if _moved(_channel_labels(old), _channel_labels(new)):
            raise MergeRefused("channels", "kanaler har flyttats i %s" % p.who())
    added_m, added_t = mine.kind("channel")[len(old):], theirs.kind("channel")[len(old):]
    if added_m and added_t and (len(added_m) != len(added_t)
                                or not all(_same(a, b) for a, b in zip(added_m, added_t))):
        raise MergeRefused("channels", "ni har båda lagt till kanaler")


def _channel_labels(chunks):
    """A channel's own name, or else its sample."""
    labels = {}
    for c in chunks:
        p = c.project
        label = _own_name(c)
        if not label:
            sample = next((i for i in range(c.lo, c.hi) if p.events[i][0] == SAMPLE), None)
            if sample is not None and p.text(sample):
                label = p.sample_key(sample)
        labels[c.key[1]] = label or None
    return labels


def _check_mixer(base, mine, theirs):
    """The same number of mixer tracks, none moved."""
    if len({len(p.kind("insert")) for p in (base, mine, theirs)}) > 1:
        raise MergeRefused("mixer", "antalet mixerkanaler har ändrats")
    labels = [{c.key[1]: (_insert_name(c), _slots(c)) if _insert_name(c) or _slots(c) else None
               for c in p.kind("insert")} for p in (base, mine, theirs)]
    for side, p in ((labels[1], mine), (labels[2], theirs)):
        if _moved(labels[0], side):
            raise MergeRefused("mixer", "mixerkanaler har flyttats i %s" % p.who())


def _check_patterns(base, mine, theirs):
    """None of base's patterns gone or moved: FL numbers them anew when one is
    deleted, inserted or moved. A pattern is there while it has notes,
    settings or a clip; one emptied and without a name or clips is gone."""
    have = base.patterns()
    for p in (mine, theirs):
        gone = sorted(have - p.patterns())
        if gone:
            raise MergeRefused("patterns", "mönster %d har tagits bort eller flyttats i %s" % (gone[0], p.who()))
        if _moved(_pattern_labels(base), _pattern_labels(p)):
            raise MergeRefused("patterns", "mönster har flyttats i %s" % p.who())


def _pattern_labels(p):
    """A pattern's name, or else its notes."""
    labels = {}
    for n in p.patterns():
        settings, notes = p.get(("pattern", n, 0)), p.get(("notes", n))
        label = _pattern_name(settings) if settings is not None else ""
        if not label and notes is not None:
            label = tuple(e for e in notes.canon() if e[0] == NOTES and e[1])
        labels[n] = label or None
    return labels


def _check_arrangements(base, mine, theirs):
    """None of base's arrangements gone or playlist tracks moved; no
    arrangement added by both, unless the same."""
    have = [{c.key[1] for c in p.kind("arrangement")} for p in (base, mine, theirs)]
    for numbers, p in ((have[1], mine), (have[2], theirs)):
        if have[0] - numbers:
            raise MergeRefused("arrangements", "ett arrangemang har tagits bort i %s" % p.who())
        for n in have[0]:
            if _moved(_track_labels(base, n), _track_labels(p, n)):
                raise MergeRefused("arrangements", "spår i playlisten har flyttats i %s" % p.who())
    for n in (have[1] & have[2]) - have[0]:
        keys = {c.key for p in (mine, theirs) for c in p.chunks
                if c.key[0] in ("arrangement", "playlist", "markers", "track") and c.key[1] == n}
        if not all(_same(mine.get(k), theirs.get(k)) for k in keys):
            raise MergeRefused("arrangements", "ni har båda lagt till ett arrangemang")


def _track_labels(p, arrangement):
    return {c.key[2]: _track_name(c) or None for c in p.kind("track") if c.key[1] == arrangement}
