"""bridge/flp.py: reading FL Studio projects and pointing their samples elsewhere.

Small projects built here test the format itself. When FL Studio 2025 is
installed, the projects bundled with it are read in place (never copied):
every one is parsed and rewritten.

    python -m unittest discover -s tests -p "test_flp.py" -v
"""

import glob
import ntpath
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import flp  # noqa: E402

FL_DIR = os.environ.get("FLSYNC_FL_DIR", r"C:\Program Files (x86)\Image-Line\FL Studio 2025")
BUNDLED = os.path.join(FL_DIR, "Data")

KICK = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Kicks\Kick.wav"
VOX = r"C:\Users\Anna\Splice\Vox 01.wav"
ANNAS_FOLDER = r"C:\Users\Anna\Music\Min låt"
NEW_VOX = r"C:\Users\Bo\Documents\Image-Line\FL Studio\Projects\FLSync\Min låt\Samples\Vox 01.wav"


# --- building projects -----------------------------------------------------------

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
    """One event. A str value is stored as UTF-16 text, like FL 11.5 and later do."""
    if eid < 64:
        return bytes([eid, value])
    if eid < 128:
        return bytes([eid]) + value.to_bytes(2, "little")
    if eid < 192:
        return bytes([eid]) + value.to_bytes(4, "little")
    if isinstance(value, str):
        value = wide(value)
    return bytes([eid]) + varint(len(value)) + value


def raw(body, ppq=96):
    """FLhd and FLdt around `body`, which must be whole events."""
    return (b"FLhd" + (6).to_bytes(4, "little") + bytes(2) + (5).to_bytes(2, "little")
            + ppq.to_bytes(2, "little") + b"FLdt" + len(body).to_bytes(4, "little") + body)


def project(*events, version="25.1.99.5087", ppq=96):
    head = ev(199, (version + "\0").encode("ascii")) if version is not None else b""
    return raw(head + b"".join(events), ppq)


def wrapper_state(name):
    """Fruity Wrapper's state as FL keeps it for a VST: a kind, then records of
    id, size and bytes; 54 is the plugin's name."""
    records = [(1, bytes(20)), (51, b"XfsS"), (54, name.encode("utf-8")), (55, b"C:\\VST\\Serum_x64.dll")]
    return (8).to_bytes(4, "little") + b"".join(
        rid.to_bytes(4, "little") + len(data).to_bytes(8, "little") + data for rid, data in records)


def song(kick=KICK, vox=VOX, folder=ANNAS_FOLDER):
    """Two samplers, an audio clip, a VST and FL's own plugins."""
    return project(
        ev(194, "Min låt"), ev(202, folder),
        ev(64, 0), ev(21, 0), ev(201, ""), ev(212, bytes(52)), ev(203, "Kick"), ev(196, kick),
        ev(64, 1), ev(21, 4), ev(201, ""), ev(203, "Vox"), ev(196, vox), ev(170, 0xFFFFFFFF),
        ev(64, 2), ev(21, 2), ev(201, "Fruity Wrapper"), ev(212, bytes(52)), ev(203, "Lead"),
        ev(213, wrapper_state("Serum")),
        ev(64, 3), ev(21, 2), ev(201, "Sytrus"), ev(213, b"\x01\x02\x03"),
        ev(64, 4), ev(21, 0), ev(201, ""), ev(196, vox),  # the same sample in a second channel
        ev(201, "Fruity Limiter"), ev(213, bytes(40)), ev(201, "fruity limiter"),
        ev(204, "Insert 1"), ev(203, VOX),  # a name that happens to read like the path
    )


def path_of(length, ending="x.wav"):
    """An absolute path exactly `length` characters long."""
    return "C:\\" + "a" * (length - 3 - len(ending)) + ending


