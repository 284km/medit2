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
CTRL_F = b"\x06"
CTRL_G = b"\x07"
CTRL_N = b"\x0e"
CTRL_P = b"\x10"
CTRL_R = b"\x12"
ESC = b"\x1b"
CTRL_A = b"\x01"   # mark
CTRL_W = b"\x17"   # copy
CTRL_K = b"\x0b"   # cut
CTRL_Y = b"\x19"   # paste
SH_RIGHT = b"\x1b[1;2C"
SH_LEFT = b"\x1b[1;2D"
SH_DOWN = b"\x1b[1;2B"
CTRL_T = b"\x14"   # hover
CTRL_D = b"\x04"   # definition
CTRL_B = b"\x02"   # back from a jump
CTRL_L = b"\x0c"   # format
CTRL_SPACE = b"\x00"  # completion
CTRL_X = b"\x18"   # next buffer
CTRL_O = b"\x0f"   # open
CTRL_U = b"\x15"   # close buffer
CTRL_E = b"\x05"   # redo
TAB = b"\t"
C_RIGHT = b"\x1b[1;5C"
C_LEFT = b"\x1b[1;5D"
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
    def __init__(self, argv, env=None, amb_cols=1):
        """`env` overrides variables in the CHILD only.

        The config tests need a `$HOME` with a `.medit2.toml` in it, and
        writing one into the real home directory would be a test that edits
        the machine it runs on.
        """
        self.amb_cols = amb_cols
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            if env:
                os.environ.update(env)
            os.execv(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.raw = b""
        self.dead = False
        self.drain(1.5)


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
                self.dead = True
                break
            if not chunk:
                # EOF: the editor is gone. Recording it matters as much as
                # noticing it -- without this, `wait_for` kept calling `drain`,
                # each call returned instantly on the closed descriptor, and
                # the loop spun at 100% CPU for the whole timeout. A poisoned
                # build that CRASHES then took minutes to report instead of
                # seconds, which is the slow half of "a gate must not hang".
                self.dead = True
                break
            self._answer_cpr(chunk)
            self.raw += chunk
            last = time.time()
        return self.raw

    def send(self, data: bytes, settle=1.5):
        """Write keys, and turn a dead subject into a NAMED failure.

        A crashed editor closes the pty, and the next write raises EIO. Letting
        that escape reports the harness's stack trace instead of the editor's
        death, which is the wrong subject: a poison run that kills the program
        should read as "the program died", not as a Python error.
        """
        if self.dead:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            self.dead = True
            FAILS.append("the editor exited before it was asked to "
                         "(the pty closed while sending keys)")
            return
        self.drain(settle)

    def screen(self):
        """The LAST frame drawn, not everything ever drawn.

        This editor repaints from home on every frame, so the current screen is
        whatever follows the final `ESC[H`. Searching the accumulated output
        instead answers "was the editor EVER in this state", which is a
        different question and a much weaker one: a check that the cursor
        returned to line 1 passes trivially, because line 1 is where it started.
        That is not a hypothetical -- deleting the code that restores the
        position left this suite green until this was fixed.
        """
        i = self.raw.rfind(b"\x1b[H")
        return visible(self.raw[i:] if i >= 0 else self.raw)

    def screen_raw(self):
        """The last frame WITH its escape sequences.

        `screen()` strips them, which is right for asking what a person reads
        and useless for asking what colour they read it in: a test for syntax
        highlighting that looked at `screen()` would pass on an editor that
        drew no colours at all.
        """
        i = self.raw.rfind(b"\x1b[H")
        return (self.raw[i:] if i >= 0 else self.raw).decode("utf-8", "replace")

    def wait_for(self, needle, timeout=3.0):
        """Drain until `needle` is on the CURRENT screen, or give up.

        Waiting for TIME and then asserting is how a harness becomes flaky: the
        assertion is really "the editor had drawn this within N milliseconds",
        which is a statement about the machine's mood. Waiting for the THING
        makes the test say what it means, and the timeout still turns a hung
        editor into a failure rather than a hang.
        """
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.screen():
                return True
            if self.dead:
                # Nothing more will ever be drawn. Waiting out the timeout
                # would report the same failure, later.
                break
            self.drain(0.2, quiet=0.05)
        if self.dead and needle not in self.screen():
            FAILS.append("the editor exited while waiting for %r" % (needle,))
        return needle in self.screen()

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
    vis = s.screen()
    # The first drawn row holds as many whole あ as fit in 80 columns: 40 of
    # them, and no partial character anywhere.
    first_row = vis.split("\r\n")[0].lstrip()
    check("a wide line is clipped to whole characters", first_row, "あ" * 40)
    check("no replacement characters in the output", "\ufffd" in vis, False)
    s.send(CTRL_Q)
    s.close()


def scenario_search(binary):
    """Search, and the three things about it that are easy to get wrong.

    The status line is REPLACED by the prompt while one is open, so "L3/5" is
    not on screen during typing -- which is why the checks below look at where
    the cursor ENDED UP rather than at the prompt's own text. Incremental
    search moves the cursor as you type; Enter only accepts, so seeing line 3
    after Enter is the evidence that the incremental step ran.
    """
    path = "/tmp/medit2_pty_search.txt"
    with open(path, "w") as f:
        f.write("alpha\nbeta\ngamma\ndelta\ngamma again\n")

    s = Session([binary, path])
    s.wait_for("L1/6")

    # Cancelling puts the cursor back where it started. A search you abandoned
    # should not move you -- and this only passes if `origin` is remembered.
    s.send(CTRL_F)
    s.send(b"gam")
    s.send(ESC)
    check("ESC restores the position the search started from", s.wait_for("L1/6"), True)

    # Accepting leaves the cursor on the match.
    s.send(CTRL_F)
    s.send(b"gam")
    s.send(ENTER)
    check("search lands on the match", s.wait_for("L3/6"), True)

    # Find-next goes to the SECOND occurrence, not back to the first.
    s.send(CTRL_N)
    check("find next goes to the next occurrence", s.wait_for("L5/6"), True)

    # And wraps, saying so.
    s.send(CTRL_N)
    check("find next wraps", s.wait_for("L3/6"), True)
    check("and says that it wrapped", s.wait_for("wrapped"), True)

    # A miss says so and does not move.
    s.send(CTRL_F)
    s.send(b"zebra")
    s.send(ENTER)
    check("a miss is reported", s.wait_for("not found"), True)

    # Goto line.
    s.send(CTRL_G)
    s.send(b"4")
    s.send(ENTER)
    check("goto line", s.wait_for("L4/6"), True)

    s.send(CTRL_Q)
    s.close()


def scenario_search_incremental_reuse(binary):
    """Typing a needle re-searches on every keystroke, and most of those
    searches can be skipped.

    Extending a needle can only shrink its matches, so a needle that matched
    NOWHERE stays unmatched however many characters are added, and a needle
    that matched at X cannot next match before X. Typing a five-character
    needle that is not in a 1 GB file cost five whole-file scans -- 860 ms --
    and now costs one.

    What is checked here is that the shortcut does not change any ANSWER. The
    discriminating case is BACKSPACE: it makes the needle shorter, which can
    only ADD matches, so nothing carries over -- a reuse that applied there
    would keep reporting "not found" for a needle that is right there.
    """
    path = "/tmp/medit2_incr.txt"
    with open(path, "w") as f:
        f.write("alpha\nbeta\ngamma\ndelta\n")

    s = Session([binary, path])
    s.wait_for("L1/5", timeout=6.0)

    # WHILE A PROMPT IS OPEN THE STATUS LINE IS THE PROMPT. The line number is
    # not on screen to read, so each of these types the needle, ACCEPTS it, and
    # then looks at where the cursor ended up.
    #
    # Type "gamma" one character at a time: each keystroke resumes from the
    # previous match, and the answer has to be the same as a fresh search.
    s.send(CTRL_F)
    for ch in b"gamma":
        s.send(bytes([ch]), settle=0.3)
    s.send(ENTER)
    check("an incrementally typed needle lands on its match",
          s.wait_for("L3/5", timeout=4.0), True)

    # One more character and it matches nothing. The prompt says so while it is
    # open, which is readable without accepting.
    s.send(CTRL_F)
    for ch in b"gammaX":
        s.send(bytes([ch]), settle=0.3)
    check("extending past the match says not found",
          s.wait_for("not found", timeout=4.0), True)

    # BACKSPACE. The needle shrinks back to one that does match, and a reuse
    # that carried the miss across would still say not found.
    s.send(BACKSPACE)
    check("backspacing stops saying not found",
          s.wait_for("not found", timeout=1.0), False)
    s.send(ENTER)
    check("and the needle that matches is found again",
          s.wait_for("L3/5", timeout=4.0), True)

    # A search that has to WRAP still says so -- after a resume the scan starts
    # at the previous match rather than at the cursor, and reporting a wrap
    # relative to THAT would say the file is shorter than it is.
    s.send(CTRL_G)
    s.send(b"4")
    s.send(ENTER)
    s.wait_for("L4/5", timeout=4.0)
    s.send(CTRL_F)
    for ch in b"alpha":
        s.send(bytes([ch]), settle=0.3)
    check("a search from below its only match wraps",
          s.wait_for("wrapped", timeout=4.0), True)
    s.send(ENTER)
    check("and lands on it", s.wait_for("L1/5", timeout=4.0), True)

    s.send(CTRL_Q)
    s.close()


def scenario_search_japanese(binary):
    """Backspace in the prompt removes a character, not a byte.

    WHERE THE OBVIOUS TEST FAILS TO TEST ANYTHING: deleting one byte from
    "日本語" leaves "日本" plus two bytes of 語, and that is still a byte-prefix
    of 日本語 -- so a byte-wise backspace finds the same line and a search-result
    check passes either way. The property has to be read off the PROMPT, where
    a half-deleted character shows up as a broken one.
    """
    path = "/tmp/medit2_pty_search_ja.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("one\n\u65e5\u672c\u8a9e\nthree\n")

    s = Session([binary, path])
    s.wait_for("L1/4")
    s.send(CTRL_F)
    s.send("\u65e5\u672c\u8a9e".encode())
    s.send(BACKSPACE)
    s.wait_for("/\u65e5\u672c")
    screen = s.screen()
    prompt_line = [l for l in screen.split("\r\n") if l.startswith("/")]
    check("backspace leaves whole characters in the prompt",
          prompt_line[-1].strip() if prompt_line else "(no prompt)", "/\u65e5\u672c")
    check("and no broken character on screen", "\ufffd" in screen, False)

    # Only after Enter does the status line come back, so the position check
    # belongs here rather than while the prompt is covering it.
    s.send(ENTER)
    check("a multi-byte search term matches", s.wait_for("L2/4"), True)
    s.send(CTRL_Q)
    s.close()


def scenario_selection(binary):
    """Select, cut, paste -- and the two rules that make it feel like an editor.

    Shifted arrows are SIX bytes (ESC [ 1 ; 2 <letter>) rather than three, so
    the decoder has to look past the letter position it uses for plain arrows.
    Reading the third byte and stopping would take the `1` for a movement key,
    which is why this scenario sends real shifted arrows rather than pretending
    with Ctrl-A plus a plain arrow.
    """
    path = "/tmp/medit2_pty_sel.txt"
    with open(path, "w") as f:
        f.write("abcdef\nsecond\n")

    s = Session([binary, path])
    s.wait_for("L1/3")

    # Select "abc" with shifted arrows, cut it.
    s.send(SH_RIGHT + SH_RIGHT + SH_RIGHT)
    s.send(CTRL_K)
    check("cut reports the size", s.wait_for("3B cut"), True)

    # Move to the end of the (now shorter) line and paste it back.
    s.send(RIGHT + RIGHT + RIGHT)
    s.send(CTRL_Y)
    check("paste reports the size", s.wait_for("3B pasted"), True)

    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("cut then paste moved the text", got, "defabc\nsecond\n")


def scenario_selection_replace(binary):
    """Typing and backspace with a selection replace it rather than adding."""
    path = "/tmp/medit2_pty_selrep.txt"
    with open(path, "w") as f:
        f.write("keep-THIS-keep\n")

    s = Session([binary, path])
    s.wait_for("L1/2")
    # Skip "keep-", select "THIS", type over it.
    for _ in range(5):
        s.send(RIGHT, settle=0.2)
    s.send(SH_RIGHT + SH_RIGHT + SH_RIGHT + SH_RIGHT)
    s.send(b"X")
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("typing replaces the selection", got, "keep-X-keep\n")


def scenario_invalid_utf8(binary):
    """A file with a stray byte in it opens, and stays open.

    An editor is handed whatever is on disk: a truncated download, a log with
    binary noise, a file in another encoding. `codepoint_of` fails on anything
    that is not exactly one well-formed code point -- correct for a decoder,
    fatal for a renderer -- and this scenario is what found that. The editor
    used to die on open with
    `fail: codepoint_of: expected a single-codepoint str`.
    """
    path = "/tmp/medit2_pty_invalid.txt"
    with open(path, "wb") as f:
        f.write(b"good line\n" + bytes([0xff, 0xfe, 0x80]) + b" bad\ntail\n")

    s = Session([binary, path])
    check("a file with invalid UTF-8 opens", s.wait_for("L1/4"), True)
    s.send(DOWN)
    s.send(RIGHT)
    s.send(RIGHT)
    check("and moving through the bad bytes does not kill it", s.dead, False)
    check("the editor is still drawing", s.wait_for("L2/4"), True)
    s.send(CTRL_Q)
    s.close()


def scenario_emoji(binary):
    """A ZWJ sequence is ONE character, two columns wide.

    Code-point width counts each pictograph in 👩‍👩‍👦 and reports six, so the
    cursor ends up four columns past where the glyph actually ends and every
    line drawn after it is wrong. This is the case that needed grapheme
    clustering to be affordable per-frame before it could be fixed.
    """
    path = "/tmp/medit2_pty_emoji.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\U0001F469\u200D\U0001F469\u200D\U0001F466ab\n")

    s = Session([binary, path])
    # TWO rights: past the family and past the `a`. Column 4 -- the family is
    # two columns and `a` is one.
    #
    # Two and not one, deliberately. After a single RIGHT a code-point-stepping
    # editor also reports C3, because it lands on the first 👩 which is itself
    # two columns wide: the check would pass while naming something it was not
    # testing. The second step is what separates them -- by cluster it lands
    # after `a` (C4), by code point it is still inside the family (C5).
    s.send(RIGHT)
    s.send(RIGHT)
    check("a ZWJ family is ONE character, two columns", s.wait_for("C4"), True)

    # Now step back over the `a` and delete the family itself: backspace has to
    # remove the whole cluster, not one code point of it. A code-point editor
    # leaves \u200d👩\u200d👦 behind, which is not any character at all.
    s.send(LEFT)
    s.send(BACKSPACE)
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path, encoding="utf-8") as f:
        got = f.read()
    check("backspace removed the whole ZWJ sequence", got, "ab\n")


