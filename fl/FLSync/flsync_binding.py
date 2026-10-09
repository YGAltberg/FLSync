"""The interface between the sync client and one area of an FL project.

A Binding exposes part of FL (the mixer, the channel rack, ...) as sync keys
(see docs/protocol_v2.md for the key scheme). The client never calls FL
itself; it asks bindings to read and write.

Reading works in *units*: a unit is a small group of keys that are read
together, e.g. one mixer track or one channel. Every tick the client re-reads
the units FL flagged as changed plus a few more in rotation (the sweep),
compares the values with what it saw last time and sends the differences.

All methods are called on FL's UI thread and must be quick.
"""

ALL = "all"  # returned from a callback hook: re-read every unit


def is_color(value):
    """Whether someone's value can be an FL color: FL's colors are signed 32-bit
    ints (0xAARRGGBB, so often negative)."""
    return isinstance(value, int) and not isinstance(value, bool) and -2 ** 31 <= value < 2 ** 31


class Binding:
    prefix = ""        # this binding owns the keys "<prefix>/..."
    seed = True        # include its keys in the starting values the first FL shares
    sweep_budget = 8   # units re-read per tick even when FL flagged nothing

    def units(self):
        """The ids (hashable) of the units that exist right now. Called every tick."""
        return ()

    def read(self, unit):
        """Yield (key, value) for every key of one unit, as FL has it now."""
        return ()

    def write(self, key, value):
        """Put someone else's value into FL.

        Return the value FL holds afterwards (the client stores it as the new
        baseline so the write is not sent back), or None when this FL has no
        such key or the value can't be applied.
        """
        return None

    def describe(self, key):
        """A short Swedish description for presence hints, e.g. 'Insert 3 volym'."""
        return key

    def transient(self, key):
        """True for event keys (play/stop): applied when someone changes them,
        ignored in the snapshot a client gets when it joins."""
        return False

    def shared_at_start(self, key, value):
        """Whether this value goes into the starting values the first FL shares.
        Everything travels over slow MIDI, so a binding with many keys that are
        nearly always at their defaults (500 playlist tracks) can leave those out.
        Changes are always sent."""
        return True

    # --- hooks from FL's script callbacks: return units to re-read now ------

    def on_dirty_mixer_track(self, index):
        return ()

    def on_dirty_channel(self, index, flag):
        return ()

    def on_refresh(self, flags):
        return ()

    def on_project_load(self):
        """Another project was opened: forget anything kept about the old one.
        The client takes the new project's values as the baseline afterwards."""

    def tick(self, now):
        """Housekeeping once per tick, before reading.

        Returns (reread, rebaseline): units to re-read now, and units whose
        current values should become the baseline without being sent (e.g. a
        plugin that just got focus or a pattern the user switched to).
        """
        return (), ()
