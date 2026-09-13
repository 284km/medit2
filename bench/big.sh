#!/bin/sh
# bench/big.sh — re-measure the large-file claims at a size you choose.
#
# The README's numbers were taken at 208 MB. This builds files at several
# sizes AND several line lengths and runs bench/big.mere over each, because the
# two axes cost different things:
#
#   FILE SIZE   decides the scan and the open pass -- both are one read of the
#               whole file
#   LINE LENGTH decides the INDEX, which is 8 bytes per line whatever the line
#               holds. A 1 GB file of 80-byte lines has a 107 MB index; the
#               same 1 GB in 8-byte lines has a 1 GB one
#
# So "how big a file can this open" has no single answer, and quoting one
# without the line length is quoting half of it.
#
# Usage:
#   sh bench/big.sh                 # the standard set (needs ~3 GB of /tmp)
#   sh bench/big.sh 1024 80         # one file: MB and bytes-per-line
#   MERE=/path/to/mere sh bench/big.sh
#
# Files are left in $TMPDIR so a second run does not rebuild them. They are
# large; delete /tmp/medit2_bench_* when you are done.

set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

MERE=${MERE:-$(command -v mere || true)}
[ -n "$MERE" ] && [ -x "$MERE" ] || {
  echo "bench: no mere binary. Set MERE=/path/to/mere" >&2; exit 1; }
CC=$(command -v clang || command -v cc || true)
[ -n "$CC" ] || { echo "bench: no C compiler" >&2; exit 1; }

TMP=${TMPDIR:-/tmp}
BIN="$TMP/medit2_bench_big"

# Compiled, not interpreted: the interpreter is another program's speed.
"$MERE" -c bench/big.mere > "$TMP/medit2_bench_big.c"
$CC -O2 "$TMP/medit2_bench_big.c" -o "$BIN"

make_file() {
  # $1 = megabytes, $2 = bytes per line (including the newline)
  path="$TMP/medit2_bench_${1}mb_${2}b.txt"
  if [ -f "$path" ]; then
    echo "$path"
    return
  fi
  python3 - "$path" "$1" "$2" <<'PY'
import sys
path, mb, per = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
# A line of `per` bytes, the last of which is the newline. The payload is not
# random: a compressible file and an incompressible one read at the same speed
# here (nothing compresses), and a fixed payload makes the file reproducible.
line = (b"x" * (per - 1)) + b"\n"
total = mb * 1000 * 1000
n = total // per
with open(path, "wb") as f:
    chunk = line * max(1, 1_000_000 // per)
    written = 0
    while written + len(chunk) <= n * per:
        f.write(chunk); written += len(chunk)
    rest = n * per - written
    if rest:
        f.write(line * (rest // per))
PY
  echo "$path"
}

run_one() {
  mb=$1; per=$2
  path=$(make_file "$mb" "$per")
  printf '\n== %s MB, %s bytes per line\n' "$mb" "$per"
  # /usr/bin/time -l for peak RSS: the program times its own phases, but only
  # the OS can say how much memory the whole run held.
  # Two runs: the first is the editor's own cost, the second adds a rebuild
  # for the shift-vs-rebuild comparison. Separate, because holding two indexes
  # at once is what the peak RSS of a combined run would be reporting.
  /usr/bin/time -l "$BIN" "$path" 2>"$TMP/medit2_bench_time.txt"
  awk '/maximum resident/{printf "peak_rss %d KiB\n", $1/1024}' "$TMP/medit2_bench_time.txt"
  "$BIN" "$path" --rebuild | grep '^rebuild' || true
}

if [ $# -eq 2 ]; then
  run_one "$1" "$2"
  exit 0
fi

# The standard set. 80 bytes per line is source code and logs; 8 is the point
# where the index costs as much as the file it describes, which is the number
# the sparse-index question turns on.
run_one 208 80
run_one 1024 80
run_one 1024 8
