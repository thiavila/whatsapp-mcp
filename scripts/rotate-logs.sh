#!/bin/bash
# Rotate log files that a supervisor (launchd's StandardOutPath/StandardErrorPath)
# keeps open in append mode. The file is copied and then truncated in place, so
# the running bridge keeps writing to the same file without a restart; the few
# lines written between the copy and the truncation are lost.
#
# Each file larger than --max-mb becomes FILE.1.gz, older copies shift to
# FILE.2.gz ... FILE.<keep>.gz and the oldest is dropped.
#
# Usage: rotate-logs.sh [--max-mb N] [--keep K] FILE...
#        (defaults: --max-mb 20 --keep 3)
#
# Run it periodically, e.g. from a launchd agent with StartInterval 3600 (see
# the README). On Linux under systemd the logs go to the journal, which rotates
# on its own.
set -euo pipefail

max_mb=20
keep=3
while [ $# -gt 0 ]; do
  case "$1" in
    --max-mb) max_mb="$2"; shift 2 ;;
    --keep) keep="$2"; shift 2 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: $0 [--max-mb N] [--keep K] FILE..." >&2; exit 2; }
case "$max_mb$keep" in *[!0-9]*) echo "--max-mb and --keep take whole numbers" >&2; exit 2 ;; esac
[ "$keep" -ge 1 ] || { echo "--keep must be at least 1" >&2; exit 2; }

max_bytes=$((max_mb * 1024 * 1024))
for file in "$@"; do
  [ -f "$file" ] || continue
  size=$(wc -c < "$file" | tr -d ' ')
  [ "$size" -gt "$max_bytes" ] || continue

  rm -f "$file.$keep.gz"
  i=$((keep - 1))
  while [ "$i" -ge 1 ]; do
    [ -f "$file.$i.gz" ] && mv "$file.$i.gz" "$file.$((i + 1)).gz"
    i=$((i - 1))
  done
  cp -p "$file" "$file.1"
  : > "$file"
  gzip -f "$file.1"
  echo "rotated $file ($((size / 1024 / 1024)) MB)"
done
