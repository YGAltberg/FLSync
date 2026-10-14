"""The piano roll scripts in fl/Piano roll scripts/, run against a fake
`flpianoroll` module and a temp exchange folder (docs/protocol_v2.md, "Notes")."""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

from fake_fl import ROOT

SCRIPTS = os.path.join(ROOT, "fl", "Piano roll scripts")
SEND = os.path.join(SCRIPTS, "FLSync skicka noter.pyscript")
FETCH = os.path.join(SCRIPTS, "FLSync hämta noter.pyscript")


class Note:
    """Like flpianoroll.Note, with FL's defaults."""

    def __init__(self):
        self.number, self.time, self.length, self.group = 60, 0, 0, 0
        self.pan, self.velocity, self.release, self.color = 0.5, 0.8, 0.5, 0
        self.fcut, self.fres, self.pitchofs, self.repeats = 0.5, 0.5, 0, 0
        self.slide = self.porta = self.muted = self.selected = False

    def clone(self):
        copy = Note()
        copy.__dict__.update(self.__dict__)
        return copy


class Score:
    def __init__(self, ppq=96):
        self.PPQ = ppq
        self.notes = []

    @property
    def noteCount(self):
        return len(self.notes)

    def getNote(self, index):
        return self.notes[index]

    def addNote(self, note):
        self.notes.append(note)

    def deleteNote(self, index):
        del self.notes[index]

    def clearNotes(self, all=False):
        self.notes = [] if all else [n for n in self.notes if not n.selected]


class Utils:
    def __init__(self):
        self.messages, self.logs = [], []

    def ShowMessage(self, text):
        self.messages.append(text)

    def log(self, text):
        self.logs.append(text)


def note(time_, length, number, velocity=0.8, **more):
    n = Note()
    n.time, n.length, n.number, n.velocity = time_, length, number, velocity
    n.__dict__.update(more)
    return n


class PianoRollScriptTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="flsync-pr-")
        self.folder = os.path.join(self.base, "Piano roll scripts", "FLSync")
        os.makedirs(self.folder)
        self.old_env = os.environ.get("FLSYNC_NOTES_DIR")
        os.environ["FLSYNC_NOTES_DIR"] = self.folder  # (in FL: the scripts' own folder)

    def tearDown(self):
        if self.old_env is None:
            del os.environ["FLSYNC_NOTES_DIR"]
        else:
            os.environ["FLSYNC_NOTES_DIR"] = self.old_env
        sys.modules.pop("flpianoroll", None)
        shutil.rmtree(self.base, ignore_errors=True)

    def run_script(self, path, score):
        """Run a .pyscript the way FL does, with `score` in its piano roll."""
        module = types.ModuleType("flpianoroll")
        module.score, module.Note, module.Utils = score, Note, Utils()
        module.__all__ = ["score", "Note", "Utils"]
        sys.modules["flpianoroll"] = module
        with open(path, encoding="utf-8") as f:
            code = compile(f.read(), path, "exec")
        exec(code, {"__name__": "__main__", "__file__": path})
        return module.Utils

    def write_context(self, session=None, age=0.0, pattern=2, channel=4):
        with open(os.path.join(self.folder, "context.json"), "w", encoding="utf-8") as f:
            json.dump({"pattern": pattern, "channel": channel, "name": "FLEX Bass", "session": session,
                       "time": time.time() - age}, f)

    def outbox(self):
        with open(os.path.join(self.folder, "outbox.json"), encoding="utf-8") as f:
            return json.load(f)

    # --- sending ------------------------------------------------------------------------

    def test_send_writes_every_note_for_the_bridge(self):
        self.write_context()
        score = Score(ppq=96)
        score.addNote(note(0, 48, 60, velocity=0.5, slide=True, pitchofs=-20))
        score.addNote(note(96, 24, 67, muted=True, repeats=3, color=5))
        utils = self.run_script(SEND, score)
        self.assertEqual(utils.messages, [])
        out = self.outbox()
        self.assertEqual((out["pattern"], out["channel"], out["ppq"]), (2, 4, 96))
        self.assertEqual(out["notes"], [[0, 48, 60, 0.5, 0.5, 0.5, 0, 0.5, 0.5, -20, 1, 0, 0, 0],
                                        [96, 24, 67, 0.8, 0.5, 0.5, 5, 0.5, 0.5, 0, 0, 0, 1, 3]])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "outbox.json.tmp")))

    def test_send_without_a_running_bridge_says_so(self):
        for age in (None, 120.0):  # no context.json at all, or one the bridge stopped updating
            if age is not None:
                self.write_context(age=age)
            utils = self.run_script(SEND, Score())
            self.assertEqual(len(utils.messages), 1)
            self.assertIn("bryggan (bridge.py) är inte igång", utils.messages[0])
            self.assertFalse(os.path.exists(os.path.join(self.folder, "outbox.json")))

    # --- fetching -----------------------------------------------------------------------

    def test_fetch_replaces_the_notes_and_scales_the_timebase(self):
        rows = [[0, 48, 60, 0.5, 0.25, 0.5, 2, 0.5, 0.5, 10, 0, 1, 0, 0],
                [96, 24, 67, 0.75, 0.5, 0.5, 0, 0.5, 0.5, 0, 0, 0, 1, 2]]
        self.write_context(session={"v": 3, "by": "Kim", "ppq": 96, "notes": rows})
        score = Score(ppq=192)
        score.addNote(note(10, 10, 40))
        utils = self.run_script(FETCH, score)
        self.assertEqual(utils.messages, [])
        self.assertEqual([(n.time, n.length, n.number, n.velocity, n.pan, n.color, n.pitchofs, n.porta, n.muted,
                           n.repeats) for n in score.notes],
                         [(0, 96, 60, 0.5, 0.25, 2, 10, True, False, 0), (192, 48, 67, 0.75, 0.5, 0, 0, False, True, 2)])
        self.assertEqual(utils.logs, ["FLSync: hämtade 2 noter från Kim"])

    def test_fetch_with_nothing_to_fetch_leaves_the_notes_alone(self):
        self.write_context(session=None)
        score = Score()
        score.addNote(note(0, 24, 50))
        utils = self.run_script(FETCH, score)
        self.assertEqual(len(score.notes), 1)
        self.assertIn("ingen har skickat noter till FLEX Bass i mönster 2", utils.messages[0])

    def test_fetch_ignores_broken_data(self):
        self.write_context(session={"v": 1, "by": "Kim", "ppq": 96, "notes": [[1, 2, 3]]})
        score = Score()
        score.addNote(note(0, 24, 50))
        utils = self.run_script(FETCH, score)
        self.assertEqual(len(score.notes), 1)
        self.assertIn("gick inte att läsa", utils.messages[0])

    def test_fetch_survives_a_broken_timebase_or_session(self):
        row = [0, 48, 60, 0.5, 0.25, 0.5, 2, 0.5, 0.5, 10, 0, 1, 0, 0]
        for session in ({"v": 1, "by": "Kim", "ppq": "fel", "notes": [row]},
                        {"v": 1, "by": "Kim", "ppq": [96], "notes": [row]},
                        {"v": 1, "by": "Kim", "ppq": float("nan"), "notes": [row]},
                        ["inte", "en", "session"], "nonsens"):
            with self.subTest(session=session):
                self.write_context(session=session)
                score = Score()
                score.addNote(note(0, 24, 50))
                utils = self.run_script(FETCH, score)
                self.assertEqual(len(score.notes), 1)
                self.assertEqual(len(utils.messages), 1)
                self.assertIn("gick inte att läsa", utils.messages[0])

    def test_send_with_a_context_that_lacks_the_channel_says_so(self):
        with open(os.path.join(self.folder, "context.json"), "w", encoding="utf-8") as f:
            json.dump({"pattern": 2, "name": "FLEX Bass", "session": None, "time": time.time()}, f)
        score = Score()
        score.addNote(note(0, 24, 50))
        utils = self.run_script(SEND, score)
        self.assertEqual(len(utils.messages), 1)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "outbox.json")))

    def test_fetch_keeps_every_field_in_fl_range(self):
        rows = [[-50, 0, 300, 2.5, -1, 9, 99, 7, -3, 999, 1, 1, 1, 99],
                [1e15, 1e15, -5, -0.5, 0.5, 0.5, -2, 0.5, 0.5, -999, 0, 0, 0, -1]]
        self.write_context(session={"v": 1, "by": "Kim", "ppq": 96, "notes": rows})
        score = Score(ppq=96)
        utils = self.run_script(FETCH, score)
        self.assertEqual(utils.messages, [])
        self.assertEqual([(n.time, n.length, n.number, n.velocity, n.pan, n.release, n.color, n.fcut, n.fres,
                           n.pitchofs, n.repeats) for n in score.notes],
                         [(0, 1, 131, 1.0, 0.0, 1.0, 15, 1.0, 0.0, 120, 14),
                          (2 ** 31 - 1, 2 ** 31 - 1, 0, 0.0, 0.5, 0.5, 0, 0.5, 0.5, -120, 0)])

    def test_round_trip(self):
        self.write_context()
        sender = Score(ppq=96)
        for i in range(16):
            sender.addNote(note(i * 24, 12, 48 + i, velocity=i / 16, pan=0.25, fcut=0.75))
        self.run_script(SEND, sender)
        out = self.outbox()
        self.write_context(session={"v": 1, "by": "Kim", "ppq": out["ppq"], "notes": out["notes"]})
        receiver = Score(ppq=96)
        self.run_script(FETCH, receiver)
        fields = ("time", "length", "number", "velocity", "pan", "release", "color", "fcut", "fres", "pitchofs",
                  "slide", "porta", "muted", "repeats")
        self.assertEqual([[getattr(n, f) for f in fields] for n in receiver.notes],
                         [[getattr(n, f) for f in fields] for n in sender.notes])


if __name__ == "__main__":
    unittest.main()