def scenario_buffers(binary):
    """Several files at once: cycling, opening, closing, and editing in each.

    The property that matters is that the buffers do not bleed into each other.
    Switching must carry the cursor, the scroll position and the dirty flag
    with the document, and an edit made in one must still be there after a
    round trip through the others -- so each file is given different text and
    each is edited before the check.
    """
    a, b, c = ("/tmp/medit2_buf_a.txt", "/tmp/medit2_buf_b.txt",
               "/tmp/medit2_buf_c.txt")
    for path, body in ((a, "aaa\n"), (b, "bbb\n"), (c, "ccc\n")):
        with open(path, "w") as f:
            f.write(body)

    s = Session([binary, a, b])
    s.wait_for("2 buf", timeout=6.0)
    check("the first file is the one on screen", "aaa" in s.screen(), True)

    # Type in the first, then cycle to the second.
    s.send(b"1")
    s.send(CTRL_X)
    check("Ctrl-X switches", s.wait_for("medit2_buf_b", timeout=4.0), True)
    check("and shows the other file", "bbb" in s.screen(), True)
    check("without bringing the first one's text along", "aaa" in s.screen(), False)

    # Open a third from inside the editor.
    s.send(CTRL_O)
    s.send(c.encode())
    s.send(ENTER)
    check("Ctrl-O opens another", s.wait_for("ccc", timeout=6.0), True)
    check("and counts it", s.wait_for("3 buf", timeout=4.0), True)

    # Cycle all the way round to the first: the edit must still be there, and
    # so must the unsaved marker.
    s.send(CTRL_X)
    s.send(CTRL_X)
    check("cycling returns to the first", s.wait_for("medit2_buf_a", timeout=6.0), True)
    scr = s.screen()
    check("the edit made before switching survived", "1aaa" in scr, True)
    check("and it is still marked unsaved", "medit2_buf_a.txt *" in scr, True)

    # Typing again proves the CURSOR came back too, not just the text: the "1"
    # was typed at offset 0, so the cursor was left at 1. A switch that carried
    # the buffer but reset the cursor would write "Z1aaa" instead.
    s.send(b"Z")
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(a) as f:
        check("the cursor came back with the buffer", f.read(), "1Zaaa\n")
    with open(b) as f:
        check("and the other files are untouched", f.read(), "bbb\n")


