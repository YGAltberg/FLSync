"""Reads and rewrites FL Studio project files (.flp), for snapshots.

When someone saves, the bridge reads the project for the samples it needs and
the plugins it uses (docs/protocol_v2.md, Snapshots); a receiver points the
project's sample paths at its own copies with rewrite_paths(). Standard library
only; PyFLP's source (github.com/demberto/PyFLP) was the map.

The format (FL 10 to FL 2026):

    "FLhd"  uint32 6, int16 format, uint16 channel count, uint16 PPQ
    "FLdt"  uint32 length, then the events (to the end of the file)

An event is an id byte and a value: ids 0-63 have 1 byte, 64-127 2 bytes,
128-191 4 bytes (little-endian), and 192-255 a length (7 bits per byte, lowest
group first, high bit = another byte follows) and that many bytes. One
exception: 172 has 3 bytes. Newer FL writes it near the start, followed by a
192 text like "FL Studio 25.2.3.5164.5164" (FL's bundled projects have it from
25.2.3 on); read as 4 bytes, the events after it are out of step (PyFLP pull
request 208).

The events used here:

    199  FL version, ASCII ("25.1.99.5087"), always the first event
    194  project title
     64  a new channel (value: its index). The channel's own events follow, up
         to the next 64 or the playlist and the mixer (SECTIONS), which come
         after the channels
    192  a channel's name in projects from before FL 12
    203  a channel's name from FL 12 on (before that: its plugin's preset
         name); also a mixer effect's name
    196  sample path, of a sampler channel, an audio clip or a plugin that plays
         the channel's sample (Fruity Granulizer). The only event outside
         plugin states that holds a file to ship
    201  internal plugin name ("FLEX", "Fruity Wrapper"; "" for a sampler). The
         first one after a 64 is the channel's generator; the ones after the
         channels are mixer effects
    202  project data folder (Options > Project general settings). FL asks
         what to do when it doesn't exist, so a receiver clears it
    213  plugin state. Fruity Wrapper, FL's host for VST/VST3/CLAP plugins, keeps
         a uint32 kind and then records (uint32 id, uint64 size, bytes) there;
         record 54 is the hosted plugin's own name (UTF-8)

Text is UTF-16LE ending in a NUL in projects saved by FL 11.5 and later, and
8-bit text (Windows-1252) ending in a NUL in older ones. The version event
decides; before it (or without one) a text of even length that ends in two NUL
bytes counts as UTF-16. All 161 projects bundled with FL 2025 (saved by FL 10.9
to 25.2) follow this. A text event that isn't one NUL-terminated string raises
FlpError: it means the events were read out of step (an event size this module
doesn't know yet), and a rewrite would damage the file.

Sample paths are absolute ("C:\\Users\\Anna\\Splice\\x.wav") or start with a
folder placeholder, see resolve(). FACTORY_PREFIXES are FL's own folders: their
samples exist on every FL and aren't sent.

Samples inside plugin states (213): FPC, DirectWave, Slicex, Fruity Slicer,
Fruity Convolver (impulses), Fruity Scratcher, Wave Traveller, GMS, BassDrum,
Harmor and VSTs keep the paths of their samples there, each plugin in its own
layout. parse() lists them as `plugin_samples` by looking for text that reads
as an absolute path (drive, UNC or placeholder) ending in an audio extension,
in UTF-16LE (FPC) and in 8-bit text (UTF-8, else Windows-1252: the others),
see _plugin_paths(). Those paths can't be rewritten (the plugin's own lengths
and checksums are around them): a receiver puts the files next to the .flp,
where FL looks for a missing sample by its name. Measured on the 161 projects
bundled with FL 2025: 2466 such paths in 50 projects, all FL's own but 12
(DirectWave in a demo, the author's D:\\ drive); the GMS and BassDrum ones
point into the authors' FL folders ("...\\FL Studio 11\\Plugins\\Fruity\\..."),
which counts as FL's own too.

Not covered: plugins hosted inside Patcher (they are part of Patcher's state,
so they aren't in `plugins` or `channels`), and states that are compressed or
encrypted (their paths can't be seen).
"""

import itertools
import ntpath
import os
import re
from dataclasses import dataclass

