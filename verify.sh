#!/bin/sh
# verify.sh — build medit2 and run every layer of its tests.
#
# Three layers, because each sees what the others cannot:
#   1. the piece table, against an in-memory origin AND a real file
#   2. the editor under a real pty (a pipe cannot see IXON, ISIG or tty_raw)
#   3. the same pty suite with `mere` on PATH, which adds the language server
#
# Usage:  sh verify.sh              # build and test
#         MERE=/path/to/mere sh verify.sh

set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"

MERE=${MERE:-$(command -v mere || true)}
[ -n "$MERE" ] && [ -x "$MERE" ] || {
  echo "verify: no mere binary. Set MERE=/path/to/mere" >&2; exit 1; }
command -v clang >/dev/null 2>&1 || { echo "verify: clang absent" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "verify: python3 absent" >&2; exit 1; }

fails=0
step() { printf '\n== %s\n' "$1"; }

# --- 1. the piece table ----------------------------------------------------
# Run on BOTH backends: the interpreter is where the logic is easy to believe,
# and the C backend is what ships. A difference between them is its own bug.
step "buffer, interpreter"
"$MERE" test/buffer_test.mere | tail -3
"$MERE" test/buffer_test.mere | grep -q "buffer: all ok" || fails=$((fails + 1))

step "buffer, C backend"
"$MERE" -c test/buffer_test.mere > /tmp/medit2_buffer_test.c
clang -O2 /tmp/medit2_buffer_test.c -o /tmp/medit2_buffer_test 2>/dev/null
/tmp/medit2_buffer_test | tail -3
/tmp/medit2_buffer_test | grep -q "buffer: all ok" || fails=$((fails + 1))

# --- 1a2. the line index ---------------------------------------------------
# The index is LAZY: it covers a prefix of the document and grows on demand.
# The interesting cases are the seam between two pages, and what a truncation
# at an edit leaves behind -- neither of which the pty suite can see, because
# every file it uses fits in the first page.
step "line index"
"$MERE" test/lines_test.mere | tail -2
"$MERE" test/lines_test.mere | grep -q "lines: all ok" || fails=$((fails + 1))

# --- 1b. search ------------------------------------------------------------
# Headless, because the interesting cases are about offsets and window
# boundaries rather than about the screen: a match that straddles a 256 KiB
# boundary, a needle longer than a window, and backward search returning the
# LAST match rather than the first.
step "search"
"$MERE" test/search_test.mere | tail -2
"$MERE" test/search_test.mere | grep -q "search: all ok" || fails=$((fails + 1))

# --- 1c. the language-server client ----------------------------------------
# Headless, because the cases this layer exists for are the ones a working
# server never sends: a stale id, an error object, a split frame, a server
# request whose id collides with ours.
step "lsp client"
"$MERE" test/lsp_test.mere | tail -2
"$MERE" test/lsp_test.mere | grep -q "lsp: all ok" || fails=$((fails + 1))

# --- 2. build the editor ---------------------------------------------------
step "build"
"$MERE" -c medit2.mere > medit2.c
# -Wno-conditional-type-mismatch: the emitted C gives a `match`'s
# exhaustiveness fall-through the placeholder value 0, which has the wrong
# type for the rest of the conditional. __lang_fail_impl is noreturn so it is
# unreachable, but it fires on every user variant. PAIN.md P10.
clang -O2 -Wno-conditional-type-mismatch medit2.c shim/medit2_shim.c -o medit2
echo "built $(ls -la medit2 | awk '{print $5}') bytes"

# --- 3. the editor, under a pty -------------------------------------------
# The language-server scenario needs `mere` on PATH, because that is what the
# editor spawns. Put the one we were given there rather than skipping it --
# a skipped scenario that looks like a pass is the failure mode this avoids.
step "pty, with the language server"
BIN=$(mktemp -d)
ln -sf "$MERE" "$BIN/mere"
PATH="$BIN:$PATH" python3 test/pty_drive.py ./medit2 || fails=$((fails + 1))
rm -rf "$BIN"

# --- 4. the large-file claim ----------------------------------------------
# The README says 208 MB opens without being loaded. A README that says a
# number nothing checks is a README that drifts.
step "a large file is opened without being read"
BIG=/tmp/medit2_verify_big.txt
if [ ! -f "$BIG" ]; then
  python3 -c "
import sys
with open('$BIG','wb') as f:
    line=('x'*79+'\n').encode()
    for _ in range(2_600_000): f.write(line)"
fi
cat > /tmp/medit2_open_check.mere <<'MERE'
import "src/buffer.mere";
import "src/lines.mere";
extern fn now_ms: unit -> int;
let path = "/tmp/medit2_verify_big.txt";
let sz = file_size path;
let f = file_openrw path;
let o = Disk ((f, sz));
let b = Buffer.of_origin o;

// What OPENING costs. The index is lazy: enough of it to draw a screen, and
// no more. This is the number the editor's startup is made of.
let t0 = now_ms ();
let ix = Lines.extend_to_line b (Lines.fresh ()) sz 24;
let t1 = now_ms ();
let _ = print ("open_ms " ++ show (t1 - t0)
               ++ " open_lines " ++ show (Lines.count ix)
               ++ " pieces " ++ show (Buffer.piece_count b));

// And what the WHOLE index costs, which is what a line count or a jump to the
// end pays for. The file still must not be in memory.
let full = Lines.extend_all b ix sz;
let _ = print ("bytes " ++ show sz ++ " lines " ++ show (Lines.count full));
file_close f
MERE
cp /tmp/medit2_open_check.mere ./.verify_open_check.mere
"$MERE" -c ./.verify_open_check.mere > /tmp/medit2_open_check.c
rm -f ./.verify_open_check.mere
clang -O2 /tmp/medit2_open_check.c -o /tmp/medit2_open_check 2>/dev/null
/usr/bin/time -l /tmp/medit2_open_check > /tmp/medit2_open_out.txt 2>/tmp/medit2_open_time.txt
cat /tmp/medit2_open_out.txt
RSS=$(awk '/maximum resident/{print $1}' /tmp/medit2_open_time.txt)
KB=$((RSS / 1024))
echo "peak RSS ${KB} KiB"

# Opening reads ONE PAGE, not the file. Anything more than a handful of lines
# means the index is being built eagerly again, which is what cost 822 ms
# before the first frame on a 1 GB file.
OPEN_LINES=$(awk '{for(i=1;i<=NF;i++) if($i=="open_lines") print $(i+1)}' /tmp/medit2_open_out.txt)
if [ "${OPEN_LINES:-0}" -gt 5000 ]; then
  echo "verify: opening indexed $OPEN_LINES lines -- the index is not lazy" >&2
  fails=$((fails + 1))
fi
OPEN_MS=$(awk '{for(i=1;i<=NF;i++) if($i=="open_ms") print $(i+1)}' /tmp/medit2_open_out.txt)
if [ "${OPEN_MS:-999}" -gt 50 ]; then
  echo "verify: opening took ${OPEN_MS} ms -- it should be one page" >&2
  fails=$((fails + 1))
fi

# And the whole index is still 8 bytes per LINE over a file never loaded.
# 60 MB is generous room over the ~21 MB index and an order of magnitude under
# the 208 MB file.
if [ "$KB" -gt 61440 ]; then
  echo "verify: 208 MB took ${KB} KiB -- the file is being loaded" >&2
  fails=$((fails + 1))
fi

printf '\n'
if [ "$fails" = "0" ]; then
  echo "verify: all ok"
  exit 0
fi
echo "verify: $fails FAILED" >&2
exit 1
