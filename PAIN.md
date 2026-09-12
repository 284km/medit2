# PAIN log — medit2

Friction hit building a terminal editor that opens files it cannot hold in
memory and talks to a language server. The friction log is as much the point as
the editor.

Status: 🔴 open · 🟡 worked around · 🟢 fixed upstream

The first medit (2026-07, mere v0.1.64) was deliberately a probe that added no
capability. This one is the opposite: the question was what an editor needs that
is not there. **Most of the answer turned out to be "nothing"** — and what was
missing was almost all in one place, which is the interesting part: not the
terminal, not the protocol, but how memory is handed back.

Four fixed upstream (P1, P2, P7, P11), four worked around (P3, P4, P5, P6),
three open (P8, P9, P10).

---

## P1 🟢 `file_pread` costs eight bytes per byte and reads them one at a time (fixed upstream, mere v0.1.475)

Paging through a large file is the whole design. `file_pread` returns
`Vec[R, int]` — one boxed int per byte — and builds it one `fgetc` at a time.
Reading 208 MB in 256 KiB pages took **3.64 s at 10.1 MB peak RSS**, against
0.33 s for `read_bytes` at 210 MB. So the choice was memory or speed.

Worse, and this took measuring twice: a loop reading pages into Vecs grew
without bound past about 4-5 MiB per page. The first reading was "the region
block does not reclaim it", and that was **wrong** -- instrumenting the runtime
showed 40 releases and 40 chain frees out of 40. What was not happening was
REUSE: releasing a grown region freed its chain and re-seeded at 1 MiB, so the
next iteration asked malloc for those megabytes again and malloc did not hand
the same pages back. See P11.

**Fixed upstream (mere v0.1.475):** `file_pread_bytes : File -> int -> int ->
bytes`, the read half of `file_pwrite_bytes` (which had existed since v0.1.222
— only the write side had a `bytes` version). One `fread` into a flat
allocation. All four backends. **208 MB now takes 0.36 s at 1.8 MB**: ten times
faster and a fifth of the memory, and the choice is gone.

## P2 🟢 `file_pread`'s result lived in the program-lifetime region (fixed upstream, mere v0.1.475)

Before P1 made it moot for this editor, the same loop leaked for a second,
independent reason. `region R { let v = file_pread f off len in vec_len v }`
grew linearly with the iteration count — 200 pages of 64 KiB came to 107 MB —
while the emitted C showed the block being acquired and released correctly and
the read being handed `&__lang_default_region`.

`typer.ml` keeps a list of constructors that bind their result to the region
open at the call site (`vec_new`, `read_file_bytes`, `strbuf_new`, `map_new`,
`bytebuf_new`…). **`file_pread` was the only one missing**, so its
region-quantified scheme instantiated a fresh marker that nothing unified with
the block. Writing `(file_pread f off len : Vec[R, int])` bound it by hand and
cut 107 MB to 2.0 MB.

**Fixed upstream (mere v0.1.475):** added to the list, so the ascription is not
needed. The same hole with the same cause is recorded in the compiler's own
comments for `bytebuf_new` (Q-127 / m3d Q-10) — it survived in the one
constructor that gets called in a loop.

## P3 🟡 A container allocated in a helper function is never reclaimed

`region` reclaims what its body allocated **lexically**. Move the identical
allocation into a function the body calls and it goes to the program-lifetime
region instead:

```mere
region R { let b = strbuf_new () in ... }   // -> mere_strbuf_new(__region_R)
region R { helper 8 }                        // -> mere_strbuf_new(&__lang_default_region)
```

This is deliberate — the runtime's comment says containers "carry identity and
must not die with a scratch block" — but the consequence is that **any contrib
that allocates a container per item is unusable on a redraw path**.
`Grapheme.clusters` makes one `StrBuf` per cluster, so calling it once per
visible line per keystroke leaks about 60 KB a frame: 2,000 frames of 40 lines
of Japanese came to 125 MB, growing linearly, inside a region block.

**Worked around**: `contrib/unicode/width.mere` walks code points via
`utf8_chars` (strings, which the block does reclaim) rather than clusters. The
cost is that an emoji ZWJ sequence counts each pictograph. Documented there
rather than hidden.

## P4 🟡 `tty_raw` leaves IXON on, so the save key freezes the terminal (worked around in the shim)

`tty_raw` clears `ICANON` and `ECHO` and stops. That is enough for a roguelike
reading hjkl. It is not enough for an editor: **Ctrl-S is XOFF**, so the first
time anyone presses the save key the terminal stops drawing and the editor
looks hung. Ctrl-Q is XON, so quitting appears to fix it — a good way never to
find the bug.

## P5 🟡 `tty_raw` leaves ISIG on, so Ctrl-Z never arrives (worked around in the shim)

The same story one key over, and this one is invisible to a piped test. With
`ISIG` kept, Ctrl-Z is SUSP and the editor never sees byte 26, so undo silently
does nothing. Through a pipe there is no line discipline, 0x1a arrives, and
undo works — so the piped test said the feature was finished.

