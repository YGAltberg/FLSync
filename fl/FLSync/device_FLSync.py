# name=FLSync
"""
FLSync - live multiplayer for FL Studio. This is the file FL loads.

FL doesn't let scripts open network connections or files, so the script
talks MIDI with the FLSync bridge (bridge/bridge.py), which talks to the relay:

    FL Studio <--MIDI "FLSync"--> bridge <--TCP--> relay

The code lives in the flsync_* modules next to this file:
    flsync_link      the MIDI wire format
    flsync_client    sync logic: ordering, conflicts, presence, structure
    flsync_structure the project's structure fingerprint
    flsync_notes     which pattern/channel the piano roll shows (for the note scripts)
    flsync_binding   the interface for one area of FL
    flsync_mixer ... the areas themselves
"""

import sys

# FL's "Reload script" runs this file again, but Python would keep our modules
# cached. Drop them so a reload picks up edited code (and tests get fresh copies).
for _name in [n for n in sys.modules if n.startswith("flsync_")]:
    del sys.modules[_name]

import os
import time
import traceback

import ui

import flsync_client
import flsync_link
import flsync_log
import flsync_notes
import flsync_structure
from flsync_client import SyncClient
from flsync_link import MidiLink
from flsync_log import log
from flsync_mixer import MixerBinding, MixerRoutingBinding, MixerSlotsBinding

CONFIG_OVERRIDES = {}  # used by the tests
MAX_ERRORS = 25        # FLSync turns itself off after this many errors...
ERROR_WINDOW = 60.0    # ...within this many seconds (one now and then over a long session is no reason)
AREAS = ("flsync_channels", "flsync_plugins", "flsync_song")  # each has bindings()


def _import_areas():
    """The area modules that are present, imported now while the script loads."""
    modules = []
    for name in AREAS:
        try:
            modules.append(__import__(name))
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
    return modules


_AREA_MODULES = _import_areas()


def make_bindings():
    bindings = [MixerBinding(), MixerSlotsBinding(), MixerRoutingBinding()]
    for module in _AREA_MODULES:
        bindings.extend(module.bindings())
    return bindings


def _hint(text):
    try:
        ui.setHintMsg(text)
    except Exception:
        pass


# --- FL Studio callbacks --------------------------------------------------------

_client = None
_errors = 0            # errors since _errors_since
_errors_since = None   # when the current ERROR_WINDOW began


def _run(method, *args):
    """Call into the client without letting an exception spam FL every tick."""
    global _client, _errors, _errors_since
    if _client is None:
        return None
    try:
        return method(_client, *args)
    except Exception:
        now = time.monotonic()
        if _errors_since is None or now - _errors_since >= ERROR_WINDOW:
            _errors, _errors_since = 0, now
        _errors += 1
        if _errors == 1 or _client.debug:
            log("fel:\n" + traceback.format_exc())
        if _errors >= MAX_ERRORS:
            log("för många fel, FLSync stängs av. Ladda om scriptet när felet är åtgärdat.")
            _client = None
        return None


def OnInit():
    global _client, _errors, _errors_since
    _client, _errors, _errors_since = None, 0, None
    # FL's sandbox blocks files, so settings come from the environment; the
    # bridge can also rename us (bridge.py --name).
    name = (CONFIG_OVERRIDES.get("name") or os.environ.get("USERNAME")
            or os.environ.get("COMPUTERNAME") or "FL")
    try:
        client = SyncClient(name, make_bindings(), MidiLink(), hint=_hint,
                            debug=CONFIG_OVERRIDES.get("debug", False),
                            fingerprint=flsync_structure.fingerprint, context=flsync_notes.context)
        client.start()
        can_send = client.link.can_send()
    except Exception:  # an exception mustn't reach FL: FLSync just stays off
        log("kunde inte starta, så FLSync är avstängt. Ladda om scriptet när felet är åtgärdat:\n"
            + traceback.format_exc())
        return
    _client = client
    log("igång som '%s' (API-version %s), väntar på bryggan (bridge.py)"
        % (name, flsync_client.api_version()))
    if not can_send:
        log("ingen MIDI-utgång är kopplad till FLSync än. Välj porten 'FLSync' under Output "
            "och ge den samma Port-nummer som ingången.")


def OnDeInit():
    global _client
    _client = None


def OnIdle():
    _run(SyncClient.idle, time.monotonic())


def OnMidiIn(event):
    # Everything on the FLSync port is sync traffic: never let FL play it.
    event.handled = True
    _run(SyncClient.on_midi, event.status, event.data1, event.data2, time.monotonic())


def OnDirtyMixerTrack(index):
    _run(SyncClient.on_dirty_mixer_track, index)


def OnDirtyChannel(index, flag):
    _run(SyncClient.on_dirty_channel, index, flag)


def OnRefresh(flags):
    _run(SyncClient.on_refresh, flags)


def OnProjectLoad(status):
    _run(SyncClient.project_load, status)
