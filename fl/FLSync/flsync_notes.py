"""Which pattern and channel the piano roll shows (docs/protocol_v2.md, "Notes").

The MIDI-script API can't touch piano-roll notes; the piano roll scripts in
fl/Piano roll scripts/ can, with the bridge as go-between. They can't ask FL
which pattern and channel they work on, so this script tells the bridge
(`pr {p, c, n}`): the active pattern and the selected channel, which is the one
the piano roll shows.
"""

import channels
import patterns

GLOBAL = 1


def context():
    """(pattern, global channel index, channel name), or None if there is no channel."""
    try:
        channel = channels.selectedChannel(1, 0, GLOBAL)
        if channel < 0:
            return None
        return patterns.patternNumber(), channel, channels.getChannelName(channel, GLOBAL)
    except (AttributeError, TypeError, ValueError):
        return None
