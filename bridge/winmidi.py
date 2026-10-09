"""Windows MIDI (WinMM) through ctypes: just enough to send and receive short
MIDI messages on loopMIDI ports, so the bridge needs nothing beyond Python."""

import collections
import ctypes
import sys
from ctypes import wintypes

MAXPNAMELEN = 32
CALLBACK_NULL = 0x00000000
CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MMSYSERR_ALLOCATED = 4


class MidiError(Exception):
    pass


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * MAXPNAMELEN), ("dwSupport", wintypes.DWORD)]


class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * MAXPNAMELEN), ("wTechnology", wintypes.WORD),
                ("wVoices", wintypes.WORD), ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
                ("dwSupport", wintypes.DWORD)]


MidiInProc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.UINT,
                                ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)

_winmm = None


def _api():
    global _winmm
    if _winmm is None:
        if sys.platform != "win32":
            raise MidiError("bryggan använder Windows MIDI och kör bara på Windows")
        w = ctypes.WinDLL("winmm")
        handle_p = ctypes.POINTER(wintypes.HANDLE)
        for name, args in {
            "midiInGetNumDevs": [], "midiOutGetNumDevs": [],
            "midiInGetDevCapsW": [ctypes.c_size_t, ctypes.POINTER(MIDIINCAPSW), wintypes.UINT],
            "midiOutGetDevCapsW": [ctypes.c_size_t, ctypes.POINTER(MIDIOUTCAPSW), wintypes.UINT],
            "midiInOpen": [handle_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD],
            "midiOutOpen": [handle_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD],
            "midiOutShortMsg": [wintypes.HANDLE, wintypes.DWORD],
            "midiInStart": [wintypes.HANDLE], "midiInStop": [wintypes.HANDLE],
            "midiInReset": [wintypes.HANDLE], "midiInClose": [wintypes.HANDLE],
            "midiOutReset": [wintypes.HANDLE], "midiOutClose": [wintypes.HANDLE],
        }.items():
            fn = getattr(w, name)
            fn.argtypes = args
            fn.restype = wintypes.UINT
        _winmm = w
    return _winmm


def _check(result, what):
    if result == MMSYSERR_ALLOCATED:
        raise MidiError("%s används redan av ett annat program" % what)
    if result != 0:
        raise MidiError("%s: MIDI-fel %d" % (what, result))


def port_names(inputs):
    api = _api()
    if inputs:
        count, caps_type, get_caps = api.midiInGetNumDevs(), MIDIINCAPSW, api.midiInGetDevCapsW
    else:
        count, caps_type, get_caps = api.midiOutGetNumDevs(), MIDIOUTCAPSW, api.midiOutGetDevCapsW
    names = []
    for index in range(count):
        caps = caps_type()
        ok = get_caps(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0
        names.append(caps.szPname if ok else "")
    return names


def find_port(name, inputs):
    names = port_names(inputs)
    for index, candidate in enumerate(names):
        if candidate.lower() == name.lower():
            return index
    kind = "MIDI-ingång" if inputs else "MIDI-utgång"
    raise MidiError("hittar ingen %s som heter '%s'. Skapa den i loopMIDI. Finns nu: %s"
                    % (kind, name, ", ".join(n for n in names if n) or "inga"))


class MidiOut:
    def __init__(self, name):
        self.name = name
        self.handle = wintypes.HANDLE()
        _check(_api().midiOutOpen(ctypes.byref(self.handle), find_port(name, inputs=False),
                                  0, 0, CALLBACK_NULL), "MIDI-utgången '%s'" % name)

    def send(self, message):
        """Send one short message packed as status | data1 << 8 | data2 << 16."""
        if self.handle:
            _check(_api().midiOutShortMsg(self.handle, message), "MIDI-utgången '%s'" % self.name)

    def close(self):
        if self.handle:
            _api().midiOutReset(self.handle)
            _api().midiOutClose(self.handle)
            self.handle = wintypes.HANDLE()


class MidiIn:
    """Collects incoming short messages (packed ints) in `self.received`.

    WinMM calls back on its own thread; deque.append is thread-safe, so the
    bridge simply drains the deque from its event loop.
    """

    def __init__(self, name):
        api = _api()
        self.name = name
        self.received = collections.deque()
        self._proc = MidiInProc(self._callback)  # keep a reference: WinMM calls it
        self.handle = wintypes.HANDLE()
        _check(api.midiInOpen(ctypes.byref(self.handle), find_port(name, inputs=True),
                              ctypes.cast(self._proc, ctypes.c_void_p).value, 0, CALLBACK_FUNCTION),
               "MIDI-ingången '%s'" % name)
        _check(api.midiInStart(self.handle), "MIDI-ingången '%s'" % name)

    def _callback(self, handle, msg, instance, param1, param2):
        if msg == MIM_DATA:
            self.received.append(param1 & 0xFFFFFF)

    def close(self):
        if self.handle:
            api = _api()
            api.midiInStop(self.handle)
            api.midiInReset(self.handle)
            api.midiInClose(self.handle)
            self.handle = wintypes.HANDLE()
