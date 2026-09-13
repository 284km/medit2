#!/usr/bin/env python3
"""bench/latency.py — how long the EDITOR takes to answer a keystroke.

`bench/big.sh` measures the pieces: the index builds in 676 ms, a search runs
at 7.2 GB/s. None of that is what a person feels. What they feel is the gap
between pressing a key and the screen changing, and that number has never been
measured here.

So this drives the real binary over a real pty and times the frames:

    startup    exec to the first frame that shows the file
    type       a printable character, at the TOP of the file and at the BOTTOM
    down       one line of movement
    pagedown   a screen of movement
    search     Ctrl-F, a needle, Enter -- reported separately for a hit near
               the start and for a miss, which scan the whole file

Reported as a median and a worst case over N repeats, because an editor is
judged by its worst frame and an average hides it.

WHY THE TOP AND THE BOTTOM. Editing at the top of the file moves every line
start after the cursor; editing at the bottom moves none. They are the two ends
of the one operation this design trades a rebuild for, and quoting one without
the other quotes the half that flatters.

Usage:
    python3 bench/latency.py ./medit2 FILE [repeats]

Needs a `mere` on PATH only if FILE is a .mere small enough for a language
server; otherwise the editor starts none.
"""

import os
import pty
import select
import struct
import sys
import fcntl
import re
import termios
import time

ROWS, COLS = 24, 80
# `ESC [ <row> ; <col> H` at the very end of a frame.
FRAME_END = re.compile(rb"\x1b\[[0-9]+;[0-9]+H\s*$")
ESC = b"\x1b"
CTRL_F = b"\x06"
CTRL_Q = b"\x11"
ENTER = b"\r"
DOWN = b"\x1b[B"