TEXT = 192          # ids from here on have a length and bytes
NEW_CHANNEL = 64
OLD_NAME = 192
TITLE = 194
SAMPLE_PATH = 196
VERSION = 199
PLUGIN_NAME = 201
DATA_PATH = 202
NAME = 203
PLUGIN_STATE = 213
TEXTS = (TITLE, SAMPLE_PATH, PLUGIN_NAME, DATA_PATH)  # the text events read here
NAMES = (OLD_NAME, NAME)  # read too, but leniently: a name never stops a project from being read
# The playlist and the mixer, which come after the channels: slot index, new
# arrangement, insert output and input, insert name, mixer parameters, routing,
# playlist items, insert flags, track data. No channel's events are among them.
SECTIONS = frozenset((98, 99, 147, 154, 204, 225, 227, 233, 236, 238))
MIXER = frozenset((98, 236))  # an insert's first event, a slot's last: the plugins after them are effects
MARKS = frozenset(e for e in SECTIONS | {NEW_CHANNEL} if e < TEXT)  # fixed-size events read here

FIXED_SIZE = [1] * 64 + [2] * 64 + [4] * 64  # value size of the ids below TEXT
FIXED_SIZE[172] = 3

UNICODE_SINCE = (11, 5)   # FL stores text as UTF-16 from this version on
WRAPPER = "fruity wrapper"
WRAPPED_NAME = 54         # record in Fruity Wrapper's state
FACTORY_PREFIXES = ("%flstudiofactorydata%", "%flstudiodata%")
# A path into some FL's own folders ("C:\\Program Files\\Image-Line\\FL Studio 11\\
# Plugins\\Fruity\\Generators\\GMS\\..."): plugins keep such paths, and the files
# come with every FL.
FL_FOLDER = re.compile(r"[\\/]fl studio[^\\/]*[\\/](?:data[\\/]patches|plugins[\\/]fruity)[\\/]", re.I)

# Samples in plugin states: an audio extension (found fast), then the path's
# start looked for in the text before it.
AUDIO = ("wav", "mp3", "ogg", "flac", "aif", "aiff", "aifc", "wv", "m4a", "opus", "aac", "wma", "rx2", "rex",
         "sf2")
_EXT8 = re.compile(rb"\.(?:" + b"|".join(a.encode() for a in AUDIO) + rb")(?![A-Za-z0-9_.\\/-])", re.I)
_EXT16 = re.compile(rb"\.\x00(?:" + b"|".join(b"".join(bytes([c]) + b"\x00" for c in a.encode()) for a in AUDIO)
                    + rb")(?![A-Za-z0-9_.\\/-]\x00)", re.I)
_PATH_CHARS = re.compile(r"[^\x00-\x1f\"*?<>|:]*")  # what a path may hold after its drive
_ROOT = re.compile(r"%[A-Za-z][A-Za-z0-9_]{0,39}%|\\\\[^\x00-\x20\\/\"*?<>|:]+[\\/]")  # placeholder or \\server\
MAX_PATH = 1024           # characters looked at before an extension for the path's start
MAX_HITS = 10000          # audio extensions looked at per plugin state: FL's projects have under 1000; a
                          # state crafted to be slow to read (each hit reads MAX_PATH back) stops there

# The placeholders in stored paths: which resolve() argument they stand for,
# and the subfolder of it.
PLACEHOLDERS = {
    "%flstudiofactorydata%": ("factory_dir", ""),
    "%flstudiodata%": ("factory_dir", "Data"),  # FL 12 and earlier
    "%flstudiouserdata%": ("user_dir", ""),
    "%flstudiodownloadsdata%": ("downloads_dir", ""),
    "%userprofile%": ("home_dir", ""),          # FL 11 to 20
}


class FlpError(ValueError):
    """Not an FL Studio project, or a truncated or damaged one."""


@dataclass(frozen=True)
class SampleRef:
    path: str      # the path text exactly as stored in the .flp
    factory: bool  # under FL's own folders: exists on every FL, never sent


@dataclass(frozen=True)
class Channel:
    index: int     # FL's global channel index, from 0
    name: str      # as FL shows it in the channel rack
    plugin: str    # internal plugin name, the hosted plugin's own for a VST; '' for samplers and audio clips