def walk(data):
    """(id, value) of every event, read without flp.py (FLhd of length 6)."""
    assert data[14:18] == b"FLdt" and int.from_bytes(data[18:22], "little") == len(data) - 22
    at, events = 22, []
    while at < len(data):
        eid = data[at]
        at += 1
        if eid == 172:
            size = 3
        elif eid < 192:
            size = (1, 2, 4)[eid // 64]
        else:
            size = shift = 0
            while True:
                byte = data[at]
                at += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
        events.append((eid, data[at:at + size]))
        at += size
    assert at == len(data)
    return events


# --- reading ------------------------------------------------------------------

class ParseTest(unittest.TestCase):
    def test_header_and_project_info(self):
        p = flp.parse(song())
        self.assertEqual((p.version, p.ppq, p.title, p.data_path), ("25.1.99.5087", 96, "Min låt", ANNAS_FOLDER))
        self.assertEqual(flp.parse(project(ppq=960)).ppq, 960)
        self.assertEqual(flp.parse(bytearray(song())), p)

    def test_each_sample_once_in_file_order(self):
        self.assertEqual(flp.parse(song()).samples, (flp.SampleRef(KICK, True), flp.SampleRef(VOX, False)))
        two = project(ev(196, VOX), ev(196, VOX.upper()), ev(196, ""), ev(196, KICK))
        self.assertEqual([s.path for s in flp.parse(two).samples], [VOX, VOX.upper(), KICK])

    def test_factory_samples(self):
        for path, factory in [
                (KICK, True),
                (r"%flstudiofactorydata%/data/patches/kick.wav", True),
                (r"%FLStudioData%\Patches\Packs\Drums\Kick.wav", True),  # FL 12 and earlier: <FL>\Data
                (r"%FLStudioUserData%\FL Studio\Audio\Recorded\Take 1.wav", False),
                (r"%FLStudioDownloadsData%\Packs\Loop.wav", False),
                (r"%USERPROFILE%\Documents\Image-Line\Data\FL Studio\Loop.wav", False),
                (VOX, False),
                (r"\\nas\samples\FLStudioFactoryData.wav", False)]:
            with self.subTest(path):
                self.assertEqual(flp.parse(project(ev(196, path))).samples, (flp.SampleRef(path, factory),))

    def test_plugins_once_with_the_vst_by_its_own_name(self):
        self.assertEqual(flp.parse(song()).plugins, ("Serum", "Sytrus", "Fruity Limiter"))

    def test_a_wrapper_whose_state_says_nothing_is_listed_as_the_wrapper(self):
        broken = (8).to_bytes(4, "little") + (54).to_bytes(4, "little") + (100).to_bytes(8, "little") + b"Ser"
        for events, plugins in [
                ([ev(201, "Fruity Wrapper"), ev(201, "FLEX")], ("Fruity Wrapper", "FLEX")),
                ([ev(201, "Fruity Wrapper"), ev(213, broken)], ("Fruity Wrapper",)),
                ([ev(201, "Fruity Wrapper"), ev(213, b"")], ("Fruity Wrapper",)),
                ([ev(201, "Fruity Wrapper"), ev(213, bytes(4))], ("Fruity Wrapper",)),
                ([ev(201, "Fruity Wrapper")], ("Fruity Wrapper",)),
                ([ev(201, "Sytrus"), ev(213, wrapper_state("Serum"))], ("Sytrus",))]:
            with self.subTest(plugins):
                self.assertEqual(flp.parse(project(*events)).plugins, plugins)

    def test_projects_from_before_fl_11_5_have_8bit_text(self):
        path = r"%FLStudioData%\Patches\Misc\Noël’s Kick.wav"  # ’ is 0x92 in Windows-1252
        old = project(ev(194, "Låt\0".encode("cp1252")), ev(196, (path + "\0").encode("cp1252")),
                      ev(201, b"Harmor\0"), ev(203, b"Bas\0"), version="11.1.1")
        p = flp.parse(old)
        self.assertEqual((p.version, p.title, p.samples, p.plugins),
                         ("11.1.1", "Låt", (flp.SampleRef(path, True),), ("Harmor",)))
        self.assertEqual(flp.parse(project(ev(196, b"C:\\\x81.wav\0"), version="10.9.0")).samples[0].path,
                         "C:\\\x81.wav")  # a byte Windows-1252 leaves undefined

    def test_without_a_version_the_bytes_tell_the_text_encoding(self):
        p = flp.parse(project(ev(196, wide(VOX)), ev(201, b"Sytrus\0"), ev(194, b"\0"), version=None))
        self.assertEqual((p.version, p.samples, p.plugins, p.title), ("", (flp.SampleRef(VOX, False),), ("Sytrus",), ""))

    def test_the_version_decides_the_text_encoding(self):
        for version, value in [("11.5.0", wide("Sytrus")), ("11.4.9", b"Sytrus\0")]:
            with self.subTest(version):
                self.assertEqual(flp.parse(project(ev(201, value), version=version)).plugins, ("Sytrus",))
        # text in the other encoding means the events were read out of step
        for version, value in [("25.2.3.5164", b"Sytrus\0"), ("11.5.0", b"Sytrus\0"), ("11.1.1", wide("Sytrus"))]:
            with self.subTest(version):
                with self.assertRaises(flp.FlpError):
                    flp.parse(project(ev(201, value), version=version))

    def test_a_text_must_be_one_nul_terminated_string(self):
        for value in [wide(VOX) + wide("skräp"), wide(VOX)[:-2], wide(VOX) + b"\0", b"", b"\0"]:
            with self.subTest(value[-6:]):
                data = project(ev(196, value))
                with self.assertRaises(flp.FlpError):
                    flp.parse(data)
                with self.assertRaises(flp.FlpError):
                    flp.rewrite_paths(data, {})

    def test_event_172_has_3_bytes(self):
        # FL 25.2.3 and later write it after the version, then a text 192
        new_fl = (b"\xac\x01\x01\x00", ev(192, "FL Studio 25.2.4.5242.5242"))
        p = flp.parse(project(*new_fl, ev(194, "Min låt"), ev(196, VOX), version="25.2.4.5242"))
        self.assertEqual((p.title, p.samples), ("Min låt", (flp.SampleRef(VOX, False),)))
        self.assertEqual(flp.rewrite_paths(project(*new_fl, ev(196, VOX), ev(203, "Vox")), {VOX: NEW_VOX}),
                         project(*new_fl, ev(196, NEW_VOX), ev(203, "Vox")))

    def test_an_empty_project(self):
        self.assertEqual(flp.parse(project(version=None)), flp.Project("", 96, "", (), (), ""))
        self.assertEqual(flp.parse(project()).version, "25.1.99.5087")

    def test_bytes_after_the_data_chunk_are_ignored(self):
        self.assertEqual(flp.parse(song() + b"\x00\xff"), flp.parse(song()))


# --- channels -------------------------------------------------------------------

def rack(*mixer):
    """Channels as FL 2025 writes them, then the playlist and `mixer` (events)."""
    return project(
        ev(64, 0), ev(21, 0), ev(201, ""), ev(212, bytes(52)), ev(203, "Kick"), ev(196, KICK),
        ev(64, 1), ev(21, 2), ev(201, "Fruity Wrapper"), ev(212, bytes(52)), ev(203, "Lead"),
        ev(213, wrapper_state("Serum")),
        ev(64, 2), ev(21, 2), ev(201, "Sytrus"), ev(213, b"\x01\x02"),  # no name of its own
        ev(64, 3), ev(21, 4), ev(201, ""), ev(196, VOX),  # an audio clip without a name
        ev(64, 4), ev(21, 5), ev(201, ""), ev(203, "Filter cutoff"),  # an automation clip
        ev(99, 0), ev(241, "Arrangement"), ev(233, b""),  # the playlist
        *mixer)


class ChannelTest(unittest.TestCase):
    MIXER = (ev(236, bytes(12)), ev(201, "Fruity Limiter"), ev(212, bytes(52)), ev(203, "Bus limiter"),
             ev(213, bytes(4)), ev(98, 0), ev(201, "Fruity Wrapper"), ev(213, wrapper_state("Pro-Q 3")),
             ev(98, 1), ev(204, "Drums"), ev(154, 0xFFFFFFFF), ev(147, 0))

    def test_every_channel_in_order_with_its_name_and_plugin(self):
        p = flp.parse(rack(*self.MIXER))
        self.assertEqual(p.channels, (flp.Channel(0, "Kick", ""), flp.Channel(1, "Lead", "Serum"),
                                      flp.Channel(2, "Sytrus", "Sytrus"),     # FL shows the plugin's name
                                      flp.Channel(3, "Vox 01", ""),           # ... or the sample's
                                      flp.Channel(4, "Filter cutoff", "")))
        self.assertEqual(p.plugins, ("Serum", "Sytrus", "Fruity Limiter", "Pro-Q 3"))
        self.assertEqual(p.effects, ("Fruity Limiter", "Pro-Q 3"))

    def test_a_channels_events_end_where_the_playlist_or_the_mixer_begins(self):
        for start in (ev(99, 0), ev(233, b""), ev(238, bytes(70)), ev(236, bytes(12)), ev(98, 0), ev(204, "Insert"),
                      ev(225, bytes(12)), ev(227, bytes(4)), ev(147, 0), ev(154, 0)):
            with self.subTest(start[0]):
                p = flp.parse(project(ev(64, 0), ev(21, 5), start, ev(201, "Maximus"), ev(203, "Master limiter"),
                                      ev(196, VOX)))
                self.assertEqual(p.channels, (flp.Channel(0, "", ""),))
                self.assertEqual(p.plugins, ("Maximus",))
                # a plugin is a mixer effect once the mixer's inserts have begun
                self.assertEqual(p.effects, ("Maximus",) if start[0] in (236, 98) else ())

    def test_a_channel_has_one_generator(self):
        # song(): the mixer's limiter follows the last channel without a marker in between
        p = flp.parse(song())
        self.assertEqual(p.channels,
                         (flp.Channel(0, "Kick", ""), flp.Channel(1, "Vox", ""), flp.Channel(2, "Lead", "Serum"),
                          flp.Channel(3, "Sytrus", "Sytrus"), flp.Channel(4, "Vox 01", "")))
        self.assertEqual(p.effects, ())  # can't be told

    def test_a_vst_whose_state_says_nothing_and_names_that_differ_in_case(self):
        p = flp.parse(project(ev(64, 0), ev(201, "Fruity Wrapper"), ev(64, 1), ev(201, "SYTRUS"),
                              ev(64, 2), ev(201, "Sytrus"), ev(213, b""), ev(236, bytes(12)), ev(201, "Fruity Wrapper"),
                              ev(201, "sytrus"), ev(98, 0)))
        self.assertEqual([c.plugin for c in p.channels], ["Fruity Wrapper", "SYTRUS", "SYTRUS"])
        self.assertEqual((p.plugins, p.effects), (("Fruity Wrapper", "SYTRUS"), ("Fruity Wrapper", "SYTRUS")))

    def test_projects_from_before_fl_12_name_channels_with_192(self):
        # 203 was the plugin's preset name then; 192 the name the user gave the channel
        old = project(ev(64, 0), ev(21, 2), ev(201, b"Harmless\0"), ev(203, b"Getta\0"), ev(192, b"Bass\0"),
                      ev(64, 1), ev(21, 4), ev(192, b"Intro\0"), ev(196, b"%FLStudioData%\\Intro.wav\0"),
                      version="11.0.3")
        self.assertEqual(flp.parse(old).channels, (flp.Channel(0, "Bass", "Harmless"), flp.Channel(1, "Intro", "")))

    def test_odd_names_never_stop_a_project_from_being_read(self):
        p = flp.parse(project(ev(64, 0), ev(203, wide("Kick") + wide("x")), ev(64, 1), ev(203, b"\x00\xd8K\x00\xff"),
                              ev(64, 2), ev(192, b"")))
        self.assertEqual(p.channels, (flp.Channel(0, "Kick", ""), flp.Channel(1, "\ufffdK", ""), flp.Channel(2, "", "")))

    def test_a_project_without_channels(self):
        self.assertEqual(flp.parse(project(*ChannelTest.MIXER)).channels, ())


# --- samples inside plugin states ------------------------------------------------

def records(*fields):
    """A plugin state the way DirectWave and FPC keep theirs: records of a uint32
    id, a uint64 length and bytes."""
    return b"".join(i.to_bytes(4, "little") + len(d).to_bytes(8, "little") + d for i, d in enumerate(fields, 500))


def utf8(text):
    return text.encode("utf-8")


USER_LOOP = r"C:\Users\Anna\Samples\Loops\Breakbeat 174.wav"
NOEL = r"D:\Ljud\Edlan & Ella Noël\Pad – lång.ogg"


class PluginSampleTest(unittest.TestCase):
    def found(self, *states):
        events = [ev(201, "DirectWave")] + [ev(213, s) for s in states]
        return [(s.path, s.factory) for s in flp.parse(project(*events)).plugin_samples]

    def test_8bit_paths_among_binary_fields(self):
        kick = r"%FLStudioFactoryData%\Data\Patches\Packs\Drums\Kicks\Kick.wav"
        harmor = r"%FLStudioFactoryData%\Data\Patches\Plugin presets\Generators\Harmor\Samples\Ooh C2.ogg"
        for state in [
                records(b"Close Grand 21", utf8(USER_LOOP), b"(\x00\x00\x00"),       # DirectWave
                b"\x04\x00\x00\x00=" + utf8(USER_LOOP) + b"\t\x00\x00\x00\x04Kick",   # Fruity Slicer: length byte '='
                b"\x00]" + utf8(USER_LOOP) + b"\xf2\x00\x00\x00",                    # Harmor: 0xF2 reads as 'ò'
                b"N\x00\x00\x00" + utf8(USER_LOOP) + b"\xd0\x07\x00\x00X",           # Wave Traveller
                b"4nyS" + utf8(USER_LOOP) + b"\x00\x00",                             # GMS: letters before the drive
                b"\x89" + utf8(USER_LOOP) + b" \x00\x00\x00\n1 - Beat 1"]:           # a space, then the next field
            with self.subTest(state[:8]):
                self.assertEqual(self.found(state), [(USER_LOOP, False)])
        self.assertEqual(self.found(b"\x01p" + utf8(kick) + b"\x00", b"]" + utf8(harmor) + b"|\xa4\x00"),
                         [(kick, True), (harmor, True)])

    def test_8bit_text_is_utf8_or_else_windows_1252(self):
        self.assertEqual(self.found(b"\x01" + utf8(NOEL) + b"\x00"), [(NOEL, False)])
        old = r"D:\Ljud\Café\Bas – sub.wav"  # not UTF-8 in Windows-1252
        self.assertEqual(self.found(b"\x01" + old.encode("cp1252") + b"\x00"), [(old, False)])

    def test_utf16_paths_as_fpc_keeps_them(self):
        snare = r"%FLStudioData%\Patches\Packs\Drums\Kits\HQ Rock Kit\snare side hq rock #1.wav"
        state = records(bytes(30), wide(snare)[:-2] + b"-\x01\x00\x00", wide(USER_LOOP), b"\x1c\x00")
        self.assertEqual(self.found(state), [(snare, True), (USER_LOOP, False)])
        self.assertEqual(self.found(b"\x00" + wide(NOEL)), [(NOEL, False)])  # at an odd offset

    def test_servers_and_placeholders(self):
        for path, factory in [(r"\\nas\ljud\Vox.flac", False),
                              (r"%FLStudioUserData%\FL Studio\Audio\Recorded\Take 1.wav", False),
                              (r"%USERPROFILE%\Music\Loop.aiff", False),
                              (r"%FLStudioData%\Patches\Misc\Kick.wv", True),
                              (r"C:\Program Files\Image-Line\FL Studio BETA\Plugins\Fruity\Generators\GMS\Synth Waves\Keys.wav", True),
                              (r"C:\Users\Kim\Desktop\FL Studio Install\Plugins\Fruity\Generators\BassDrum\Noise Samples\Hard Clap.wav", True),
                              (r"D:\Program Files (x86)\Image-Line\FL Studio 2025\Data\Patches\Packs\Loops\DL Breaker.wav", True),
                              (r"D:\Mina\FL Studio-projekt\Data\Loop.wav", False)]:
            with self.subTest(path):
                self.assertEqual(self.found(b"\x10" + utf8(path) + b"\x00"), [(path, factory)])
                self.assertEqual(self.found(b"\x10\x00" + wide(path)), [(path, factory)])

    def test_every_audio_extension(self):
        for ext in flp.AUDIO + ("WAV", "Mp3"):
            with self.subTest(ext):
                path = r"C:\Ljud\x." + ext
                self.assertEqual(self.found(b"\x00" + utf8(path) + b"\x00", b"\x00" + wide(path)), [(path, False)])

    def test_what_isnt_an_audio_file_with_an_absolute_path(self):
        for text in ["AmbientNoise_01.wav",                     # a bare name (Morphine): it can't be found
                     "Samples\\Loop.wav", "C:Loop.wav", "1:\\Loop.wav", ":\\Loop.wav",
                     "/Applications/FL Studio 20.app/Contents/Resources/FL/Plugins/Fruity/Generators/BassDrum/x.wav",
                     "C:\\VST\\Serum_x64.dll", "C:\\Ljud\\x.wav.asd", "C:\\Ljud\\x.wave", "C:\\Ljud\\x.wavx",
                     "C:\\Ljud\\x.wav_old", "C:\\Ljud\\x.wav-1", "C:\\Ljud\\x.mid", "C:\\Ljud\\.wav\\x",
                     "C:\\Ljud\\x?.wav", "C:\\Lj\x01ud\\x.wav", "%%\\x.wav", "%1%\\x.wav", "\\\\\\x.wav"]:
            with self.subTest(text):
                self.assertEqual(self.found(b"\x00" + text.encode("latin-1") + b"\x00", b"\x00" + wide(text)), [])

    def test_a_folder_named_like_an_audio_file(self):
        path = r"C:\Ljud\Loop.wav\Loop.wav"
        self.assertEqual(self.found(b"\x00" + utf8(path) + b"\x00", wide(path)), [(path, False)])

    def test_two_paths_in_a_row(self):
        other = r"E:\Ljud\Snare.mp3"
        self.assertEqual(self.found(b"\x01" + utf8(USER_LOOP) + b"\x91" + utf8(other) + b"\x00"),
                         [(USER_LOOP, False), (other, False)])

    def test_each_file_once(self):
        same = [USER_LOOP, USER_LOOP.upper(), USER_LOOP.replace("\\", "/")]
        self.assertEqual(self.found(*(b"\x00" + utf8(p) + b"\x00" for p in same), wide(USER_LOOP)),
                         [(USER_LOOP, False)])
        p = flp.parse(project(ev(196, USER_LOOP), ev(201, "Slicex"), ev(213, b"\x00" + utf8(USER_LOOP) + b"\x00")))
        self.assertEqual((p.samples, p.plugin_samples),  # in a channel and in a plugin: listed as both
                         ((flp.SampleRef(USER_LOOP, False),), (flp.SampleRef(USER_LOOP, False),)))

    def test_paths_inside_a_vst_state(self):
        chunk = b'<Sampler><Zone file="' + utf8(USER_LOOP) + b'" root="60"/><Zone file="C:\\Kits\\Hat.flac"/></Sampler>'
        state = wrapper_state("Sitala") + (56).to_bytes(4, "little") + len(chunk).to_bytes(8, "little") + chunk
        p = flp.parse(project(ev(64, 0), ev(201, "Fruity Wrapper"), ev(213, state)))
        self.assertEqual([s.path for s in p.plugin_samples], [USER_LOOP, r"C:\Kits\Hat.flac"])  # not its .dll
        self.assertEqual(p.channels, (flp.Channel(0, "Sitala", "Sitala"),))

    def test_escaped_backslashes_still_resolve(self):
        escaped = USER_LOOP.replace("\\", "\\\\")  # a JSON state
        self.assertEqual(self.found(b'{"file":"' + utf8(escaped) + b'"}'), [(escaped, False)])
        self.assertEqual(flp.resolve(escaped), USER_LOOP)

    def test_a_state_without_text(self):
        rng = random.Random(3)
        noise = bytes(rng.randrange(256) for _ in range(200000))
        self.assertEqual(self.found(noise, bytes(1000), b""), [])


# --- rewriting -----------------------------------------------------------------

class RewriteTest(unittest.TestCase):
    def test_nothing_to_change_gives_the_same_bytes(self):
        others = {"Sytrus": "Harmor", "Fruity Wrapper": "x", "Min låt": "x", ANNAS_FOLDER: "x", "Kick": "x"}
        for mapping in ({}, {VOX: VOX}, {r"C:\finns inte.wav": NEW_VOX}, others):
            with self.subTest(mapping):
                self.assertEqual(flp.rewrite_paths(song(), mapping), song())

    def test_only_the_sample_events_change(self):
        out = flp.rewrite_paths(song(), {VOX: NEW_VOX, r"C:\finns inte.wav": "x"})
        self.assertEqual(out, song(vox=NEW_VOX), "något annat än sökvägarna ändrades")
        self.assertEqual(int.from_bytes(out[18:22], "little"), len(out) - 22)
        self.assertEqual(flp.parse(out).samples, (flp.SampleRef(KICK, True), flp.SampleRef(NEW_VOX, False)))
        self.assertIn(ev(203, VOX), out)  # the name that reads like the path stays

    def test_several_paths_at_once(self):
        mapping = {KICK: r"D:\FLSync\Kick.wav", VOX: NEW_VOX}
        self.assertEqual(flp.rewrite_paths(song(), mapping), song(kick=mapping[KICK], vox=NEW_VOX))

    def test_lengths_across_varint_boundaries(self):
        length_bytes = {126: b"\x7e", 127: b"\x7f", 128: b"\x80\x01",
                        16382: b"\xfe\x7f", 16383: b"\xff\x7f", 16384: b"\x80\x80\x01"}
        # (version, its text encoding, {size of the event's value: path length that gives it})
        for version, encode, sizes in [
                ("25.1.99.5087", wide, {126: 62, 128: 63, 16382: 8190, 16384: 8191}),
                ("11.1.1", lambda t: (t + "\0").encode("cp1252"), {127: 126, 128: 127, 16383: 16382, 16384: 16383})]:
            small, big = sorted(sizes)[:2], sorted(sizes)[2:]
            for a, b in [small, small[::-1], big, big[::-1], (min(sizes), max(sizes)), (max(sizes), min(sizes))]:
                with self.subTest(version=version, sizes=(a, b)):
                    before, after = path_of(sizes[a], "före.wav"), path_of(sizes[b], "efter.wav")
                    build = lambda path: project(ev(203, encode("Kanal")), ev(196, encode(path)),
                                                 ev(203, encode("Efter")), version=version)
                    out = flp.rewrite_paths(build(before), {before: after})
                    self.assertEqual(out, build(after))
                    self.assertIn(bytes([196]) + length_bytes[b] + encode(after), out)
                    self.assertEqual(int.from_bytes(out[18:22], "little"), len(out) - 22)
                    self.assertEqual(flp.parse(out).samples[0].path, after)

    def test_projects_from_before_fl_11_5_get_8bit_paths(self):
        old_path = r"%FLStudioData%\Patches\Kick.wav"
        new_path = r"C:\Användare\Åsa\Ljud – bas.wav"  # – is 0x96 in Windows-1252
        build = lambda path: project(ev(196, (path + "\0").encode("cp1252")), version="11.1.1")
        self.assertEqual(flp.rewrite_paths(build(old_path), {old_path: new_path}), build(new_path))
        with self.assertRaises(flp.FlpError):
            flp.rewrite_paths(build(old_path), {old_path: "C:\\音楽\\x.wav"})

    def test_the_data_folder(self):
        self.assertEqual(flp.rewrite_paths(song(), {}, data_path=""), song(folder=""))
        self.assertEqual(flp.rewrite_paths(song(), {VOX: NEW_VOX}, data_path=r"D:\Bo\Min låt"),
                         song(vox=NEW_VOX, folder=r"D:\Bo\Min låt"))
        self.assertEqual(flp.rewrite_paths(song(), {}, data_path=ANNAS_FOLDER), song())
        without = project(ev(196, VOX))
        self.assertEqual(flp.rewrite_paths(without, {}, data_path=""), without)  # nothing is added

    def test_bytes_after_the_data_chunk_stay(self):
        self.assertEqual(flp.rewrite_paths(song() + b"\x00\xff", {VOX: NEW_VOX}), song(vox=NEW_VOX) + b"\x00\xff")

    def test_impossible_new_paths(self):
        with self.assertRaises(flp.FlpError):
            flp.rewrite_paths(song(), {VOX: "C:\\a\0b.wav"})
        with self.assertRaises(TypeError):
            flp.rewrite_paths(song(), {VOX: b"C:\\x.wav"})


# --- damaged files -----------------------------------------------------------------

class MalformedTest(unittest.TestCase):
    def assertRejected(self, data):
        with self.assertRaises(flp.FlpError):
            flp.parse(data)
        with self.assertRaises(flp.FlpError):
            flp.rewrite_paths(data, {VOX: NEW_VOX})

    def test_not_a_project(self):
        self.assertTrue(issubclass(flp.FlpError, ValueError))
        for data in [b"", b"FLhd", b"FLhd" + bytes(10), b"RIFF" + bytes(40), b"MThd" + song()[4:],
                     bytes(random.Random(5).randrange(256) for _ in range(500))]:
            with self.subTest(data[:8]):
                self.assertRejected(data)

    def test_damaged_header(self):
        good = song()
        self.assertRejected(good[:4] + (5).to_bytes(4, "little") + good[8:])     # FLhd too short
        self.assertRejected(good[:4] + (0xFFFFFFF0).to_bytes(4, "little") + good[8:])
        self.assertRejected(good[:14] + b"FLdx" + good[18:])                      # no FLdt
        self.assertRejected(good[:18] + (len(good) - 21).to_bytes(4, "little") + good[22:])  # chunk past the end

    def test_every_truncation(self):
        good = song()
        for n in range(len(good)):
            with self.subTest(n):
                self.assertRejected(good[:n])

    def test_events_running_past_the_end(self):
        for tail in [b"\xc4\xff\xff",                  # its length goes on past the end
                     b"\xc4\x80\x80\x80\x80\x80\x01",  # a length of more than 5 bytes
                     b"\xc4\x10abc",                   # 16 bytes promised, 3 there
                     b"\xc4",                          # no length at all
                     b"\x80\x01\x02",                  # a 4-byte value cut short
                     b"\x40\x01",                      # a 2-byte value cut short
                     b"\x00"]:                         # a 1-byte value missing
            with self.subTest(tail):
                self.assertRejected(raw(ev(199, b"25.1.0\0") + ev(196, VOX) + tail))
        # a length that never ends (from the network): rejected at once, not after
        # a minute of ever bigger numbers
        started = time.perf_counter()
        self.assertRejected(raw(ev(199, b"25.1.0\0") + b"\xc4" + b"\xff" * 1000000 + b"\x01"))
        self.assertLess(time.perf_counter() - started, 2.0)
        # the chunk ends inside an event, though the file goes on
        body = ev(196, VOX) + ev(203, "Kick")
        data = raw(body)
        self.assertRejected(data[:18] + (len(body) - 1).to_bytes(4, "little") + data[22:])

    def test_random_damage_only_ever_raises_flperror(self):
        rng, good = random.Random(1234), song()
        for i in range(3000):
            data = bytearray(good)
            for _ in range(rng.randint(1, 4)):
                if not data:
                    break
                at = rng.randrange(len(data))
                kind = rng.randrange(4)
                if kind == 0:
                    data[at] = rng.randrange(256)
                elif kind == 1:
                    del data[at:at + rng.randint(1, 30)]
                elif kind == 2:
                    data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 30)))
                else:
                    data = data[:at]
            for call in (lambda: flp.parse(data), lambda: flp.rewrite_paths(data, {VOX: NEW_VOX}, data_path="")):
                try:
                    call()
                except flp.FlpError:
                    pass
                except Exception as e:  # noqa: BLE001
                    self.fail("försök %d: %r av %s" % (i, e, bytes(data[:60])))


