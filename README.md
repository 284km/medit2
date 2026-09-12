# medit2

A terminal text editor written in [Mere](https://github.com/merelang/mere) that
**does not load the file**, and talks to a language server over the same
`poll(2)` as the keyboard.

```
$ ls -la big.txt
-rw-r--r--  1 you  staff  208000000  big.txt

$ ./medit2 big.txt
```

Opens in **0.5 s at 25 MB of memory** — and 20 MB of that is the line index for
2,600,001 lines. The document itself is never read; the editor holds a piece
table over the file on disk and reads only the screen it is drawing.

```
line 0
line 1
...
/tmp/big.txt  L2000001/2600001 C1  208000000B  [lsp ok]
```

## Build

```sh
mere -c medit2.mere > medit2.c
clang -O2 -Wno-conditional-type-mismatch medit2.c shim/medit2_shim.c -o medit2
./medit2 [--wide] FILE
```

The shim is what Mere's host does not have: the terminal's size
(`ioctl(TIOCGWINSZ)`), a child process on one bidirectional fd (`socketpair` +
`fork`), and the half of raw mode `tty_raw` leaves behind. Five functions, and
**no change to the compiler** — Mere emits an `extern` prototype for any name it
does not implement itself.

`--wide` says this terminal draws EastAsianWidth=Ambiguous characters (`±`,
`※`, the box-drawing set) in two columns, which a terminal configured for CJK
does. The standard declines to decide, so the editor is told rather than
guessing.

## Keys

| | |
|---|---|
| arrows | move; left/right step by **grapheme cluster** — not a byte, and not a code point |
| printable text | insert — a paste arrives as one edit and one undo step |
| Enter / Backspace | as expected; backspace removes a whole cluster |
| Ctrl-S | save |
| Ctrl-Z | undo |
| Ctrl-Q | quit |

## How it does not load the file

A **piece table**. A `buf` is a list of pieces, each naming a stretch of either
the file on disk or the append-only string this session has typed. Opening a
3 GB file allocates one piece.

The piece list is a **persistent list**, and that is the design's second win.
It has one entry per edit rather than one per line, so an undo snapshot is one
cons cell — the old and new lists share every piece they have in common. Undo
on a 208 MB file costs exactly what undo on an empty one costs. (`vec` has no
insert-at either, which is the first reason; the undo is the better one.)

Reads go through **`file_pread_bytes`**, which this editor is the reason for
(mere v0.1.475). Its predecessor `file_pread` returns one boxed int per byte and
builds it one `fgetc` at a time: 208 MB took 3.64 s at 10.1 MB, against 0.36 s
at 1.8 MB now. See [PAIN.md](./PAIN.md) P1.

## Two structural rules

Both measured rather than assumed, and both in the source where they apply:

1. **One region block per redraw.** Building a 40×120 screen 20,000 times costs
   5.9 GB without one and 2.0 MB with one — 2890×. An editor redraws on every
   keystroke, so this is the difference between a session that runs all day and
   one that does not.

2. **The edit is not inside that block.** A region block reclaims what its body
   allocated, and the new piece list *is* allocated by the edit. State
   transitions happen outside; only the screen string is built inside. Getting
   this backwards frees the document.

## Japanese

A terminal cursor moves by columns and a Japanese character is two of them. The
editor clips lines by columns (never leaving half a kanji at the right edge,
which corrupts every row below it), reports the cursor's real column, and moves
and deletes by character.

Widths come from Mere's `contrib/unicode/width.mere`, vendored here under
`.mere_modules/`. It is generated from the UCD and cross-checked against Reline:
17,793 code points agree, and every difference is in a category with a reason
attached rather than a waiver.

Widths and cursor movement are both by **grapheme cluster**, not code point, so
`👩‍👩‍👦` is one character two columns wide rather than seven code points of six —
and backspace removes the whole thing rather than leaving a joiner and two
pictographs that are not any character at all. That needed the clustering to
stop allocating a container per cluster first, which is [PAIN.md](./PAIN.md) P3.

## The language server

`./medit2 foo.mere` starts `mere lsp` and shows its diagnostics in the status
bar. Nothing in Mere had to change for this: a `socketpair` makes the child look
exactly like an accepted TCP connection, so `tcp_read` / `tcp_write` /
`io_poll_add` work on it unchanged, and one `poll(2)` waits on the keyboard and
the server together.

A server is attached to `.mere` files under 1 MB only. `mere lsp` syncs full
text, so pointing it at the large files this editor exists to open would re-send
the whole buffer on every idle tick. The two things this editor does well do not
overlap, and it does not pretend they do.

`didChange` is sent when the editor goes **idle**, not per keystroke, for the
same reason.

## Tests

```sh
sh verify.sh
```

Three layers, because each catches what the others cannot:

| | what it can see |
|---|---|
| `test/buffer_test.mere` | the piece table, run against **both** an in-memory origin and a real file — the pure one is the oracle for the one that ships |
| `test/pty_drive.py` | a **real pty**: `tty_raw` is a no-op off-tty, `term_rows` answers -1 through a pipe, and IXON/ISIG only exist where there is a line discipline. Piped tests passed while Ctrl-Z did nothing at all |
| the same suite with `mere` on `PATH` | the language server end to end — a diagnostic arriving, and clearing when the file is fixed |

The pty suite waits for the thing it is about to assert on rather than for a
duration, so it is not a race; and every scenario has been checked to fail when
the behaviour it names is broken.

## Why it exists

A dogfood for Mere, and the record of what it needed: [PAIN.md](./PAIN.md).
**Ten of its eleven findings went upstream** — a positioned read that builds
`bytes`, two region-lifetime bugs, raw mode not delivering the keys a program
asked for, display width existing at all, grapheme clustering that a renderer
can afford, a lexer papercut, an unchecked `extern` arity, and a warning on
every emitted `match`.

The one that stayed is the interesting one. A container allocated inside a
called function goes to the program-lifetime region rather than to the `region`
block around the call, and that is conservative **on purpose** — the type
system's level discipline is what separates an allocation that is internal from
one shared with something that outlives the call, and it declines to guess. What
was fixable was the caller: the clustering that made it hurt no longer allocates
a container at all.

The first [medit](https://github.com/284km/medit) (2026-07) was deliberately a
probe that added no capability. This one asked the opposite question.