@dataclass(frozen=True)
class Project:
    version: str       # FL version that saved it (event 199), '' if absent
    ppq: int           # from the FLhd header
    title: str         # event 194, '' if absent
    samples: tuple     # SampleRef for every distinct sample path, in file order
    plugins: tuple     # distinct plugin names, in file order (see parse())
    data_path: str = ""  # project data folder (event 202), '' if none
    channels: tuple = ()        # Channel for every channel, in FL's order
    plugin_samples: tuple = ()  # SampleRef for every distinct audio file named in a plugin's state
    effects: tuple = ()         # distinct plugin names in mixer slots, in file order, spelled as in `plugins`


def parse(data):
    """Reads a project from the bytes of an .flp. Raises FlpError if they aren't
    a whole one.

    `plugins` holds each non-empty internal plugin name (event 201) once, the
    first spelling of names that differ only in case. For Fruity Wrapper it's
    the hosted plugin's own name ("Serum"), so missing VSTs can be found, or
    "Fruity Wrapper" when its state doesn't say.

    `channels` has a Channel per channel (event 64), in FL's global order. Its
    plugin is spelled as in `plugins`. Its name is the one the user gave it
    (192 in projects from before FL 12, else 203), or what FL shows without
    one: the plugin's name, or the sample's file name. `effects` are the
    plugins in mixer slots (after the mixer's first insert). In each of the
    161 projects bundled with FL 2025, every plugin is on a channel or in a
    mixer slot.

    `plugin_samples` has the audio files named in plugin states (213), see
    _plugin_paths(), each once (paths that differ only in case or slashes are
    one), `factory` for those in FL's own folders."""
    data = bytes(data)
    ppq, _, start, end = _chunk(data)
    version = title = data_path = ""
    samples, plugins, plugin_samples, effects = {}, {}, {}, {}
    channels = []   # [name, old name, plugin, sample] of each channel
    channel = None  # the channel whose events these are, until the playlist or the mixer
    mixer = False   # whether the mixer's inserts have begun
    wrapper = None  # (name, channel, in the mixer) of a Fruity Wrapper waiting for its state, which names its plugin

    def add_plugin(name, owner, effect):
        if name:
            name = plugins.setdefault(name.lower(), name)
            if effect:
                effects.setdefault(name.lower(), name)
        if owner is not None:
            owner[2] = name

    for eid, _, vstart, vend, text, _ in _read(data, start, end):
        if eid == NEW_CHANNEL:
            channel = [None, None, None, None]
            channels.append(channel)
        elif eid in SECTIONS:
            channel = None
            mixer = mixer or eid in MIXER
        elif eid == VERSION:
            version = text
        elif eid == TITLE:
            title = text
        elif eid == DATA_PATH:
            data_path = text
        elif eid == SAMPLE_PATH:
            if text and text not in samples:
                samples[text] = SampleRef(text, text.lower().startswith(FACTORY_PREFIXES))
            if channel is not None and channel[3] is None:
                channel[3] = text
        elif eid in NAMES:
            slot = 1 if eid == OLD_NAME else 0
            if channel is not None and channel[slot] is None:
                channel[slot] = text
        elif eid == PLUGIN_NAME:
            if wrapper is not None:
                add_plugin(*wrapper)  # the previous wrapper's state never came
            owner = channel if channel is not None and channel[2] is None else None  # the generator
            if owner is not None:
                owner[2] = ""
            effect = channel is None and mixer
            wrapper = (text, owner, effect) if text.lower() == WRAPPER else None
            if wrapper is None:
                add_plugin(text, owner, effect)
        elif eid == PLUGIN_STATE:
            state = data[vstart:vend]
            for path in _plugin_paths(state):
                plugin_samples.setdefault(ntpath.normcase(path), SampleRef(path, is_factory(path)))
            if wrapper is not None:
                add_plugin(_wrapped_name(state) or wrapper[0], wrapper[1], wrapper[2])
                wrapper = None
    if wrapper is not None:
        add_plugin(*wrapper)
    rack = []
    for index, (name, old, plugin, sample) in enumerate(channels):
        shown = old or name or plugin or (ntpath.splitext(ntpath.basename(sample))[0] if sample else "")
        rack.append(Channel(index, shown, plugin or ""))
    return Project(version, ppq, title, tuple(samples.values()), tuple(plugins.values()), data_path,
                   tuple(rack), tuple(plugin_samples.values()), tuple(effects.values()))


