"""The project's structure fingerprint (docs/protocol_v2.md, "Structure").

Sync keys address things by number: channel 3, mixer track 2 slot 1. They mean
the same thing in two projects only if both have the same channels and plugins
in the same places, so parameters flow only between FLs whose fingerprints
match. Names aren't part of it: renaming is synced live.

The fingerprint has two parts, 6 hex digits each: the channels', then the
mixer's. Mixer keys only need the mixer part to match, channel keys the
channel part, so a new instrument doesn't stop the mixer's sync and a new
effect doesn't stop the channels'. Song keys (tempo, play) need neither.
"""

import channels
import mixer
import plugins

from flsync_mixer import SLOTS, tracks

GLOBAL = 1  # useGlobalIndex, passed by position like everywhere else
PART = 6    # hex digits per part (flsync_client.parts_differ splits them)


def _fnv64(text):
    """FNV-1a, 64 bits: the same everywhere (no hashlib needed inside FL)."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def describe_channels():
    """One item per channel: its type and instrument."""
    parts = []
    for ch in range(channels.channelCount(GLOBAL)):
        plugin = plugins.getPluginName(ch, -1, False, GLOBAL) if plugins.isValid(ch, -1, GLOBAL) else ""
        parts.append("c%d:%s" % (channels.getChannelType(ch, GLOBAL), plugin))
    return parts


def describe_mixer():
    """The number of mixer tracks, and one item per effect slot with a plugin."""
    track_list = tracks()
    parts = ["t%d" % len(track_list)]
    for track in track_list:
        for slot in range(SLOTS):
            if mixer.isTrackPluginValid(track, slot):
                parts.append("m%d.%d:%s" % (track, slot, plugins.getPluginName(track, slot)))
    return parts


def describe():
    """What the fingerprint is made of, one item per channel and plugin slot."""
    return describe_channels() + describe_mixer()


def fingerprint():
    """The structure's fingerprint, or None if this FL can't tell (old API, or
    not right now: a plugin that is loading can refuse to be asked)."""
    try:
        return _fnv64("|".join(describe_channels()))[:PART] + _fnv64("|".join(describe_mixer()))[:PART]
    except Exception:
        return None