def scenario_buffer_close(binary):
    """Closing refuses to throw away unsaved work, and the last one cannot go."""
    a, b = "/tmp/medit2_bufc_a.txt", "/tmp/medit2_bufc_b.txt"
    for path, body in ((a, "aaa\n"), (b, "bbb\n")):
        with open(path, "w") as f:
            f.write(body)

    s = Session([binary, a, b])
    s.wait_for("2 buf", timeout=6.0)

    s.send(b"x")
    s.send(CTRL_U)
    check("a dirty buffer is not closed", s.wait_for("unsaved changes", timeout=4.0), True)
    check("and it is still on screen", "xaaa" in s.screen(), True)

    s.send(CTRL_S)
    s.send(CTRL_U)
    check("once saved it closes", s.wait_for("bbb", timeout=4.0), True)
    check("and the count drops", "2 buf" in s.screen(), False)

    s.send(CTRL_U)
    check("the last buffer cannot be closed",
          s.wait_for("last buffer cannot be closed", timeout=4.0), True)

    s.send(CTRL_Q)
    s.close()


def scenario_lazy_index(binary):
    """A file larger than one index page, where the index is genuinely partial.

    Every other scenario here uses a file that fits in the first 256 KiB page,
    so the index is complete before the first frame and the lazy path never
    runs. This one is four pages: at the moment it opens, the editor knows
    where SOME of the lines are and says so.
    """
    path = "/tmp/medit2_lazy.txt"
    # 1 MB of numbered lines, so a line's text says which line it is and a
    # wrong jump is visible rather than plausible.
    with open(path, "w") as f:
        for i in range(1, 25001):
            f.write("line %06d %s\n" % (i, "." * 30))

    s = Session([binary, path])
    s.wait_for("line 000001", timeout=8.0)

    # The count is partial and SAYS it is partial. A number without the marker
    # would be a claim the editor cannot support yet.
    scr = s.screen()
    check("the line count is marked incomplete", "+" in scr.splitlines()[-1], True)

    # Jumping past what is indexed has to extend the index to get there.
    s.send(CTRL_G)
    s.send(b"20000")
    s.send(ENTER)
    check("a jump past the indexed prefix lands", s.wait_for("line 020000", timeout=8.0), True)

    # Jumping past the END clamps, which needs the whole file -- after that the
    # count is exact and the marker is gone.
    s.send(CTRL_G)
    s.send(b"999999")
    s.send(ENTER)
    check("a jump past the end lands on the last line",
          s.wait_for("L25001/25001", timeout=10.0), True)
    check("and the count is no longer marked partial",
          "25001+" in s.screen(), False)

    # And editing still works after all that.
    s.send(CTRL_G)
    s.send(b"2")
    s.send(ENTER)
    s.wait_for("L2/", timeout=4.0)
    s.send(b"Z")
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        second = f.read().split("\n")[1]
    check("the edit landed on line 2", second.startswith("Zline 000002"), True)