def is_factory(path):
    """Whether a sample path is in FL's own folders: a placeholder for them, or
    an absolute path into some FL's Data\\Patches or Plugins\\Fruity (plugins
    keep such paths). Those files come with every FL."""
    return path.lower().startswith(FACTORY_PREFIXES) or FL_FOLDER.search(path) is not None


def rewrite_paths(data, mapping, data_path=None):
    """A copy of the .flp in which every sample path (event 196) whose text is a
    key of `mapping` has the mapped text instead. Unknown keys are ignored.

    With `data_path` the project data folder (an existing event 202) becomes
    that text: '' clears it, so the receiver's FL doesn't ask about a folder
    that only the sharer has. The FLdt length is fixed up; every other byte
    stays as it was. Raises FlpError for a damaged file, or a path that the
    file's text encoding can't hold (only in projects from before FL 11.5)."""
    data = bytes(data)
    _, length_at, start, end = _chunk(data)
    parts, copied, grown = [], 0, 0
    for eid, at, _, vend, text, wide in _read(data, start, end):
        if eid == SAMPLE_PATH:
            new = mapping.get(text)
        elif eid == DATA_PATH:
            new = data_path
        else:
            continue
        if new is None or new == text:
            continue
        value = _encode(new, wide)
        event = bytes([eid]) + _varint(len(value)) + value
        parts += [data[copied:at], event]
        grown += len(event) - (vend - at)
        copied = vend
    if not parts:
        return data
    length = end - start + grown
    if length > 0xFFFFFFFF:
        raise FlpError("projektet blir för stort")
    parts.append(data[copied:])
    out = bytearray().join(parts)
    out[length_at:length_at + 4] = length.to_bytes(4, "little")
    return bytes(out)


def resolve(path, factory_dir=None, user_dir=None, downloads_dir=None, home_dir=None):
    """The local file that a stored sample path refers to, or None if that
    can't be told. Placeholders (any case) are replaced:

        %FLStudioFactoryData%    factory_dir: FL's install folder ("...\\FL Studio 2025")
        %FLStudioData%           factory_dir\\Data (FL 12 and earlier)
        %FLStudioUserData%       user_dir: FL's user data folder (File settings;
                                 by default <Documents>\\Image-Line)
        %FLStudioDownloadsData%  downloads_dir: FL's downloads folder
        %USERPROFILE%            home_dir, by default the current user's (FL 11 to 20)

    None when the folder a placeholder needs isn't given, for other
    placeholders, and for relative paths. Absolute paths (drive or UNC) stay
    as they are. The result is normalised for Windows (backslashes, no
    doubled separators or '..'), on any OS."""
    found = re.match(r"%[^%\\/]*%", path)
    if found:
        where = PLACEHOLDERS.get(found.group(0).lower())
        if where is None:
            return None
        base = {"factory_dir": factory_dir, "user_dir": user_dir, "downloads_dir": downloads_dir,
                "home_dir": home_dir or os.path.expanduser("~")}[where[0]]
        if not base:
            return None
        return ntpath.normpath("\\".join((base, where[1], path[found.end():])))
    drive, rest = ntpath.splitdrive(path)
    if drive and rest[:1] in ("\\", "/"):
        return ntpath.normpath(path)
    return None


def installed_plugins(plugin_db_dir):
    """Lower-cased names of the plugins in FL's plugin database, or an empty
    set if the folder doesn't exist. Pass its Installed folder:

        <Documents>\\Image-Line\\FL Studio\\Presets\\Plugin database\\Installed\\
            Generators\\ and Effects\\, each with Fruity\\, VST\\, VST3\\, CLAP\\ ...
                <name>.fst and <name>.nfo for each plugin FL can load

    The names are the file names and, from the .nfo, ps_name and
    ps_file_name_<n> (the plugin's own name in each format, which can hold
    characters a file name can't, like "Mono/Stereo"). An .nfo without an
    .fst is a file FL scanned but can't use, and doesn't count.

    FL's own plugins (the Fruity folders) come with every FL; native_plugins()
    lists them from FL's install folder, which also has some that the
    database leaves out (Wasp)."""
    names = set()
    for folder, _, files in os.walk(plugin_db_dir):
        presets = {f[:-4].lower() for f in files if f.lower().endswith(".fst")}
        names |= presets
        for f in files:
            if f.lower().endswith(".nfo") and f[:-4].lower() in presets:
                names |= _nfo_names(os.path.join(folder, f))
    names.discard("")
    return names


