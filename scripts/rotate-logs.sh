#!/bin/bash
# Rotate log files that a supervisor (launchd's StandardOutPath/StandardErrorPath)
# keeps open in append mode. The file is copied and then truncated in place, so
# the running bridge keeps writing to the same file without a restart; the few
# lines written between the copy and the truncation are lost.
#
# Each file larger than --max-mb becomes FILE.1.gz, older copies shift to
# FILE.2.gz ... FILE.<keep>.gz and the oldest is dropped. The copy waits in
# FILE.rotating until it is compressed, so a failed run (full disk, say) loses
# nothing: the next run finishes it before taking a new copy.
#
# Usage: rotate-logs.sh [--max-mb N] [--keep K] FILE...
#        (defaults: --max-mb 20 --keep 3)
#
# Run it periodically, e.g. from a launchd agent with StartInterval 3600 (see
# the README). On Linux under systemd the logs go to the journal, which rotates
# on its own.
set -euo pipefail

usage() { echo "usage: $0 [--max-mb N] [--keep K] FILE..." >&2; exit 2; }
is_count() { case "$1" in '' | *[!0-9]*) return 1 ;; esac; }

max_mb=20
keep=3
while [ $# -gt 0 ]; do
  case "$1" in
    --max-mb | --keep)
      [ $# -ge 2 ] && is_count "$2" || { echo "$1 takes a whole number" >&2; usage; }
      if [ "$1" = --max-mb ]; then max_mb=$((10#$2)); else keep=$((10#$2)); fi
      shift 2 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || usage
[ "$max_mb" -ge 1 ] || { echo "--max-mb must be at least 1" >&2; exit 2; }
[ "$keep" -ge 1 ] && [ "$keep" -le 100 ] || { echo "--keep must be between 1 and 100" >&2; exit 2; }
max_bytes=$((max_mb * 1024 * 1024))

# Steps return on the first failure; set -e does not apply inside a function
# called from an || list, so every step checks its own status.
rotate_file() {
  local file="$1" pending="$1.rotating" size i
  if [ ! -f "$pending" ]; then
    [ -f "$file" ] || return 0
    size=$(wc -c < "$file" | tr -d ' ')
    [ "$size" -gt "$max_bytes" ] || return 0
    cp -p "$file" "$pending" || { rm -f "$pending"; return 1; }
    : > "$file" || return 1
    echo "rotated $file ($((size / 1024 / 1024)) MB)"
  fi
  gzip -c "$pending" > "$pending.gz" || { rm -f "$pending.gz"; return 1; }
  rm -f "$file.$keep.gz"
  i=$((keep - 1))
  while [ "$i" -ge 1 ]; do
    if [ -f "$file.$i.gz" ]; then mv "$file.$i.gz" "$file.$((i + 1)).gz" || return 1; fi
    i=$((i - 1))
  done
  mv "$pending.gz" "$file.1.gz" && rm -f "$pending"
}

# One rotation per file at a time (a manual run may overlap the scheduled one).
# A lock left by a run that died is cleared after an hour.
with_lock() {
  local lock="$1.rotate-lock" rc=0
  if ! mkdir "$lock" 2>/dev/null; then
    [ -d "$lock" ] || { echo "cannot create $lock" >&2; return 1; }
    [ -n "$(find "$lock" -maxdepth 0 -mmin +60 2>/dev/null)" ] && rmdir "$lock" 2>/dev/null
    mkdir "$lock" 2>/dev/null || { echo "skipping $1: another rotation is running" >&2; return 0; }
  fi
  rotate_file "$1" || rc=$?
  rmdir "$lock"
  return "$rc"
}

status=0
for file in "$@"; do
  with_lock "$file" || { echo "failed to rotate $file" >&2; status=1; }
done
exit "$status"
