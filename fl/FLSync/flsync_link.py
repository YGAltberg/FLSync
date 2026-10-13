"""JSON messages between the FL script and the bridge, carried as MIDI events.

Same wire format as bridge/wire.py (see there for the why): a message is
START, one PAYLOAD event per ASCII JSON byte, END with a 7-bit checksum. The
events are pitch bends with a 7-bit sequence number in data2; channels 0-3
come from the bridge, channels 8-11 go to it. Heartbeats carry the sender's
random epoch, so a restarted sender is recognised at once.
"""

import collections
import json
import random

import device

WIRE_STATUS = 0xE0
PAYLOAD, START, END, HEARTBEAT = 0, 1, 2, 3
FROM_BRIDGE, TO_BRIDGE = 0, 8  # channel offsets per direction
DUPLICATE_WINDOW = 8            # an event up to this far behind is a copy (FL can deliver input twice; see wire.py)
MAX_EVENTS_PER_TICK = 90        # spread big bursts over ticks
LINK_TIMEOUT = 3.0              # the bridge counts as gone after this long without an event


def wire_parts(msg):
    """The (kind, data1) parts of one message, numbered only when sent."""
    payload = json.dumps(msg, separators=(",", ":")).encode("ascii")
    return [(START, 0)] + [(PAYLOAD, b) for b in payload] + [(END, sum(payload) & 0x7F)]


class MidiLink:
    def __init__(self):
        self.received = []
        self.buffer = None
        self.epoch = None                    # the bridge's current epoch
        self.expect = None                   # sequence number of the next new incoming event
        self.outgoing = collections.deque()  # (kind, data1)
        self.send_epoch = random.randrange(128)
        self.send_seq = 0
        self.last_heard = None
        self.stats = {"messages": 0, "copies": 0, "gaps": 0, "broken": 0}  # for troubleshooting

    def alive(self, now):
        return self.last_heard is not None and now - self.last_heard < LINK_TIMEOUT

    def receiving(self):
        """A message from the bridge is coming in (a big welcome takes seconds)."""
        return self.buffer is not None

    def forgive(self, now):
        """FL itself was busy and heard nothing meanwhile: the silence was ours,
        not the bridge's, so the bridge gets LINK_TIMEOUT from now to be heard."""
        if self.last_heard is not None:
            self.last_heard = max(self.last_heard, now)

    def can_send(self):
        return bool(device.isAssigned())  # needs an output port linked to the script

    def receive(self, status, data1, data2, now):
        """Take one incoming event. Returns "heartbeat" or "message" when the
        script should run, otherwise None."""
        kind = (status & 0x0F) - FROM_BRIDGE
        if status & 0xF0 != WIRE_STATUS or not PAYLOAD <= kind <= HEARTBEAT:
            return None  # our own events echoed by the shared port, or other MIDI
        # After a silence no copy comes this late: the bridge may have restarted
        # with the epoch it had before (1 in 128), numbering from 0 again.
        silent = not self.alive(now)
        self.last_heard = now
        if kind == HEARTBEAT and data1 != self.epoch:
            self.epoch, self.expect, self.buffer = data1, None, None  # the bridge (re)started
        if self.expect is not None and data2 != self.expect:
            if not silent and 0 < (self.expect - data2) & 0x7F <= DUPLICATE_WINDOW:
                self.stats["copies"] += 1
                return None  # a copy of an event we already have
            self.stats["gaps"] += 1
            self.buffer = None  # events went missing: the message in progress is broken
        self.expect = (data2 + 1) & 0x7F
        if kind == HEARTBEAT:
            return "heartbeat"
        if kind == START:
            self.buffer = bytearray()
        elif self.buffer is not None:
            if kind == PAYLOAD:
                self.buffer.append(data1)
            elif kind == END:
                payload, self.buffer = bytes(self.buffer), None
                try:
                    msg = json.loads(payload) if sum(payload) & 0x7F == data1 else None
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    self.stats["broken"] += 1
                    return None
                self.stats["messages"] += 1
                self.received.append(msg)
                return "message"
        return None

    def poll(self):
        messages, self.received = self.received, []
        return messages

    def send(self, msg):
        self.outgoing.extend(wire_parts(msg))

    def busy(self):
        """More is waiting to go out than the next flush sends."""
        return len(self.outgoing) > MAX_EVENTS_PER_TICK

    def heartbeat(self):
        self.outgoing.append((HEARTBEAT, self.send_epoch))

    def event(self, kind, data1=0):
        event = (WIRE_STATUS | (TO_BRIDGE + kind)) | (data1 << 8) | (self.send_seq << 16)
        self.send_seq = (self.send_seq + 1) & 0x7F
        return event

    def flush(self):
        if not self.can_send():
            self.outgoing.clear()
            return
        for _ in range(min(MAX_EVENTS_PER_TICK, len(self.outgoing))):
            device.midiOutMsg(self.event(*self.outgoing.popleft()))