**Both worked around** in `shim/medit2_shim.c` (`term_editor_mode`, clearing
`IXON`/`IXOFF`/`ICRNL`/`ISIG`/`OPOST`). Both are candidates for upstream: "raw"
that leaves flow control and job control on is not raw. It is a behaviour change
for existing users of `tty_raw`, so it wants its own slice.

## P6 🟡 No display width anywhere, and the line-break table cannot supply it

A terminal cursor moves by columns and a Japanese character is two of them. Get
this wrong and every line containing one is drawn in the wrong place.

`contrib/unicode/lb_table.mere`'s generator already reads `EastAsianWidth.txt`
— and keeps one bit, `flag_eastasian`, set for `F`, `W` and `H` together,
because UAX #14 only ever asks "is this East Asian?". `H` is halfwidth katakana:
East Asian and **one** column wide. So the width is not derivable from it.

**Built**: `contrib/unicode/width.mere` + a generated `width_table.mere` +
`scripts/gen_width_table.sh`, checked against Reline with
`scripts/width_check.sh` (17,793 match; every difference has a named reason)
and against a real terminal with `scripts/width_probe_terminal.sh`.

## P7 🟢 Nothing was missing for the language server

The expectation was a new capability: a long-lived child process with
bidirectional pipes. `run` is blocking and inherits stdio; `contrib/os/
subprocess.mere` is one-shot.

It turned out to need **no compiler change at all**. A `socketpair` makes the
child look exactly like an accepted TCP connection, and `tcp_read`/`tcp_write`
are `read(2)`/`write(2)` rather than recv/send — so Mere's existing socket calls
work on it unchanged. `io_poll_*` (v0.1.313) takes any fd, including `fd 0`, so
one `poll(2)` waits on the keyboard and the server together. Eighteen lines of
C in `shim/medit2_shim.c`.

The friction was that **`io_poll_*` is not in `docs/stdlib-reference.md`** — it
lived only in the changelog, and this editor had a busy-polling loop designed
around `stdin_byte` before it was found. Same shape as `file_pwrite_bytes`,
which the mraft dogfood missed for a whole slice for the same reason.
**Documented upstream in v0.1.475.**

## P8 🔴 `\{` escapes an interpolation brace but `\}` is a lex error

```
lex error: unknown escape: \}
```

Writing a JSON literal in a Mere source file means `\{` for the open brace and a
bare `}` for the close. Asymmetric, and the first thing anyone writing a
JSON-RPC client hits.

## P9 🔴 A wrong `extern fn` declaration for a native-FFI name is caught by clang, not by mere

Declaring `mem_copy_str` with one argument too few compiled fine and then:

```
error: too few arguments to function call, expected 3, have 2
```

The compiler implements these names itself (`native_ffi_names` in
`codegen_c.ml`) and does not check the user's declaration against its own.

## P10 🔴 The emitted C warns on every `match` in a pointer-typed expression

```
warning: pointer/integer type mismatch in conditional expression
  ('list_piece' and 'int')
```

The exhaustiveness fall-through arm emits `__lang_fail_impl(...); 0;`, and the
`0` has the wrong type for the rest of the conditional. `__lang_fail_impl` is
`noreturn` so nothing is broken, but the warning fires on every user variant and
would stop a `-Werror` build.

## P11 🟢 A released region freed everything and the process grew anyway (fixed upstream, mere v0.1.475)

The second half of P1, and the one that had to be measured twice because the
bookkeeping was already right.

A loop building a multi-megabyte value inside `region R { }` grew without bound:
40 iterations of a 5 MiB `Vec` reached **216 MB** of resident memory. The first
reading was "the region block does not reclaim it". Counters in the emitted C
said otherwise — `block_release=40`, `freed_chain=40`, `big_allocs=40`. Every
block *was* freed.

What was not happening was **reuse**. Releasing a grown region freed its whole
chain and re-seeded at 1 MiB, so the next iteration asked malloc for those
megabytes again, and malloc did not hand the same pages back.

**Fixed upstream (mere v0.1.475):** the release keeps the region's largest block
and drops the rest. The same loop is **13.7 MB and flat**. The retained block is
capped (`__LANG_REGION_KEEP_MAX`, 16 MiB) so a one-off enormous value is not
held for the rest of the run, and `scripts/region_reclaim_check.sh` pins both
sides of that trade.

**The lesson is about the instrument, not the runtime.** Peak RSS does not
answer "was it reclaimed?" — it answers "how much was resident at once", and
those differ exactly when memory is freed and not reused. The counter answered
it; and patching the hypothesis into the already-emitted C proved the fix was
worth 16× before the compiler was touched.

## not pain

- **`file_openrw` / `file_pread_bytes` address past 2 GB.** A 3,000,000,009-byte
  sparse file reports its size correctly and reads its last nine bytes.
- **The piece table wants exactly what a persistent list gives.** The piece list
  has one entry per edit, not per line, so an undo snapshot is one cons cell no
  matter how large the file is. medit found this for lines; here it is what
  makes undo on a 208 MB file free.
- **`substring` keeps the edit operations short**, which was medit's finding too
  and is still true.