# --- local files -------------------------------------------------------------------

class ResolveTest(unittest.TestCase):
    FACTORY = r"C:\Program Files\Image-Line\FL Studio 2025"
    USER = r"C:\Users\Bo\Documents\Image-Line"
    DOWNLOADS = r"C:\Users\Bo\Documents\Image-Line\Downloads"
    HOME = r"C:\Users\Bo"

    def resolve(self, path, **dirs):
        dirs = dict(dict(factory_dir=self.FACTORY, user_dir=self.USER, downloads_dir=self.DOWNLOADS,
                         home_dir=self.HOME), **dirs)
        return flp.resolve(path, **dirs)

    def test_placeholders_in_any_case(self):
        factory, user, home = self.FACTORY, self.USER, self.HOME
        for path, expected in [
                (r"%USERPROFILE%\Documents\Image-Line\Data\FL Studio\Loop.wav",
                 home + r"\Documents\Image-Line\Data\FL Studio\Loop.wav"),
                (r"%userprofile%\Music\Loop.wav", home + r"\Music\Loop.wav"),
                (KICK, r"C:\Program Files\Image-Line\FL Studio 2025\Data\Patches\Packs\Drums\Kicks\Kick.wav"),
                (r"%flstudiofactorydata%\data\patches\kick.wav", factory + r"\data\patches\kick.wav"),
                (r"%FLStudioFactoryData%/Data/Patches//Misc/./Kick.wav", factory + r"\Data\Patches\Misc\Kick.wav"),
                (r"%FLStudioData%\Patches\Packs\Kick.wav", factory + r"\Data\Patches\Packs\Kick.wav"),
                (r"%FLSTUDIODATA%Patches\Kick.wav", factory + r"\Data\Patches\Kick.wav"),
                (r"%FLStudioUserData%\FL Studio\Audio\Recorded\Take 1.wav", user + r"\FL Studio\Audio\Recorded\Take 1.wav"),
                (r"%FLStudioDownloadsData%\Packs\Loop.wav", self.DOWNLOADS + r"\Packs\Loop.wav")]:
            with self.subTest(path):
                self.assertEqual(self.resolve(path), expected)

    def test_folders_given_in_another_form(self):
        expected = self.FACTORY + r"\Data\Patches\Kick.wav"
        for factory in (self.FACTORY + "\\", self.FACTORY.replace("\\", "/"), self.FACTORY + "\\\\"):
            with self.subTest(factory):
                self.assertEqual(self.resolve(r"%FLStudioFactoryData%\Data\Patches\Kick.wav", factory_dir=factory),
                                 expected)

    def test_absolute_paths_stay(self):
        for path, expected in [
                (VOX, VOX),
                ("c:/Users/Anna//Splice/./Vox 01.wav", r"c:\Users\Anna\Splice\Vox 01.wav"),
                (r"D:\Samples\Old\..\Loops\Åsa.wav", r"D:\Samples\Loops\Åsa.wav"),
                (r"\\nas\samples\Vox.wav", r"\\nas\samples\Vox.wav"),
                ("//nas/samples/Vox.wav", r"\\nas\samples\Vox.wav")]:
            with self.subTest(path):
                self.assertEqual(flp.resolve(path), expected)

    def test_the_user_profile_is_this_users_by_default(self):
        self.assertEqual(flp.resolve(r"%USERPROFILE%\Music\Loop.wav"),
                         ntpath.normpath(os.path.expanduser("~") + r"\Music\Loop.wav"))

    def test_none_when_it_cant_be_told(self):
        self.assertIsNone(flp.resolve(KICK))
        self.assertIsNone(flp.resolve(r"%FLStudioUserData%\x.wav", factory_dir=self.FACTORY))
        self.assertIsNone(flp.resolve(r"%FLStudioDownloadsData%\x.wav", self.FACTORY, self.USER))
        self.assertIsNone(flp.resolve(r"%FLStudioFactoryData%\x.wav", factory_dir=""))
        for path in [r"%FLStudioProjectData%\x.wav", r"%TEMP%\x.wav", r"Samples\x.wav", "x.wav", "C:x.wav",
                     r"\Users\x.wav", ""]:
            with self.subTest(path):
                self.assertIsNone(self.resolve(path))


class PluginDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="flsync-flp-")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, rel, text=""):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text)

    def nfo(self, rel, name, *own_names):
        lines = ["fileversion=2", "ps_name=" + name, "ps_files=%d" % len(own_names)]
        lines += ["ps_file_name_%d=%s" % (i, n) for i, n in enumerate(own_names)]
        self.write(rel, "\n".join(lines + ["ps_file_scanflags_0=1", ""]))

    def test_installed_plugins(self):
        db = os.path.join(self.dir, "Installed")
        self.write(r"Installed\Effects\Fruity\Fruity Limiter.fst")
        self.nfo(r"Installed\Effects\Fruity\Fruity Limiter.nfo", "Fruity Limiter", "Fruity Limiter")
        self.write(r"Installed\Effects\VST3\Abbey Road Chambers Mono-Stereo.fst")
        self.nfo(r"Installed\Effects\VST3\Abbey Road Chambers Mono-Stereo.nfo", "Abbey Road Chambers Mono-Stereo",
                 "Abbey Road Chambers Mono/Stereo", "Abbey Road Chambers Mono/Stereo")
        self.write(r"Installed\Generators\VST\Serum.FST")
        self.nfo(r"Installed\Generators\VST\Serum.NFO", "Serum", "Serum")
        self.write(r"Installed\Generators\CLAP\Vital.fst")  # no .nfo
        self.nfo(r"Installed\Effects\VST\ValhallaRoom_x64.nfo", "ValhallaRoom_x64", "ValhallaRoom_x64")  # failed scan
        self.write(r"Installed\VerifiedIDs.nfo", "VST:64:1298219617=Effects\\VST\\BASSROOM.NFO\n")
        self.write(r"Installed\Effects\Fruity\readme.txt", "ps_name=Nope\n")
        self.assertEqual(flp.installed_plugins(db), {
            "fruity limiter", "abbey road chambers mono-stereo", "abbey road chambers mono/stereo", "serum", "vital"})
        self.assertEqual(flp.installed_plugins(self.dir), flp.installed_plugins(db))  # its parent works too

    def test_missing_database(self):
        self.assertEqual(flp.installed_plugins(os.path.join(self.dir, "finns inte")), set())

    def test_native_plugins(self):
        for rel in [r"Plugins\Fruity\Generators\Wasp\Wasp_x64.dll", r"Plugins\Fruity\Generators\FLEX\FLEX_x64.dll",
                    r"Plugins\Fruity\Effects\Fruity Limiter\Fruity Limiter_x64.dll", r"Plugins\Fruity\Effects\notes.txt"]:
            self.write(rel)
        self.assertEqual(flp.native_plugins(self.dir), {"wasp", "flex", "fruity limiter"})
        self.assertEqual(flp.native_plugins(os.path.join(self.dir, "finns inte")), set())


