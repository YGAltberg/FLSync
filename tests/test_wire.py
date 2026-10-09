"""The MIDI wire format, and that the FL script's copy of it matches the bridge's."""

import random
import unittest

from fake_fl import FakeFL
from harness import bridge_mod  # noqa: F401  (puts bridge/ on sys.path)

import wire

MSG = {"t": "set", "k": "mixer/2/pan", "v": -0.5, "l": 3, "name": "Åsa"}


def decode_all(events, direction=wire.TO_FL):
    decoder = wire.Decoder(direction)
    results = [decoder.feed(*wire.unpack(e)) for e in events]
    return [r for r in results if isinstance(r, dict)]


class WireTest(unittest.TestCase):
    def test_roundtrip(self):
        events = wire.Encoder(wire.TO_FL).message(MSG)
        self.assertEqual(len(events), len(wire.to_ascii_payload(MSG)) + 2)
        self.assertEqual(decode_all(events), [MSG])

    def test_every_event_is_a_valid_pitch_bend(self):
        msg = {"t": "welcome", "name": "Hänne ÅÄÖ", "state": {"mixer/%d/vol" % i: i / 7 for i in range(50)}}
        for direction in (wire.TO_FL, wire.FROM_FL):
            events = wire.Encoder(direction).message(msg)
            for event in events:
                status, data1, data2 = wire.unpack(event)
                self.assertEqual(status & 0xF0, 0xE0)
                self.assertEqual(event, status | data1 << 8 | data2 << 16)  # nothing lost in 7 bits
            self.assertEqual(decode_all(events, direction), [msg])

    def test_every_event_delivered_twice_still_decodes_once(self):
        encoder, rng = wire.Encoder(), random.Random(1)
        events = [e for _ in range(20) for e in encoder.message(MSG)]  # wraps the 7-bit sequence
        doubled, late = [], []
        for event in events:
            doubled.append(event)
            late.append(event)
            while len(late) > rng.randint(0, 3):
                doubled.append(late.pop(0))
        doubled.extend(late)
        self.assertEqual(decode_all(doubled), [MSG] * 20)

    def test_a_lost_event_drops_only_that_message(self):
        encoder = wire.Encoder()
        first, second, third = encoder.message({"n": 1}), encoder.message({"n": 2}), encoder.message({"n": 3})
        self.assertEqual(decode_all(first + second[:3] + second[4:] + third), [{"n": 1}, {"n": 3}])

    def test_checksum_catches_a_corrupted_byte(self):
        events = wire.Encoder().message({"n": 1})
        status, data1, data2 = wire.unpack(events[3])
        events[3] = status | ((data1 ^ 1) << 8) | (data2 << 16)
        self.assertEqual(decode_all(events), [])

    def test_a_restarted_sender_is_recognised_by_its_epoch(self):
        decoder = wire.Decoder(wire.TO_FL)
        old = wire.Encoder(epoch=1)
        for e in [old.heartbeat()] + old.message({"n": 0}):
            decoder.feed(*wire.unpack(e))
        # The new sender's numbers start at 0, which looks like copies of old
        # events; its heartbeat's new epoch tells the decoder to start over.
        new = wire.Encoder(epoch=2)
        results = [decoder.feed(*wire.unpack(e)) for e in [new.heartbeat()] + new.message({"n": 1})]
        self.assertEqual(results[-1], {"n": 1})

    def test_copies_up_to_the_duplicate_window_behind_are_dropped(self):
        # A copy 8 behind: 7 newer events came between it and the original.
        self.assertEqual(wire.DUPLICATE_WINDOW, 8)
        encoder = wire.Encoder()
        events = [e for n in range(12) for e in encoder.message({"n": n})]
        late = list(events)
        for pos in range(len(events) - 1, -1, -1):  # every event once more, after the 7 that follow it
            late.insert(min(pos + 8, len(late)), events[pos])
        self.assertEqual(decode_all(late), [{"n": n} for n in range(12)])

    def test_after_many_lost_events_only_the_messages_they_were_part_of_are_lost(self):
        # FL stalls and the MIDI driver drops what comes meanwhile: the events
        # after the hole are new ones, however far ahead they are numbered.
        for receiver in ("bridge", "fl"):
            for lost in (1, 5, 27, 28, 40, 63, 100, 119):
                with self.subTest(receiver=receiver, lost=lost):
                    encoder = wire.Encoder(wire.TO_FL, epoch=5)
                    events, spans = [encoder.heartbeat()], []
                    for n in range(30):
                        start = len(events)
                        events += encoder.message({"t": "sets", "kv": [["m/%d/v" % n, 0.5]]})
                        spans.append((start, len(events)))
                        events.append(encoder.heartbeat())
                    cut = spans[0][1] + 1  # right after the first message and its heartbeat
                    hole = range(cut, cut + lost)
                    kept = [e for pos, e in enumerate(events) if pos not in hole]
                    whole = [n for n, (start, end) in enumerate(spans) if end <= cut or start >= cut + lost]
                    got = self.decode_with(receiver, kept)
                    self.assertEqual([msg["kv"][0][0] for msg in got], ["m/%d/v" % n for n in whole])

    def test_both_sides_take_the_same_events_for_copies(self):
        fl = FakeFL("A")
        try:
            self.assertEqual(fl.script.flsync_link.DUPLICATE_WINDOW, wire.DUPLICATE_WINDOW)
        finally:
            fl.close()

    def decode_with(self, receiver, events):
        if receiver == "bridge":
            return decode_all(events)
        fl = FakeFL("A")
        try:
            link = fl.script.flsync_link.MidiLink()
            for event in events:
                link.receive(*wire.unpack(event), now=0.0)
            return link.poll()
        finally:
            fl.close()

    def test_fl_starts_afresh_after_the_bridge_was_gone(self):
        # A restarted bridge may pick the epoch the last one had (1 in 128), and
        # its numbers start at 0 again: after the link timed out nothing it
        # sends may count as a copy of what the old one sent.
        fl = FakeFL("A")
        try:
            module = fl.script.flsync_link
            link = module.MidiLink()
            old = wire.Encoder(wire.TO_FL, epoch=5)
            for _ in range(3):
                link.receive(*wire.unpack(old.heartbeat()), now=0.0)  # the next would be number 3
            self.assertTrue(link.alive(1.0))
            later = module.LINK_TIMEOUT + 1.0
            self.assertFalse(link.alive(later))
            new = wire.Encoder(wire.TO_FL, epoch=5)
            for event in [new.heartbeat()] + new.message({"n": 1}):
                link.receive(*wire.unpack(event), now=later)
            self.assertEqual(link.poll(), [{"n": 1}])
        finally:
            fl.close()

    def test_late_copies_of_heartbeats_dont_restart_the_stream(self):
        encoder = wire.Encoder(epoch=5)
        heartbeat = encoder.heartbeat()
        message = encoder.message({"n": 1})
        events = [heartbeat] + message[:4] + [heartbeat] + message[4:]  # a late copy mid-message
        self.assertEqual(decode_all(events), [{"n": 1}])

    def test_each_side_ignores_the_other_direction(self):
        self.assertEqual(decode_all(wire.Encoder(wire.TO_FL).message(MSG), wire.FROM_FL), [])
        self.assertIsNone(wire.Decoder(wire.TO_FL).feed(0x90, 60, 100))  # a note from elsewhere

    def test_fl_script_and_bridge_agree(self):
        fl = FakeFL("A")
        try:
            link = fl.script.flsync_link.MidiLink()
            # FL -> bridge
            sent = [link.event(kind, data1) for kind, data1 in fl.script.flsync_link.wire_parts(MSG)]
            self.assertEqual(sent, wire.Encoder(wire.FROM_FL).message(MSG))
            # bridge -> FL, every event twice
            kinds = []
            for event in wire.Encoder(wire.TO_FL).message(MSG):
                kinds.append(link.receive(*wire.unpack(event), now=0.0))
                kinds.append(link.receive(*wire.unpack(event), now=0.0))
            self.assertEqual(kinds.count("message"), 1)
            self.assertEqual(link.poll(), [MSG])
            # FL's own events coming back on the shared port are ignored
            for event in sent:
                self.assertIsNone(link.receive(*wire.unpack(event), now=2.0))
            self.assertEqual(link.last_heard, 0.0)
        finally:
            fl.close()


if __name__ == "__main__":
    unittest.main()
