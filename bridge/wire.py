"""The MIDI wire format between the bridge and the FLSync script in FL Studio.

Messages are ASCII JSON sent as a run of pitch-bend events:

    START, one PAYLOAD event per byte, END (data1 = 7-bit checksum)

Every event carries a 7-bit sequence number in data2, and its MIDI channel
says what it is and which way it goes:

    channel  0-3   bridge -> FL    (PAYLOAD, START, END, HEARTBEAT)
    channel  8-11  FL -> bridge    (the same, plus 8)

Why it looks like this:
  * Short messages, not SysEx: SysEx between FL and the bridge crashed FL
    Studio 2025.2.4.
  * Separate channels per direction let FL and the bridge share one loopMIDI
    port; each side hears its own events echoed back and ignores them.
  * Sequence numbers: FL sometimes delivers its MIDI input twice (two open
    handles on the same port), interleaved: a copy comes at most a few
    events after the original. The receiver drops an event numbered up to
    DUPLICATE_WINDOW behind the one it expects as a copy. Any other number
    means events went missing (FL stalled and the driver dropped input): the
    message in progress is broken and the receiver goes on from there, however
    far ahead the number is. The checksum catches anything that still goes wrong.
  * Heartbeats carry a random epoch in data1. A new epoch means the sender
    restarted and its numbering began again, so the receiver starts over
    instead of mistaking the new events for copies of old ones.

The FL script has its own copy of this format (it can't import from here);
tests check that the two agree.
"""

import json
import random

STATUS = 0xE0  # pitch bend
PAYLOAD, START, END, HEARTBEAT_KIND = 0, 1, 2, 3
TO_FL, FROM_FL = 0, 8   # channel offsets per direction
DUPLICATE_WINDOW = 8    # an event up to this far behind is a copy (copies seen: at most 3 events late)
HEARTBEAT = object()    # what Decoder.feed returns for a heartbeat


def unpack(message):
    return message & 0xFF, (message >> 8) & 0x7F, (message >> 16) & 0x7F


def to_ascii_payload(msg):
    return json.dumps(msg, separators=(",", ":")).encode("ascii")


def checksum(payload):
    return sum(payload) & 0x7F


def parts(payload):
    """The (kind, data1) parts of one payload, not yet numbered."""
    return [(START, 0)] + [(PAYLOAD, byte) for byte in payload] + [(END, checksum(payload))]


class Encoder:
    """Numbers events for one direction. Number them as they are sent, so
    the sequence on the wire never goes backwards."""

    def __init__(self, direction=TO_FL, epoch=None):
        self.direction = direction
        self.epoch = random.randrange(128) if epoch is None else epoch
        self.seq = 0

    def event(self, kind, data1=0):
        event = (STATUS | (self.direction + kind)) | (data1 << 8) | (self.seq << 16)
        self.seq = (self.seq + 1) & 0x7F
        return event

    def heartbeat(self):
        return self.event(HEARTBEAT_KIND, self.epoch)

    def message(self, msg):
        return [self.event(kind, data1) for kind, data1 in parts(to_ascii_payload(msg))]


class Decoder:
    """Reassembles the messages travelling in one direction."""

    def __init__(self, direction=FROM_FL):
        self.direction = direction
        self.epoch = None
        self.expect = None
        self.buffer = None

    def accepts(self, status):
        return status & 0xF0 == STATUS and 0 <= (status & 0x0F) - self.direction <= HEARTBEAT_KIND

    def feed(self, status, data1, data2):
        """Returns a message dict, HEARTBEAT, or None (part of a message, a copy,
        or not ours)."""
        if not self.accepts(status):
            return None
        kind = (status & 0x0F) - self.direction
        if kind == HEARTBEAT_KIND and data1 != self.epoch:
            self.epoch, self.expect, self.buffer = data1, None, None  # the sender (re)started
        if self.expect is not None and data2 != self.expect:
            if 0 < (self.expect - data2) & 0x7F <= DUPLICATE_WINDOW:
                return None  # a copy of an event we already have
            self.buffer = None  # events went missing: the message in progress is broken
        self.expect = (data2 + 1) & 0x7F
        if kind == HEARTBEAT_KIND:
            return HEARTBEAT
        if kind == START:
            self.buffer = bytearray()
        elif self.buffer is not None:
            if kind == PAYLOAD:
                self.buffer.append(data1)
            elif kind == END:
                payload, self.buffer = bytes(self.buffer), None
                if checksum(payload) != data1:
                    return None
                try:
                    msg = json.loads(payload)
                except ValueError:
                    return None
                return msg if isinstance(msg, dict) else None
        return None