def native_plugins(factory_dir):
    """Lower-cased names of FL's own plugins: the folders in
    <factory_dir>\\Plugins\\Fruity\\Generators and \\Effects (empty set if missing)."""
    names = set()
    for kind in ("Generators", "Effects"):
        try:
            entries = list(os.scandir(os.path.join(factory_dir, "Plugins", "Fruity", kind)))
        except OSError:
            continue
        names.update(e.name.lower() for e in entries if e.is_dir())
    return names


# --- the format ------------------------------------------------------------------

def _chunk(data):
    """(PPQ, offset of the FLdt length, first event, end of the events)."""
    if len(data) < 14 or data[:4] != b"FLhd":
        raise FlpError("inte en FL Studio-projektfil")
    header = int.from_bytes(data[4:8], "little")
    if header < 6:
        raise FlpError("trasig projektfil (för kort FLhd)")
    at = 8 + header
    if len(data) < at + 8:
        raise FlpError("projektfilen är avkortad")
    if data[at:at + 4] != b"FLdt":
        raise FlpError("trasig projektfil (FLdt saknas)")
    start = at + 8
    end = start + int.from_bytes(data[at + 4:start], "little")
    if end > len(data):
        raise FlpError("projektfilen är avkortad")
    return int.from_bytes(data[12:14], "little"), at + 4, start, end


def _events(data, at, end, fixed=frozenset()):
    """(id, start, value start, value end) of each event with a length and bytes
    in data[at:end], and of the fixed-size ones whose id is in `fixed`; checks
    every event on the way."""
    while at < end:
        eid = data[at]
        vstart = at + 1
        if eid < TEXT:
            vend = vstart + FIXED_SIZE[eid]
        else:
            size = shift = 0
            while True:
                if vstart >= end or shift > 28:
                    raise FlpError("trasig eller avkortad projektfil (byte %d)" % at)
                byte = data[vstart]
                vstart += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            vend = vstart + size
        if vend > end:
            raise FlpError("trasig eller avkortad projektfil (byte %d)" % at)
        if eid >= TEXT or eid in fixed:
            yield eid, at, vstart, vend
        at = vend


def _read(data, start, end):
    """(id, start, value start, value end, text, whether UTF-16) of the events
    parse() and rewrite_paths() use: the version (as text), TEXTS and NAMES
    (decoded), and new channels, SECTIONS and plugin states (text None)."""
    unicode = None
    for eid, at, vstart, vend in _events(data, start, end, MARKS):
        if eid == VERSION:
            version = data[vstart:vend].decode("ascii", "replace").split("\0", 1)[0]
            unicode = _unicode(version)
            yield eid, at, vstart, vend, version, None
        elif eid in TEXTS:
            yield (eid, at, vstart, vend) + _text(data[vstart:vend], unicode, at)
        elif eid in NAMES:
            yield (eid, at, vstart, vend) + _name(data[vstart:vend], unicode)
        elif eid == PLUGIN_STATE or eid == NEW_CHANNEL or eid in SECTIONS:
            yield eid, at, vstart, vend, None, None


def _unicode(version):
    """True if FL `version` stores text as UTF-16, None if it can't be read."""
    found = re.match(r"(\d{1,9})\.(\d{1,9})", version)  # a giant number is no version (int() would refuse it)
    return None if found is None else (int(found.group(1)), int(found.group(2))) >= UNICODE_SINCE