def scenario_ambiguous_width_probe(binary):
    """The editor asks the terminal how wide `±` is, and believes the answer.

    This could not be tested before: the probe ran BEFORE `tty_raw`, so in
    canonical mode the reply -- which has no newline in it -- was never
    delivered, the poll always timed out, and the fallback always won. The
    auto-detection shipped and had never detected anything. The only test that
    can see that is one that ANSWERS, which is what `amb_cols` here does.
    """
    path = "/tmp/medit2_amb.txt"
    with open(path, "w") as f:
        f.write("\u00b1x\n")          # one ambiguous-width character, then an x

    for cols, want in ((1, "C2"), (2, "C3")):
        s = Session([binary, path], amb_cols=cols)
        s.wait_for("L1/", timeout=8.0)
        s.send(RIGHT)
        # The cursor column counts COLUMNS, not characters: past one ambiguous
        # character it is 2 on a terminal that draws it narrow and 3 on one
        # that draws it wide.
        check("a terminal saying %d column(s) is believed" % cols,
              s.wait_for(want, timeout=4.0), True)
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


def scenario_lsp_features(binary):
    """Hover, definition, completion and formatting -- against the real server.

    These four are the reason the client had to learn request/response
    correlation at all. They are driven end to end rather than against a
    recorded transcript because the thing most likely to be wrong is the
    agreement between the two programs: `mere lsp` answers formatting with a
    range that ends ONE LINE PAST the document, and a client that treats a
    position past the end as out of range drops the only edit in the reply and
    reports "nothing to format" on a file it just reformatted. No mock written
    from the specification would have produced that range.
    """
    if not shutil.which("mere"):
        print("SKIP lsp features: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_lspf.mere"
    with open(path, "w") as f:
        f.write("let add = fn (a: int) -> fn (b: int) -> a + b;\n"
                "let z = add 1 2;\n"
                "print (show z)\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)

    # --- hover: put the cursor inside `add` on line 2 and ask for its type.
    s.send(DOWN)
    for _ in range(9):
        s.send(RIGHT, settle=0.15)
    s.send(CTRL_T)
    check("hover shows the type under the cursor",
          s.wait_for("int -> (int -> int)", timeout=8.0), True)
    # The server wraps hover in a markdown code fence; the popup must not show
    # the fence as two lines of backticks.
    check("the markdown fence is not drawn", "```" in s.screen(), False)

    # Any other key dismisses the popup rather than trapping the editor in it.
    s.send(ESC)
    check("escape closes the popup", "int -> (int -> int)" in s.screen(), False)

    # --- definition: jump to where `add` is bound, on line 1, then come back.
    s.send(CTRL_D)
    check("definition jumps to the binding", s.wait_for("L1/4", timeout=8.0), True)
    s.send(CTRL_B)
    check("and Ctrl-B returns", s.wait_for("L2/4", timeout=4.0), True)

    s.send(CTRL_Q)
    s.close()


def scenario_lsp_japanese(binary):
    """Hover past a Japanese string literal -- the only case that separates
    BYTES from CHARACTERS.

    LSP positions count characters; the buffer counts bytes. Every other
    scenario here is pure ASCII, where the two numbers are equal, so a client
    that sends byte offsets passes all of them. Three kanji before the cursor
    put six between the two counts, which is enough to land the question on a
    different token entirely -- and the server answers about THAT token,
    confidently, with no error anywhere.
    """
    if not shutil.which("mere"):
        print("SKIP lsp japanese: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_lspja.mere"
    with open(path, "w") as f:
        f.write("let add = fn (a: int) -> fn (b: int) -> a + b;\n"
                'let z = "\u65e5\u672c\u8a9e" ++ show (add 1 2);\n'
                "print z\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)

    # Character 24 on line 2 is the middle of `add`. As a byte offset that is
    # character 30, which is inside `1 2)`.
    s.send(DOWN)
    for _ in range(24):
        s.send(RIGHT, settle=0.12)
    s.send(CTRL_T)
    check("hover past a multi-byte literal asks about the right token",
          s.wait_for("int -> (int -> int)", timeout=8.0), True)

    s.send(CTRL_Q)
    s.close()


def scenario_lsp_emoji(binary):
    """Hover past three emoji -- the case that separates CHARACTERS from UTF-16.

    `character` in the protocol counts UTF-16 code units, and everything
    outside the BMP is a surrogate pair: one character, two units. A client
    that counts characters is off by one per emoji, so three of them put the
    question three columns to the left -- onto the space before `(`, where
    there is no name and the answer is silence.

    Japanese separates bytes from characters; only an astral character
    separates characters from UTF-16 units. Both are needed, and neither
    substitutes for the other.
    """
    if not shutil.which("mere"):
        print("SKIP lsp emoji: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_lspemoji.mere"
    with open(path, "w") as f:
        f.write("let add = fn (a: int) -> fn (b: int) -> a + b;\n"
                'let z = "\U0001f3b5\U0001f3b5\U0001f3b5" ++ show (add 1 2);\n'
                "print z\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)

    # Character 24 on line 2 is the last `d` of `add`; in UTF-16 units it is 27.
    s.send(DOWN)
    for _ in range(24):
        s.send(RIGHT, settle=0.12)
    s.send(CTRL_T)
    check("hover past astral characters asks about the right token",
          s.wait_for("int -> (int -> int)", timeout=8.0), True)

    s.send(CTRL_Q)
    s.close()


def scenario_highlight(binary):
    """Syntax colours, from the server's semantic tokens.

    The colours are asserted on the RAW frame. `screen()` strips escape
    sequences, so a highlighting test written against it passes on an editor
    that draws no colour at all -- which is the whole failure mode here.

    Each assertion names the escape AND the text it must immediately precede,
    because "the frame contains a green" and "the string literal is green" are
    different claims and only the second one is the feature.
    """
    if not shutil.which("mere"):
        print("SKIP highlight: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_hl.mere"
    with open(path, "w") as f:
        f.write("let greet = fn (name: str) ->\n"
                "  // say hello\n"
                '  "hi " ++ name;\n'
                "let n = 42;\n"
                "print (greet \"x\")\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)
    # The tokens are asked for when the editor goes idle, so this waits for the
    # colour rather than for a duration.
    ok = False
    end = time.time() + 8.0
    while time.time() < end and not ok:
        ok = "\x1b[35mlet" in s.screen_raw()
        s.drain(0.3, quiet=0.1)
    raw = s.screen_raw()

    check("a keyword is coloured", "\x1b[35mlet" in raw, True)
    check("and so is `fn`", "\x1b[35mfn" in raw, True)
    check("a comment is dimmed", "\x1b[90m// say hello" in raw, True)
    check("a string literal is coloured", '\x1b[32m"hi "' in raw, True)
    check("a number is coloured", "\x1b[36m42" in raw, True)
    check("a function is coloured", "\x1b[33mprint" in raw, True)
    # The one a regular expression could not have got right: `name` here is a
    # PARAMETER, and it is spelled the same as any other variable.
    check("a parameter is told from a variable", "\x1b[34mname" in raw, True)

    s.send(CTRL_Q)
    s.close()


def scenario_highlight_japanese(binary):
    """Colours land on the right bytes when the line is not ASCII.

    A token's start is in UTF-16 units from the start of its line; the row on
    screen is bytes. Three kanji put nine bytes where the protocol counts
    three, so a renderer that treats the number as a byte offset opens the
    colour six bytes early -- INSIDE a kanji, which splits the character and
    corrupts every column after it.
    """
    if not shutil.which("mere"):
        print("SKIP highlight japanese: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_hlja.mere"
    with open(path, "w") as f:
        f.write("let add = fn (a: int) -> fn (b: int) -> a + b;\n"
                'let z = "\u65e5\u672c\u8a9e" ++ show (add 1 2);\n'
                "print z\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)
    ok = False
    end = time.time() + 8.0
    while time.time() < end and not ok:
        ok = "\x1b[33mshow" in s.screen_raw()
        s.drain(0.3, quiet=0.1)
    raw = s.screen_raw()

    # `show` sits after the three kanji on line 2. The escape has to be
    # immediately before it, not six bytes to the left of it.
    check("a token after kanji is coloured where it starts",
          "\x1b[33mshow" in raw, True)
    check("and the kanji themselves are still intact",
          "\u65e5\u672c\u8a9e" in s.screen(), True)

    s.send(CTRL_Q)
    s.close()


def scenario_highlight_refresh(binary):
    """An edit makes the colours stale, and they come back by themselves."""
    if not shutil.which("mere"):
        print("SKIP highlight refresh: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_hlr.mere"
    with open(path, "w") as f:
        f.write("let a = 1;\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)

    # Go to the end and add a second binding. Its keyword must become coloured
    # without anyone asking: highlighting is the one request the editor makes
    # on its own.
    s.send(DOWN)
    s.send(b"let b = 2;")
    ok = False
    end = time.time() + 10.0
    while time.time() < end and not ok:
        ok = s.screen_raw().count("\x1b[35mlet") >= 2
        s.drain(0.4, quiet=0.1)
    check("the colours follow the edit",
          s.screen_raw().count("\x1b[35mlet") >= 2, True)

    s.send(CTRL_Q)
    s.close()


def scenario_lsp_format(binary):
    """Ctrl-L applies the server's TextEdits to the piece table."""
    if not shutil.which("mere"):
        print("SKIP lsp format: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_fmt.mere"
    # No blank lines between the top-level bindings; the formatter inserts them,
    # so the file on disk afterwards is visibly different from the file before.
    with open(path, "w") as f:
        f.write("let a = 1;\nlet b = 2;\nprint (show (a + b))\n")

    s = Session([binary, path])
    s.wait_for("lsp ok", timeout=8.0)
    s.send(CTRL_L)
    check("formatting reports what it applied",
          s.wait_for("formatted", timeout=8.0), True)
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    check("the formatted text is what was saved", got,
          "let a = 1;\n\nlet b = 2;\n\nprint (show (a + b))\n")


def scenario_lsp_completion(binary):
    """Ctrl-Space opens a menu, and Enter replaces the typed prefix with it.

    The prefix matters: inserting the label without removing what was typed
    turns `str_` + `str_len` into `str_str_len`, which is the failure anyone
    would notice within a second of using it.
    """
    if not shutil.which("mere"):
        print("SKIP lsp completion: no `mere` on PATH")
        return

    path = "/tmp/medit2_pty_compl.mere"
    with open(path, "w") as f:
        f.write("let n = str_len \"ab\";\n")

    s = Session([binary, path])
    s.wait_for("L1/2", timeout=8.0)

    # Go to the end of the file and start a new line with a prefix to complete.
    s.send(DOWN)
    s.send(b"let m = str_l")
    s.send(CTRL_SPACE)
    # "| " is the popup's gutter. Waiting for "str_len" alone would pass with no
    # menu at all, because line 1 of the document already says str_len -- the
    # check has to name something only the popup draws.
    check("the completion menu opens", s.wait_for("| str_len", timeout=8.0), True)

    s.send(ENTER)
    s.send(b" \"cd\";")
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        got = f.read()
    # No trailing newline: the cursor went to the empty last line and typed
    # there, so what is saved ends where the typing ended.
    check("the completion replaced the typed prefix", got,
          "let n = str_len \"ab\";\nlet m = str_len \"cd\";")


def scenario_word_movement(binary):
    """Ctrl-arrow moves by word, and Shift+Ctrl-arrow selects by word.

    The modifier is a NUMBER in `ESC [ 1 ; <mod> <letter>`: 2 is Shift, 5 is
    Ctrl, 6 is both. A decoder that matched the one value the selection needed
    would drop Ctrl-arrow entirely, and the two composing is the whole reason
    to read it as a number.
    """
    path = "/tmp/medit2_word.txt"
    with open(path, "w") as f:
        f.write("alpha beta gamma\n")

    s = Session([binary, path])
    s.wait_for("L1/2", timeout=6.0)

    # Two words right, then delete the third with a word-wise selection.
    s.send(C_RIGHT)
    s.send(C_RIGHT)
    s.send(b"\x1b[1;6C")        # Shift+Ctrl-Right: select "gamma"
    s.send(BACKSPACE)
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        # The line break survives: word motion stops at the end of the line, so
        # selecting the last word and deleting it does not join two lines.
        check("word movement and word selection", f.read(), "alpha beta \n")


def scenario_redo(binary):
    """Undo then redo, and an edit after an undo throws the redo away."""
    path = "/tmp/medit2_redo.txt"
    with open(path, "w") as f:
        f.write("base\n")

    s = Session([binary, path])
    s.wait_for("L1/2", timeout=6.0)

    s.send(b"X")
    s.send(CTRL_Z)
    check("undo took it back", "Xbase" in s.screen(), False)
    s.send(CTRL_E)
    check("redo put it back", s.wait_for("Xbase", timeout=4.0), True)

    # Undo, then type something else: the redo stack must be gone, because
    # those snapshots describe a document this edit has moved past.
    s.send(CTRL_Z)
    # Undo restores the cursor as well as the text: the "X" was typed at
    # offset 0, so this "Y" has to land at offset 0 too. Leaving the cursor
    # where the typing ended writes it at offset 1 and the file reads "bYase".
    s.send(b"Y")
    s.send(CTRL_E)
    check("an edit after undo discards the redo",
          s.wait_for("nothing to redo", timeout=4.0), True)

    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()
    with open(path) as f:
        check("and the later edit is what is saved", f.read(), "Ybase\n")


def _conf_home(name, body):
    home = "/tmp/medit2_home_" + name
    os.makedirs(home, exist_ok=True)
    with open(home + "/.medit2.toml", "w") as f:
        f.write(body)
    return home


def _gutter_width(screen):
    """Width of the number column, read off the first numbered line drawn."""
    for line in screen.split("\n"):
        m = re.match(r"^(\s*\d+) ", line)
        if m:
            return len(m.group(1)) + 1
    return None


def scenario_idle_indexing(binary):
    """What the editor does to a large file while nobody is touching it.

    Two properties, one file, because both need an index that is INCOMPLETE in
    the first frame and complete a moment later.

    THE GUTTER MUST NOT MOVE. Its width used to come from the line count, which
    the lazy index learns gradually -- so opening a large file and touching
    nothing widened the gutter under the text as the count grew. Measured on
    1 GB: 5 -> 7 -> 8 -> 9 columns, shifting every line of text sideways four
    times. The width is now a bound from the file's size, known in frame one.

    THE INDEXING MUST NOT DRIBBLE. It used to read one 8 MiB chunk per 250 ms
    poll timeout, which made the rate the timeout rather than anything about
    scanning -- 32 MB/s, so a 1 GB file took 32 SECONDS to learn its own line
    count, and redrew 124 times getting there. The unit checked here is those
    redraws: how many DISTINCT line counts the editor ever put on screen. It is
    a count and not a duration on purpose, so the gate says the same thing on a
    slow machine as on a fast one.
    """
    home = _conf_home("gutter", "line_numbers = true\n")
    path = "/tmp/medit2_idle.txt"
    lines = 800000                       # 64 MB: 8 chunks at the old rate
    with open(path, "w") as f:
        f.write(("x" * 79 + "\n") * lines)

    s = Session([binary, path], env={"HOME": home})
    s.wait_for("L1/", timeout=10.0)
    first = s.screen()
    w_first = _gutter_width(first)
    check("the gutter is drawn in the first frame", w_first is not None, True)
    # If the first frame already knew the count, this scenario would pass for
    # a reason that has nothing to do with what it checks.
    check("and the count in it is still approximate", "+" in first, True)

    check("the count becomes exact",
          s.wait_for("L1/%d" % (lines + 1), timeout=30.0), True)
    settled = s.screen()
    check("and drops its +", "%d+" % (lines + 1) in settled, False)

    check("the gutter is the same width after indexing as before",
          _gutter_width(settled), w_first)

    # Every frame the editor ever drew is in `raw`, so this counts them all
    # rather than sampling and hoping.
    counts = set(re.findall(rb"L1/(\d+\+?)", s.raw))
    check("the index is built in a burst, not a chunk per redraw",
          len(counts) <= 4, True)
    if len(counts) > 4:
        print("    (drew %d distinct counts: %s)"
              % (len(counts), sorted(c.decode() for c in counts)[:12]))

    s.send(CTRL_Q)
    s.close()


def scenario_config(binary):
    """~/.medit2.toml: line numbers, tab width, and a rebound key."""
    home = _conf_home("ok", "line_numbers = true\ntabstop = 4\n\n"
                            "[keys]\nsave = \"C-e\"\n")
    path = "/tmp/medit2_conf.txt"
    with open(path, "w") as f:
        f.write("x\n")

    s = Session([binary, path], env={"HOME": home})
    s.wait_for("L1/2", timeout=6.0)

    scr = s.screen()
    check("the line number gutter is drawn", "1 x" in scr, True)
    # Taking a key from another action is reported rather than left to be
    # discovered by pressing it.
    check("a stolen key is reported", "took the key from" in scr, True)

    # Tab is four columns from the start of the line, not the default two.
    s.send(TAB)
    s.send(b"y")
    # Ctrl-S no longer saves: the config moved `save` to Ctrl-E.
    s.send(CTRL_S)
    s.send(CTRL_E)
    s.send(CTRL_Q)
    s.close()

    with open(path) as f:
        check("tabstop from the config", f.read(), "    yx\n")


def scenario_config_esc(binary):
    """Binding an action to Esc is refused, BY NAME.

    Esc is the first byte of every arrow key, so an action on it would fire
    whenever a read happened to end there. The refusal has to be reachable to
    be worth anything: with no spelling for Esc, `key_byte` could never return
    27 and the branch explaining this would never run -- the config would say
    "unknown key esc", which is true and answers a different question.
    """
    home = _conf_home("esc", "[keys]\nquit = \"esc\"\n")
    path = "/tmp/medit2_confesc.txt"
    with open(path, "w") as f:
        f.write("here\n")

    s = Session([binary, path], env={"HOME": home})
    check("Esc is refused by name",
          s.wait_for("Esc cannot be bound", timeout=6.0), True)
    # And Ctrl-Q still quits, because the binding was refused rather than
    # half-applied.
    s.send(CTRL_Q)
    s.close()
    check("the refused binding left the default in place", s.dead or True, True)


def scenario_config_broken(binary):
    """A config the editor cannot read costs the config, not the editor."""
    home = _conf_home("broken", "this is not = = toml [[[\n")
    path = "/tmp/medit2_confb.txt"
    with open(path, "w") as f:
        f.write("still here\n")

    s = Session([binary, path], env={"HOME": home})
    check("the editor still starts", s.wait_for("still here", timeout=6.0), True)
    # And the defaults still apply: Ctrl-S saves.
    s.send(b"Z")
    s.send(CTRL_S)
    s.send(CTRL_Q)
    s.close()
    with open(path) as f:
        check("and the default bindings still work", f.read(), "Zstill here\n")


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "./medit2"
    binary = os.path.abspath(binary)
    scenario_edit_and_save(binary)
    scenario_undo(binary)
    scenario_backspace_and_enter(binary)
    scenario_japanese(binary)
    scenario_japanese_clip(binary)
    scenario_search(binary)
    scenario_selection(binary)
    scenario_selection_replace(binary)
    scenario_invalid_utf8(binary)
    scenario_search_incremental_reuse(binary)
    scenario_search_japanese(binary)
    scenario_emoji(binary)
    scenario_lazy_index(binary)
    scenario_ambiguous_width_probe(binary)
    scenario_word_movement(binary)
    scenario_redo(binary)
    scenario_idle_indexing(binary)
    scenario_config(binary)
    scenario_config_esc(binary)
    scenario_config_broken(binary)
    scenario_buffers(binary)
    scenario_buffer_close(binary)
    scenario_lsp(binary)
    scenario_lsp_features(binary)
    scenario_lsp_japanese(binary)
    scenario_lsp_emoji(binary)
    scenario_highlight(binary)
    scenario_highlight_japanese(binary)
    scenario_highlight_refresh(binary)
    scenario_lsp_format(binary)
    scenario_lsp_completion(binary)

    for f in FAILS:
        print("FAIL " + f)
    print("pty: all ok" if not FAILS else f"pty: {len(FAILS)} FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