# --- FL's own projects ---------------------------------------------------------------

def bundled(rel):
    return os.path.join(BUNDLED, *rel.split("/"))


def read(path):
    with open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(os.path.isdir(BUNDLED), "FL Studio 2025 finns inte på den här datorn")
class BundledProjectsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(glob.glob(os.path.join(BUNDLED, "**", "*.flp"), recursive=True))

    def each(self):
        """(name, bytes, Project) of every project bundled with FL."""
        self.assertGreater(len(self.files), 0)
        for path in self.files:
            data = read(path)
            yield os.path.relpath(path, BUNDLED), data, flp.parse(data)

    def test_every_bundled_project_parses(self):
        samples = found = 0
        root = ntpath.normpath(FL_DIR) + "\\"
        for name, _, p in self.each():
            with self.subTest(name):
                self.assertRegex(p.version, r"^\d+\.\d+\.\d+")
                self.assertTrue(24 <= p.ppq <= 960, p.ppq)
                self.assertNotIn("fruity wrapper", [n.lower() for n in p.plugins])  # every VST is named
                for s in p.samples:
                    self.assertTrue(s.factory, s.path)  # FL ships only its own samples
                    local = flp.resolve(s.path, FL_DIR)
                    self.assertTrue(local.startswith(root), local)
                    samples += 1
                    found += os.path.isfile(local)
        # FL's own Touchscreen template points at 5 samples that FL doesn't ship
        self.assertGreater(samples, 0)
        self.assertGreaterEqual(found, samples * 0.99, "FL:s egna samples hittas inte")

    def test_their_plugins_come_with_fl(self):
        native = flp.native_plugins(FL_DIR)
        for name, _, p in self.each():
            with self.subTest(name):
                self.assertEqual([n for n in p.plugins if n.lower() not in native], [])

    @unittest.skipUnless(os.path.isfile(bundled("Templates/Minimal/Basic with limiter/Basic with limiter.flp")),
                         "mallen finns inte")
    def test_a_template(self):
        p = flp.parse(read(bundled("Templates/Minimal/Basic with limiter/Basic with limiter.flp")))
        drums = r"%FLStudioFactoryData%\Data\Patches\Packs\Legacy\Drums\Dance\Basic 808 "
        self.assertEqual(p.samples, tuple(flp.SampleRef(drums + kind + ".wav", True)
                                          for kind in ("Kick", "Clap", "HiHat", "Snare")))
        self.assertEqual((p.ppq, p.title, p.plugins, p.data_path), (96, "", ("FLEX", "Emphasizer"), ""))

    @unittest.skipUnless(os.path.isfile(bundled("Demo projects/Performance mode/Charles Deluxe - Performance Demo.flp")),
                         "demon finns inte")
    def test_a_project_from_fl_10(self):
        p = flp.parse(read(bundled("Demo projects/Performance mode/Charles Deluxe - Performance Demo.flp")))
        self.assertEqual((p.version, p.title), ("10.9.0", "Performance demo"))
        self.assertEqual(len(p.samples), 9)
        for s in p.samples:
            self.assertTrue(s.path.startswith("%FLStudioData%\\Patches\\Misc\\Used by demo projects\\") and s.factory)
            self.assertTrue(os.path.isfile(flp.resolve(s.path, FL_DIR)), s.path)

    @unittest.skipUnless(os.path.isfile(bundled("Demo projects/Visual/ZGameEditor Visualizer/Treben - Gimbal and Sinan - RawFL.flp")),
                         "demon finns inte")
    def test_vsts_by_their_own_names(self):
        p = flp.parse(read(bundled("Demo projects/Visual/ZGameEditor Visualizer/Treben - Gimbal and Sinan - RawFL.flp")))
        for name in ("Fruity Compressor", "Fruity Flanger", "Fruity Chorus"):  # VST versions of FL's effects
            self.assertIn(name, p.plugins)

    def test_their_channels(self):
        for name, data, p in self.each():
            with self.subTest(name):
                numbers = [int.from_bytes(value, "little") for eid, value in walk(data) if eid == 64]
                self.assertEqual([c.index for c in p.channels], numbers)  # FL numbers them in file order
                self.assertEqual(numbers, list(range(len(numbers))))
                # every plugin is a channel's or in a mixer slot
                self.assertEqual(set(p.plugins), {c.plugin for c in p.channels if c.plugin} | set(p.effects))

    @unittest.skipUnless(os.path.isfile(bundled("Templates/Minimal/Basic with limiter/Basic with limiter.flp")),
                         "mallen finns inte")
    def test_a_templates_channels_and_mixer(self):
        p = flp.parse(read(bundled("Templates/Minimal/Basic with limiter/Basic with limiter.flp")))
        self.assertEqual(p.channels, tuple(flp.Channel(i, "808 " + n, "") for i, n in enumerate(("Kick", "Clap", "HiHat", "Snare")))
                         + (flp.Channel(4, "FLEX Bass", "FLEX"),))
        self.assertEqual(p.effects, ("Emphasizer",))

    @unittest.skipUnless(os.path.isfile(bundled("Demo projects/Performance mode/nucleon - Housework.flp")), "demon finns inte")
    def test_channel_names_from_fl_11(self):
        p = flp.parse(read(bundled("Demo projects/Performance mode/nucleon - Housework.flp")))
        self.assertEqual(p.version, "11.0.3")
        self.assertEqual(p.channels[:5], (flp.Channel(0, "Chords", "Harmless"), flp.Channel(1, "Bass", "Harmless"),
                                          flp.Channel(2, "Lead", "Harmless"), flp.Channel(3, "Houser", "Slicex"),
                                          flp.Channel(4, "Ride Break", "Slicex")))

    def test_samples_inside_their_plugins(self):
        total = placeholders = found = 0
        own = []
        for name, _, p in self.each():
            for s in p.plugin_samples:
                total += 1
                with self.subTest(name=name, path=s.path):
                    self.assertRegex(s.path, r"(?i)^(?:[a-z]:\\|%flstudio(?:factory)?data%\\)")
                    self.assertTrue(s.path.lower().endswith(tuple("." + a for a in flp.AUDIO)))
                if s.path.startswith("%"):
                    self.assertTrue(s.factory, s.path)
                    placeholders += 1
                    found += os.path.isfile(flp.resolve(s.path, FL_DIR))
                elif not s.factory:
                    own.append((name, s.path))
        self.assertGreater(total, 2000)
        # Six templates name a sample FL doesn't ship, two demos a path that FL wrote wrongly
        # ("%FLStudioFactoryData%\%FLStudioFactoryData%\...")
        self.assertGreaterEqual(found, placeholders * 0.99, "FL:s egna samples hittas inte")
        # The only ones not from FL: a DirectWave piano on a demo author's D: drive.
        self.assertEqual(sorted({n for n, _ in own}),
                         [os.path.join("Demo projects", "Demo songs", "Mehdi Ebrahiminejad - Never Out.flp")])
        self.assertTrue(all(path.startswith("D:\\00 Projects\\") for _, path in own), own)

    def test_samples_inside_plugins_of_each_kind(self):
        for rel, path in [
                ("Demo projects/Product demos/Square a Saw - Oxygen (Tevlo Vocals Resynthesized).flp",  # FPC, UTF-16
                 r"%FLStudioData%\Patches\Packs\Drums\Kits\HQ Rock Kit\snare side hq rock #1.wav"),
                ("Demo projects/Demo songs/Bombs Away ft KARRA - I'm Awake.flp",  # Fruity Slicer
                 r"%FLStudioFactoryData%\Data\Patches\Packs\Loops\DL Breaker.wav"),
                ("Demo projects/Demo songs/Edlan & Ella Noël - Song For You.flp",  # UTF-8 in Fruity Slicer
                 "%FLStudioFactoryData%\\Data\\Patches\\Misc\\Used by demo projects\\Edlan & Ella Noël - Song For You"
                 "\\WAMDB_Drum_Loop_10_Tops_175BPM_464633.ogg"),
                ("Demo projects/Demo songs/Astes - Bien Duro.flp",  # Fruity Convolver's impulse
                 r"%FLStudioFactoryData%\Data\Patches\Misc\Used by demo projects\Astes - Bien Duro\IMP Evnt - Medium Chamber.wv"),
                ("Demo projects/Demo songs/Asher Postman - Future House.flp",  # GMS, in the author's FL
                 r"C:\Program Files\Image-Line\FL Studio BETA\Plugins\Fruity\Generators\GMS\Synth Waves\Keys.wav")]:
            if not os.path.isfile(bundled(rel)):
                continue
            with self.subTest(rel):
                p = flp.parse(read(bundled(rel)))
                self.assertIn(flp.SampleRef(path, True), p.plugin_samples)
                if path.startswith("%"):
                    self.assertTrue(os.path.isfile(flp.resolve(path, FL_DIR)))

    def test_newer_projects_have_event_172(self):
        newer = 0
        for name, data, p in self.each():
            if tuple(int(n) for n in p.version.split(".")[:3]) < (25, 2, 3):
                continue
            with self.subTest(name):
                events = walk(data)
                at = [eid for eid, _ in events].index(172)
                self.assertLess(at, 8)
                self.assertEqual(len(events[at][1]), 3)
                self.assertEqual(events[at + 1][0], 192)
                self.assertTrue(events[at + 1][1].decode("utf-16-le").startswith("FL Studio " + p.version))
                newer += 1
        if not newer:
            self.skipTest("inga projekt från FL 25.2.3 eller senare")

    def test_unchanged_rewrite_gives_the_same_bytes(self):
        for name, data, p in self.each():
            with self.subTest(name):
                self.assertEqual(flp.rewrite_paths(data, {}), data)
                same = {s.path: s.path for s in p.samples}
                self.assertEqual(flp.rewrite_paths(data, same, data_path=p.data_path), data)

    def test_rewriting_every_sample(self):
        rewritten = 0
        for name, data, p in self.each():
            if not p.samples:
                continue
            with self.subTest(name):
                mapping = {s.path: r"C:\FLSync\Åsa\Samples\%d %s" % (i, ntpath.basename(s.path))
                           for i, s in enumerate(p.samples)}
                out = flp.rewrite_paths(data, mapping, data_path="")
                q = flp.parse(out)
                self.assertEqual([s.path for s in q.samples], [mapping[s.path] for s in p.samples])
                self.assertFalse(any(s.factory for s in q.samples))
                self.assertEqual((q.version, q.ppq, q.title, q.plugins, q.data_path),
                                 (p.version, p.ppq, p.title, p.plugins, ""))
                before, after = walk(data), walk(out)
                self.assertEqual(len(before), len(after))
                changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
                self.assertTrue(all(before[i][0] in (196, 202) and after[i][0] == before[i][0] for i in changed),
                                "något annat än sökvägarna ändrades")
                back = flp.rewrite_paths(out, {new: old for old, new in mapping.items()}, data_path=p.data_path)
                self.assertEqual(back, data)
                rewritten += 1
        self.assertGreater(rewritten, 0)


if __name__ == "__main__":
    unittest.main()
