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
| Shift+arrows | extend the selection; an unshifted arrow drops it |
| printable text | insert — a paste arrives as one edit and one undo step. With a selection, replaces it |
| Enter / Backspace | as expected; backspace removes a whole cluster, or the selection |
| Ctrl-A | start a selection at the cursor |
| Ctrl-W / Ctrl-K / Ctrl-Y | copy / cut / paste — through `pbcopy`/`xclip` when there is one, and internally when there is not |
| Ctrl-F / Ctrl-R | search forward / backward, incrementally, over the piece table — 208 MB in 0.46 s at 2.3 MB resident |
| Ctrl-N / Ctrl-P | the next / previous match, wrapping, and it says when it wrapped |
| Ctrl-G | go to line |
| Ctrl-T | the type under the cursor (hover) |
| Ctrl-D / Ctrl-B | go to the definition / come back |
| Ctrl-Space | completion; the menu narrows as you keep typing, Enter or Tab takes it |
| Ctrl-L | format the file through the server |
| Ctrl-Left / Ctrl-Right | move by word; with Shift, select by word. Word motion stops at a line end, so selecting the last word and deleting it does not join two lines |
| Ctrl-X / Ctrl-O / Ctrl-U | next buffer / open a file / close this buffer |
| Ctrl-S / Ctrl-Z / Ctrl-E / Ctrl-Q | save / undo / redo / quit |
| Esc | close a popup, or cancel a prompt |

Undo restores the **cursor** as well as the text: typing `X` at the start of
`base` and undoing puts the next character back at offset 0, not at offset 1.
The position rides on the undo stack itself rather than in a parallel one in
the editor, because a parallel stack has to be pushed in exactly the same
places and the first edit that forgets desynchronises them silently.

## Configuration

`~/.medit2.toml`, if it is there. A config with a mistake in it costs you the
line with the mistake and a message saying so, never the editor: one you cannot
start is one you cannot fix the dotfile with.

```toml
wide = true          # this terminal draws EastAsianWidth=Ambiguous as 2 columns
tabstop = 4          # Tab inserts spaces to the next multiple of this
line_numbers = true

[color]              # SGR numbers, by the server's token-type NAME
keyword = "35"
comment = "90"

[keys]               # any action, on any control byte
save = "C-s"
redo = "C-e"
complete = "C-space"
```

Rebinding an action to a key another action already had is allowed and is
**reported**: the action that lost it now has no key at all, and finding that
out by pressing it is the worst way to find out. `Esc` is refused by name --
it is the first byte of every arrow key, so binding an action to it would make
arrows do that action whenever the read happened to split there.

Without a config, `--wide` decides; without that, the editor **asks the
terminal**: it prints one ambiguous-width character and reads the cursor
position back with `ESC[6n`. That is a measurement of this terminal rather than
a guess from `$LANG`, which says what language you read and nothing about how
the emulator draws. The probe reads the keyboard's descriptor, so it cuts its
own answer out of what it read and hands the rest back to the editor -- the
first version swallowed a keystroke typed during startup.

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

Hover, go-to-definition, completion and formatting all go over the same
connection. One request is outstanding at a time and it is correlated by id: a
reply carrying a previous id is dropped, because the user has already moved on
from the position it is about. An unanswered request is given up on after three
seconds rather than leaving the editor waiting for an answer that is not coming.

Two things this found in `mere lsp`, neither of which any test written from the
specification would have produced:

- **its columns were bytes.** The protocol counts UTF-16 code units. Every test
  was ASCII, where the two numbers are equal; one line with kanji on it put the
  question six columns to the left, and the server answered about that token
  instead — correctly, and about the wrong thing. Fixed in `lib/lsp.ml`, in both
  directions, and for diagnostics and semantic tokens as well as for hover.
- **completion could not offer a builtin.** `str_len` and `print` are not
  declarations, so the scope walk that reads the tree could not see them. Typing
  `str_l` and asking for completion offered `list_iter`.

Completion is filtered by the client, which is what the server's
`isIncomplete: false` asks for: it returns the whole visible scope once, and the
menu narrows as you keep typing with no second round trip.

## Syntax highlighting

From the server's semantic tokens, so there is no second lexer in here
pretending to know Mere. `mere lsp` says which names are **parameters** and
which are **functions** -- a distinction no pattern over the text can make --
and, since v0.1.478, where the keywords, literals and comments are, taken from
the compiler's own lexer. An editor that coloured only the identifiers and left
`let`, `"text"` and the comments plain does not look like it is highlighting
anything, so that half was added to the server rather than guessed at here.

A token's start is in UTF-16 units from the start of its line and the row on
screen is bytes, so the two are converted per row. Three kanji put nine bytes
where the protocol counts three; opening the colour at the byte offset would
split a character and corrupt every column after it.

The colours refresh themselves. It is the one request the editor makes without
anyone pressing a key -- which is also why it will not make it while a hover or
a completion menu is open: asking clears the pending answer, and the popup you
were reading would vanish.

## Tests

```sh
sh verify.sh
```

Three layers, because each catches what the others cannot:

| | what it can see |
|---|---|
| `test/buffer_test.mere` | the piece table, run against **both** an in-memory origin and a real file — the pure one is the oracle for the one that ships |
| `test/search_test.mere` | a match that straddles a 256 KiB window boundary, a needle longer than a window, and backward search returning the **last** match rather than the first |
| `test/lsp_test.mere` | the cases a working server never sends: a reply to a question the user moved on from, an `error` instead of a `result`, a frame split across two reads, and a server *request* whose id collides with ours |
| `test/pty_drive.py` | a **real pty**: `tty_raw` is a no-op off-tty, `term_rows` answers -1 through a pipe, and IXON/ISIG only exist where there is a line discipline. Piped tests passed while Ctrl-Z did nothing at all |
| the same suite with `mere` on `PATH` | the language server end to end — a diagnostic arriving and clearing, hover, definition, completion, formatting, and the two encoding cases: **kanji** separates bytes from characters, and an **emoji** separates characters from UTF-16 units. Neither substitutes for the other |

The pty suite waits for the thing it is about to assert on rather than for a
duration, so it is not a race; and every scenario has been checked to fail when
the behaviour it names is broken.

Four of those checks did not fail the first time they were poisoned, and each
one was a defect in the test rather than in the editor:

- a poison that did not **compile** looked green
- a poison that did not change **behaviour** looked green — the discriminating
  input had to become a server-sent *request*, whose id collides with the
  client's on the first question each side asks
- "the completion menu opened" waited for `str_len`, and **that word was
  already on line 1**; it waits for the popup's own gutter now
- "positions are sent as characters" passed with byte offsets, because **every
  test file was ASCII**. Kanji separate bytes from characters and an emoji
  separates characters from UTF-16 units; both are now scenarios, and neither
  substitutes for the other

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
