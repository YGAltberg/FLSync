"""What live sync can't carry, and FL saving by itself (docs/phase5.md).

Clips in the playlist, piano-roll notes, samples, new channels and plugins
and their state aren't keys: FL's script API can't read or write them. They
travel as saved versions. So that nobody has to press Ctrl+S for them, the
client looks for edits that weren't a live value (an undo step with no live
change around it, more patterns, a longer song) and lets FL save by itself
once nothing changes for a moment; the bridge shares the save as if Ctrl+S
had been pressed. Someone else's version comes in the same way: FL saves
first (nothing of ours is lost) and the bridge opens the merged project.
"""

import general
import midi

try:
    import patterns
except ImportError:  # every FL with scripting has it
    patterns = None

try:
    import transport
except ImportError:
    transport = None

try:
    import ui
except ImportError:
    ui = None

SAVES_SINCE = (26, 1, 7)  # FL fixed crashes and freezes of scripts calling globalTransport in 26.1.7
UNDO = getattr(midi, "HW_Dirty_Undo", 131072)  # OnRefresh: the undo history changed (FL 26.1)


def _call(module, name, *args):
    """module.name(*args), or None if this FL hasn't got it or can't say now."""
    function = getattr(module, name, None)
    if function is None:
        return None
    try:
        return function(*args)
    except Exception:  # e.g. "Operation unsafe at current time"
        return None


def marks():
    """What changes with edits live sync can't see: (undo history, number of
    patterns, length of the song). A part this FL can't tell is None."""
    undo = (_call(general, "getUndoHistoryCount"), _call(general, "getUndoHistoryPos"))
    return (undo if undo != (None, None) else None, _call(patterns, "patternCount"),
            _call(transport, "getSongLength", getattr(midi, "SONGLENGTH_ABSTICKS", 2)))


def recording():
    return bool(_call(transport, "isRecording"))


def in_menu():
    return bool(_call(ui, "isInPopupMenu"))


def can_save(version):
    """Whether FL `version` (a tuple) may be asked to save by a script."""
    return version is not None and tuple(version[:3]) >= SAVES_SINCE and transport is not None \
        and hasattr(transport, "globalTransport") and hasattr(midi, "FPT_Save")


def save():
    """Ask FL to save the project, as Ctrl+S does. False if it refused."""
    try:
        transport.globalTransport(midi.FPT_Save, 1, getattr(midi, "PME_System", 2), getattr(midi, "GT_Global", 8))
    except Exception:
        return False
    return True