def _text(value, unicode, at):
    """(text, whether it was UTF-16) of the text event at byte `at`. `unicode`
    None: tell from the bytes."""
    wide = unicode if unicode is not None else len(value) % 2 == 0 and value.endswith(b"\0\0")
    if not wide:
        try:
            text = value.decode("cp1252")
        except UnicodeDecodeError:  # a byte cp1252 leaves undefined
            text = value.decode("latin-1")
    elif len(value) % 2 == 0:
        text = value.decode("utf-16-le", "surrogatepass")
    else:
        text = ""
    if text[-1:] != "\0" or "\0" in text[:-1]:
        raise FlpError("trasig projektfil (text vid byte %d)" % at)
    return text[:-1], wide


def _name(value, unicode):
    """(text up to its first NUL, whether it was UTF-16) of a name event, which
    never raises: names are only shown."""
    wide = unicode if unicode is not None else len(value) % 2 == 0 and value.endswith(b"\0\0")
    if wide:
        text = value[:len(value) // 2 * 2].decode("utf-16-le", "replace")
    else:
        text = value.decode("cp1252", "replace")
    return text.split("\0", 1)[0], wide


def _plugin_paths(state):
    """The absolute paths of audio files in a plugin's state: those in UTF-16LE
    text (FPC) first, then those in 8-bit text (UTF-8, else Windows-1252).

    Plugins keep a path among binary fields: a length in front of it (often a
    byte that reads as a letter), and after it no NUL but the next field. So a
    path is found from its extension backwards: the path characters before an
    extension, to a drive ("C:\\"), a placeholder ("%FLStudioFactoryData%")
    or a server ("\\\\nas\\"). A bare file name doesn't count (it can't be found),
    nor does a macOS path. After the extension may come anything but what would
    go on a name ("x.wav.asd" is no audio file)."""
    found = []
    for pattern, width, encoding in ((_EXT16, 2, "utf-16-le"), (_EXT8, 1, "latin-1")):
        done = 0  # where the previous path ended: the next one starts after it
        for hit in itertools.islice(pattern.finditer(state), MAX_HITS):
            begin = hit.start()
            if begin < done:
                continue
            first = max(done, begin - MAX_PATH * width)
            first += (begin - first) % width  # in step with the extension's characters
            before = state[first:begin].decode(encoding, "replace")
            body = len(before) - _PATH_CHARS.match(before[::-1]).end()  # where the path characters start
            head = before[:body]
            if body < len(before) and before[body] in "\\/" and head[-1:] == ":" and head[-2:-1].isalpha() \
                    and head[-2:-1].isascii():
                start = body - 2  # a drive
            else:
                root = _ROOT.search(before, body)
                if root is None:
                    continue
                start = root.start()
            if width == 2:
                path = before[start:] + state[begin:hit.end()].decode(encoding)
            else:
                path = _decode8(state[first + start:hit.end()])
            found.append(path)
            done = hit.end()
    return found


def _decode8(raw):
    """8-bit text from a plugin's state: UTF-8 (FL 2025 writes it), else Windows-1252."""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _encode(text, wide):
    if not isinstance(text, str):
        raise TypeError("a path must be a str, not %s" % type(text).__name__)
    if "\0" in text:
        raise FlpError("en sökväg kan inte innehålla NUL")
    if wide:
        return (text + "\0").encode("utf-16-le", "surrogatepass")
    try:
        return (text + "\0").encode("cp1252")
    except UnicodeEncodeError:
        raise FlpError("%s går inte att spara i ett projekt från före FL 11.5" % text) from None


def _varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _wrapped_name(state):
    """The hosted plugin's own name from Fruity Wrapper's state, '' if the
    state isn't laid out as expected."""
    name, at = "", 4
    while at < len(state):
        if at + 12 > len(state):
            return ""
        end = at + 12 + int.from_bytes(state[at + 4:at + 12], "little")
        if end > len(state):
            return ""
        if int.from_bytes(state[at:at + 4], "little") == WRAPPED_NAME and not name:
            name = state[at + 12:end].decode("utf-8", "replace").split("\0", 1)[0].strip()
        at = end
    return name


def _nfo_names(path):
    """ps_name and every ps_file_name_<n> in a plugin database .nfo, lower-cased."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    names = set()
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and (key == "ps_name" or key.startswith("ps_file_name_")):
            names.add(value.strip().lower())
    return names
