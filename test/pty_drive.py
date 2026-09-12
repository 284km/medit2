#!/usr/bin/env python3
"""test/pty_drive.py — drive medit2 through a real pty.

A pipe is not a terminal, and the difference is exactly where a full-screen
editor's bugs live: tty_raw is a no-op off-tty, term_rows/term_cols answer -1,
and IXON never matters because there is no line discipline to freeze. Piped
tests pass while the real thing is unusable.

So this opens a pty, sets its size, sends keystrokes, and reads back what the
editor drew. What it asserts is the FILE and the visible text, never the exact
escape bytes -- those are the editor's business and change whenever the redraw
does.

Usage:  python3 test/pty_drive.py ./medit2
"""

import os
import pty
import re
import select
import struct
import sys
import fcntl
import shutil
import termios
import time

ROWS, COLS = 24, 80

# Keys, by the byte the terminal actually sends.
CTRL_S = b"\x13"
CTRL_Q = b"\x11"
CTRL_Z = b"\x1a"
BACKSPACE = b"\x7f"
ENTER = b"\r"
UP, DOWN, RIGHT, LEFT = b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"

ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def visible(raw: bytes) -> str:
    """What a person would see: escape sequences removed, nothing else."""
    return ANSI.sub(b"", raw).decode("utf-8", "replace")


class Session:
    def __init__(self, argv):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execv(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.raw = b""
        self.drain(1.5)

    def drain(self, seconds=0.2, quiet=0.08):
        """Read until the editor stops drawing, or `seconds` elapse.

        Waiting a fixed time makes a harness that is slow when it passes and
        flaky when the machine is busy. Waiting for QUIESCENCE -- nothing new
        for `quiet` seconds -- is both faster and steadier, and the cap is still
        there so a hung editor fails the test instead of hanging it.
        """
        deadline = time.time() + seconds
        last = time.time()
        while time.time() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.02)
            if not r:
                if time.time() - last >= quiet:
                    break
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.raw += chunk
            last = time.time()
        return self.raw

    def send(self, data: bytes, settle=1.5):
        os.write(self.fd, data)
        self.drain(settle)

    def wait_for(self, needle, timeout=3.0):
        """Drain until `needle` appears in the visible output, or give up.

        Waiting for TIME and then asserting is how a harness becomes flaky: the
        assertion is really "the editor had drawn this within N milliseconds",
        which is a statement about the machine's mood. Waiting for the THING
        makes the test say what it means, and the timeout still turns a hung
        editor into a failure rather than a hang.
        """
        end = time.time() + timeout
        while time.time() < end:
            if needle in visible(self.raw):
                return True
            self.drain(0.2, quiet=0.05)
        return needle in visible(self.raw)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass


FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


def check_in(name, needle, haystack):
    if needle not in haystack:
        FAILS.append(f"{name}: {needle!r} not in output")


def scenario_edit_and_save(binary):
    path = "/tmp/medit2_pty_edit.txt"
    with open(path, "w") as f:
        f.write("alpha\nbeta\ngamma\n")

    s = Session([binary, path])

    # The editor drew something, and it knows the pty's size: the status line
    # reports four lines (three plus the empty one after the final newline).
    check("status shows the file", s.wait_for("medit2_pty_edit.txt"), True)
    check("status shows the line count", s.wait_for("L1/4"), True)

    # Type at the head, move down two lines, type again. The cursor keeps its
    # GOAL COLUMN across a vertical move (column 4, after "ONE "), which is what
    # every editor does -- so the second insert lands at column 4 of "gamma",
    # not at its start. Asserting the start here would be asserting a bug.
    s.send(b"ONE ")
    s.send(DOWN + DOWN)
    s.send(b"TWO ")

    # Ctrl-S is XOFF unless IXON was cleared. If it was not, the terminal stops
    # accepting output here and everything after this is silence -- which is
    # exactly the bug this scenario exists to catch.
    before = len(s.raw)
    s.send(CTRL_S)
    grew = len(s.raw) > before
    check("terminal still draws after Ctrl-S (IXON cleared)", grew, True)

    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("file was saved with both edits", got, "ONE alpha\nbeta\ngammTWO a\n")


def scenario_undo(binary):
    path = "/tmp/medit2_pty_undo.txt"
    with open(path, "w") as f:
        f.write("keep\n")

    s = Session([binary, path])
    s.send(b"XY")
    s.send(CTRL_Z)          # undo the Y
    s.send(CTRL_Z)          # undo the X
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("undo walked both edits back", got, "keep\n")