class Sub:
    def __init__(self, argv, amb_cols=1):
        self.amb_cols = amb_cols
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execv(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.raw = b""
        self.pending = b""


    # The editor asks the terminal how wide an ambiguous-width character was
    # drawn (`ESC[6n`) and waits up to 120 ms for the answer. A harness that
    # stays silent makes every startup pay that wait AND leaves the reply
    # parser -- which decides whether Japanese punctuation is one column or
    # two -- with no test at all.
    #
    # `amb_cols` is what this pretend terminal claims: 1 for a Western
    # terminal, 2 for one configured for CJK. The cursor lands one past the
    # character it drew.
    def _answer_cpr(self, chunk):
        if b"\x1b[6n" not in chunk:
            return
        for _ in range(chunk.count(b"\x1b[6n")):
            try:
                os.write(self.fd, b"\x1b[1;%dR" % (1 + self.amb_cols))
            except OSError:
                return

    def _read_frames(self, deadline):
        """Read what is there; return the last COMPLETE frame seen, or None.

        A frame is complete when the editor has drawn its status bar and put
        the cursor back -- the last thing `render` emits is a cursor-position
        escape, so a frame ends at `ESC [ <n> ; <n> H`.
        """
        last = None
        while time.time() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.002)
            if not r:
                break
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self._answer_cpr(chunk)
            self.pending += chunk
            self.raw += chunk
        # Frames start at ESC[H -- the editor repaints from home every time --
        # and end at the cursor placement after the status bar.
        parts = self.pending.split(b"\x1b[H")
        if len(parts) <= 1:
            return None
        frames = parts[1:]

        # A frame ends at the cursor placement `render` emits last. NOT at the
        # status bar's reverse-video close: a PROMPT replaces the status line
        # and draws it plain, so a completeness test that looked for `ESC[0m`
        # saw every ordinary frame and no prompt frame at all -- Ctrl-G timed
        # out after thirty seconds while the editor sat there with the prompt
        # open and drawn.
        def complete(p):
            return FRAME_END.search(p) is not None

        for p in frames:
            if complete(p):
                last = p
        # WHAT IS CONSUMED HAS TO LEAVE. Keeping the tail unconditionally meant
        # a complete frame was re-parsed and returned by every later call, so
        # `settle` was handed the same frame forever and ran to its cap. Only
        # an INCOMPLETE tail is carried over.
        self.pending = b"\x1b[H" + frames[-1] if not complete(frames[-1]) else b""
        return last

    def first_frame(self, timeout=120.0):
        """Block until the editor has drawn ANYTHING.

        `settle` cannot do this: it drains what is pending and returns, so
        called while the editor is still building its line index it returns
        None instantly. Every later measurement then started its clock before
        the editor had finished starting up, and the first keystroke was billed
        for the rest of the open -- 823 ms at 1 GB, for a Down arrow, on an
        editor whose steady state is 3.5 ms.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            f = self._read_frames(t0 + timeout)
            if f is not None:
                return (time.time() - t0) * 1000.0, f
        raise TimeoutError("the editor drew nothing")

    def settle(self, cap=5.0):
        """Drain what is pending and return the last frame drawn.

        NOT a wait for quiet. THE EDITOR REPAINTS ON A TIMER: its loop waits
        250 ms for input and draws at the top of every iteration, so frames
        keep arriving with nothing happening and a "wait until it goes quiet"
        never returns. (That is a finding about the editor, not about this
        script -- an idle editor on a 1 GB file re-renders four times a second
        for nothing.)

        It also means "how long until a frame arrives" is not a measurement of
        anything: the first version of this read 0.0 ms for every keystroke,
        because a frame drawn BEFORE the key was sent was still in the pty
        buffer. `timed` waits for a frame that DIFFERS instead.
        """
        end = time.time() + cap
        last = None
        while time.time() < end:
            f = self._read_frames(end)
            if f is None:
                break
            last = f
        return last

    def timed(self, keys, timeout=30.0, what=""):
        """Send `keys` and time until the screen CHANGES.

        Waiting for a frame is not enough (see settle); waiting for a DIFFERENT
        frame is, because the idle repaint draws the same screen and the answer
        to a keystroke does not.
        """
        before = self.settle()
        t0 = time.time()
        self.send(keys)
        deadline = t0 + timeout
        while time.time() < deadline:
            f = self._read_frames(deadline)
            if f is not None and f != before:
                return (time.time() - t0) * 1000.0, f
        # A measurement that never saw the screen change has to name itself.
        # Returning silently would put a timeout into the numbers as if it were
        # a latency.
        raise TimeoutError(f"no frame changed within {timeout}s after {what or keys!r}")

    def send(self, data):
        os.write(self.fd, data)

    def close(self):
        try:
            os.write(self.fd, CTRL_Q)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass


def stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2], xs[-1]


def report(name, xs):
    med, worst = stats(xs)
    # 16 ms is one frame at 60 Hz and 100 ms is the threshold above which an
    # action stops feeling like a direct response. Both are marked so the
    # number is read against something rather than admired on its own.
    mark = "" if worst < 16 else ("  (over one frame)" if worst < 100 else "  (OVER 100 ms)")
    print(f"{name:<22} median {med:8.1f} ms   worst {worst:8.1f} ms{mark}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    binary = os.path.abspath(sys.argv[1])
    path = sys.argv[2]
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    size = os.path.getsize(path)
    print(f"== {path}  {size/1e6:.0f} MB, {reps} repeats")

    # --- startup ------------------------------------------------------------
    # A fresh process each time. The page cache is warm after the first, which
    # is the honest case: a file you are about to edit was just looked at.
    starts = []
    for _ in range(3):
        s = Sub([binary, path])
        ms, _ = s.first_frame()
        starts.append(ms)
        s.close()
    report("startup", starts)

    s = Sub([binary, path])
    s.first_frame()
    s.settle()

    # --- typing at the top --------------------------------------------------
    # Every line start after the cursor moves. This is the worst case for the
    # incremental index and the one a benchmark is tempted to leave out.
    top = [s.timed(b"x")[0] for _ in range(reps)]
    report("type (top of file)", top)
    sys.stdout.flush()

    # --- movement -----------------------------------------------------------
    downs = [s.timed(DOWN)[0] for _ in range(reps)]
    report("one line down", downs)

    pages = [s.timed(DOWN * (ROWS - 1))[0] for _ in range(reps)]
    report("a screen down", pages)

    # --- typing near the end ------------------------------------------------
    # Reached with Ctrl-G rather than by holding Down: the point is to measure
    # the edit, not the trip.
    s.timed(b"\x07")                     # Ctrl-G, go to line
    s.timed(b"999999999", what="a line number")
    s.timed(ENTER, timeout=60, what="accept the line number")
    bottom = [s.timed(b"y", what="type at the end")[0] for _ in range(reps)]
    report("type (end of file)", bottom)

    # --- search -------------------------------------------------------------
    # Two needles: one whose first hit is at the cursor, and one that is
    # nowhere, which has to read the whole file before it can say so.
    #
    # THE HIT HAS TO BE PLANTED. The first version searched for "y" and read
    # the same time as the miss, because these files are one repeated
    # character and the only `y` in them was the one the previous phase typed
    # at the END. It was measuring a whole-file scan twice and calling one of
    # them a hit.
    s.timed(b"\x07", what="Ctrl-G")
    s.timed(b"1")
    s.timed(ENTER, timeout=60, what="back to line 1")
    s.timed(b"ZQ7", what="plant a needle")
    # And back to the start, so the needle is AHEAD of the cursor. Planting it
    # leaves the cursor just past it, and a search from there misses, wraps,
    # and reads the whole file -- which is what the previous version of this
    # measured while calling it a hit.
    s.timed(b"\x07", what="Ctrl-G")
    s.timed(b"1")
    s.timed(ENTER, timeout=60, what="back to the needle")

    hits = []
    for _ in range(3):
        s.timed(CTRL_F, what="open search")
        hits.append(s.timed(b"ZQ7", timeout=60, what="search for a planted hit")[0])
        s.timed(ESC, what="close search")
    report("search, hit at cursor", hits)

    misses = []
    for _ in range(3):
        s.timed(CTRL_F, what="open search")
        misses.append(s.timed(b"zzq-no-such-needle", timeout=120, what="search for a miss")[0])
        s.timed(ESC, what="close search")
    report("search, whole-file miss", misses)

    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
