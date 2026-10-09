"""FLSync's installer in FLSync's own window: setup/setup.py's Setup, run on a
worker thread, talking through the page instead of a console.

    run = SetupRun("install", root)   # or "uninstall"; root: the FLSync to install
    run.look()                        # what is there now, for the page's first view
    run.begin()                       # the installer starts
    run.view()                        # {state, lines, question, ...} for the page
    run.answer(True)                  # the page's Ja/Nej to a question
    run.cancel()                      # Avbryt while it waits for FL to close

state: "ready" (not started), "running", "asking" (a question waits for its
answer), "done" (code: 0 fine, 1 problems, 2 stopped before changing anything).
"""

import argparse
import re
import threading
import traceback

LINES_SHOWN = 400
QUESTION = re.compile(r"\s*\[(J/n|j/N)\]\s*\Z")


class SetupRun:
    def __init__(self, kind, root, setup_module=None, system=None, dry_run=False):
        if setup_module is None:
            import winsys
            setup_module = winsys._setup()
        self.kind = kind  # "install" or "uninstall"
        self.root = root
        self.mod = setup_module
        self.system = system or setup_module.System()
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.state = "ready"
        self.lines = []
        self.partial = ""
        self.question = None   # {"text", "default"} while asking
        self.reply = None
        self.replied = threading.Event()
        self.stop = threading.Event()
        self.code = None
        self.todo = []
        self.problems = []
        self.intro = None      # look()
        self.thread = None

    def _args(self, dry=None):
        return argparse.Namespace(dry_run=self.dry_run if dry is None else dry, uninstall=self.kind == "uninstall",
                                  yes=False, skip_loopmidi=False, skip_fl_config=False)

    def look(self):
        """What the first view says: is FLSync installed, this very version, and
        does FL run (it must be closed while FL's settings change)."""
        try:
            setup = self.mod.Setup(self.system, self._args(dry=True), root=self.root, out=lambda *_: None,
                                   window=True)
            app = setup.app_dir()
            present = bool(self.system.exists(app))
            intro = {"present": present, "same": present and bool(setup.program_same(app)),
                     "fl_running": bool(setup.fl_running()), "here": self.mod.same_path(self.root, app)}
        except Exception:
            intro = {"present": False, "same": False, "fl_running": False, "here": False}
        with self.lock:
            self.intro = intro
        return intro

    def begin(self):
        """Start the installer (once). False if it runs or ran already."""
        with self.lock:
            if self.state != "ready":
                return False
            self.state = "running"
        self.thread = threading.Thread(target=self._run, name="flsync-setup", daemon=True)
        self.thread.start()
        return True

    def _run(self):
        try:
            setup = self.mod.Setup(self.system, self._args(), root=self.root, out=self._out, ask=self._ask,
                                   interactive=True, window=True, cancelled=self.stop.is_set)
            code = setup.run()
            todo, problems = list(setup.todo), list(setup.problems)
        except Exception:
            self._out("Något oväntat gick fel:\n" + traceback.format_exc())
            code, todo, problems = 1, [], ["Något oväntat gick fel (se ovan)."]
        with self.lock:
            if self.partial:
                self.lines.append(self.partial)
                self.partial = ""
            self.code, self.todo, self.problems = code, todo, problems
            self.question = None
            self.state = "done"

    def _out(self, text="", end="\n"):
        with self.lock:
            parts = (self.partial + str(text) + end).split("\n")
            self.partial = parts.pop()
            self.lines.extend(parts)
            del self.lines[:-LINES_SHOWN * 2]

    def _ask(self, prompt):
        """Setup's question ("  Text? [J/n] "): the page shows it and this waits
        for its Ja/Nej (or Avbryt, which answers no)."""
        text = str(prompt)
        match = QUESTION.search(text)
        default = bool(match) and match.group(1) == "J/n"
        with self.lock:
            self.replied.clear()
            self.question = {"text": QUESTION.sub("", text).strip(), "default": default}
            self.state = "asking"
        while not self.replied.wait(0.5):
            if self.stop.is_set():
                break
        with self.lock:
            yes = bool(self.reply) if self.replied.is_set() else False
            self.question, self.reply = None, None
            if self.state == "asking":
                self.state = "running"
        return "j" if yes else "n"

    def answer(self, yes):
        with self.lock:
            if self.state != "asking":
                return False
            self.reply = bool(yes)
        self.replied.set()
        return True

    def cancel(self):
        """Avbryt: stops a wait for FL to close, and answers a question no."""
        self.stop.set()

    def done(self):
        return self.state == "done"

    def view(self):
        with self.lock:
            lines = self.lines[-LINES_SHOWN:] + ([self.partial] if self.partial else [])
            return {"kind": self.kind, "state": self.state, "lines": lines, "question": self.question,
                    "code": self.code, "todo": list(self.todo), "problems": list(self.problems),
                    "intro": self.intro, "dry_run": self.dry_run}