def scenario_backspace_and_enter(binary):
    path = "/tmp/medit2_pty_keys.txt"
    with open(path, "w") as f:
        f.write("ab\n")

    s = Session([binary, path])
    s.send(RIGHT + RIGHT)   # after "ab"
    s.send(BACKSPACE)       # -> "a"
    s.send(ENTER)           # -> "a\n"
    s.send(b"c")            # -> "a\nc"
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("backspace and enter", got, "a\nc\n")


def scenario_japanese(binary):
    """The one a piped test cannot check: columns, not bytes.

    "日本語" is 9 bytes and 6 columns. An editor that counts bytes reports
    column 10 after three characters and puts the cursor in the wrong place on
    every line that contains one -- which is most lines, here.
    """
    path = "/tmp/medit2_pty_jp.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("日本語abc\n")

    s = Session([binary, path])

    # Three RIGHTs must cross three CHARACTERS (nine bytes), not three bytes.
    # Byte-wise movement would land inside 日 and every slice after it is
    # mojibake.
    s.send(RIGHT + RIGHT + RIGHT)
    check("cursor is at column 7 after three wide characters", s.wait_for("C7"), True)

    # Backspace removes a whole character: deleting one byte of a three-byte
    # kanji leaves two bytes that are not any character at all.
    s.send(BACKSPACE)
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path, encoding="utf-8") as f:
        got = f.read()
    check("backspace removed one kanji, not one byte", got, "日本abc\n")


def scenario_japanese_clip(binary):
    """A line wider than the terminal is clipped by COLUMNS.

    Clipping by bytes puts half a kanji at the right edge, and half a kanji is
    not something a terminal can draw -- it corrupts the rest of the screen.
    """
    path = "/tmp/medit2_pty_jp_wide.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("あ" * 100 + "\n")

    s = Session([binary, path])
    s.wait_for("L1/2")          # the status line means the first frame is done
    vis = visible(s.raw)
    # The first drawn row holds as many whole あ as fit in 80 columns: 40 of
    # them, and no partial character anywhere.
    first_row = vis.split("\r\n")[0].lstrip()
    check("a wide line is clipped to whole characters", first_row, "あ" * 40)
    check("no replacement characters in the output", "\ufffd" in vis, False)
    s.send(CTRL_Q)
    s.close()


def scenario_lsp(binary):
    """The language server, over a socketpair, in the same poll(2) as the keyboard.

    Needs `mere` on PATH -- that is what the editor spawns. Skipped rather than
    failed when it is absent, because a missing toolchain is not a bug in the
    editor; but the skip is PRINTED, so an empty run cannot look like a pass.
    """
    if not shutil.which("mere"):
        print("SKIP lsp: no `mere` on PATH (the editor spawns `mere lsp`)")
        return

    path = "/tmp/medit2_pty_lsp.mere"
    # A deliberate parse error on line 2 (the `+` has no right operand), and a
    # STRING LITERAL on line 1 that the editor has to escape on the way out.
    #
    # The quote is the discriminating part, and that was checked rather than
    # assumed: with the escaping removed, a file of plain text still round-trips
    # because `mere lsp`'s JSON parser tolerates a literal newline inside a
    # string. A literal `"` it cannot tolerate -- it ends the string early and
    # the message is malformed however lenient the parser is. Without this
    # character the scenario passes with the escaping deleted, which is a test
    # that tests nothing.
    with open(path, "w") as f:
        f.write('let greet = "hi" in\nlet y = 1 +\nprint greet\n')

    s = Session([binary, path])

    # The server has to start, handshake, and answer -- so this waits for the
    # diagnostic rather than for a duration.
    got = s.wait_for("parse error", timeout=8.0)
    check("the server's diagnostic reaches the status bar", got, True)

    # Now fix it, and watch the diagnostic clear. This exercises the OTHER
    # direction: the editor's didChange has to reach the server, which means
    # the escaping and the framing both have to be right.
    #
    # The cursor starts at the top of the file; move to the end of line 2 and
    # complete the expression.
    s.send(DOWN)
    for _ in range(11):
        s.send(RIGHT, settle=0.2)
    s.send(b" 1 in")

    # didChange is sent when the editor goes idle, so this waits through at
    # least one idle tick.
    cleared = s.wait_for("lsp ok", timeout=8.0)
    check("fixing the file clears the diagnostic", cleared, True)

    s.send(CTRL_Q)
    s.close()


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "./medit2"
    binary = os.path.abspath(binary)
    scenario_edit_and_save(binary)
    scenario_undo(binary)
    scenario_backspace_and_enter(binary)
    scenario_japanese(binary)
    scenario_japanese_clip(binary)
    scenario_lsp(binary)

    for f in FAILS:
        print("FAIL " + f)
    print("pty: all ok" if not FAILS else f"pty: {len(FAILS)} FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
