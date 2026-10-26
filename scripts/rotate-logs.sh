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
# Whole numbers of at most 6 digits, so the arithmetic below cannot overflow.
is_count() { case "$1" in '' | *[!0-9]*) return 1 ;; esac; [ ${#1} -le 6 ]; }

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

# One run at a time (a manual run may overlap the scheduled one). The script
# re-runs itself through perl (shipped with macOS), which takes a kernel flock
# and hands the locked descriptor to the new bash process: the lock lives as
# long as this script and the commands it runs, and the kernel drops it when
# they exit, so a crashed run leaves nothing to clean up. The lock file sits in
# a private directory under $HOME, the same for launchd and a login shell.
lock_dir="${XDG_CACHE_HOME:-$HOME/.cache}/rotate-logs"
# Already running under the lock: the inherited descriptor is the lock file.
# (Compared by inode, since macOS reports another device for /dev/fd/N.)
inode() { ls -diL "$1" 2>/dev/null | awk '{print $1}'; }
holds_lock() {
  [ -n "${ROTATE_LOGS_LOCK_FD:-}" ] && [ -e "/dev/fd/$ROTATE_LOGS_LOCK_FD" ] && [ -f "$lock_dir/lock" ] &&
    [ "$(inode "/dev/fd/$ROTATE_LOGS_LOCK_FD")" = "$(inode "$lock_dir/lock")" ]
}
if ! holds_lock; then
  mkdir -p "$lock_dir" && chmod 700 "$lock_dir"
  exec perl -e '
    use Fcntl qw(:DEFAULT :flock F_GETFD F_SETFD FD_CLOEXEC);
    my $lock = shift;
    sysopen(my $fh, $lock, O_WRONLY | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0600)
      or die "cannot open lock $lock: $!\n";
    -f $fh or die "lock $lock is not a regular file\n";
    unless (flock($fh, LOCK_EX | LOCK_NB)) {
      die "cannot lock $lock: $!\n" unless $!{EWOULDBLOCK} || $!{EAGAIN};
      print STDERR "skipping: another rotation is running\n";
      exit 0;
    }
    my $flags = fcntl($fh, F_GETFD, 0) or die "fcntl: $!\n";
    fcntl($fh, F_SETFD, $flags & ~FD_CLOEXEC) or die "fcntl: $!\n";
    $ENV{ROTATE_LOGS_LOCK_FD} = fileno($fh);
    exec { $ARGV[0] } @ARGV or die "cannot run $ARGV[0]: $!\n";
  ' "$lock_dir/lock" "$BASH" "$0" --max-mb "$max_mb" --keep "$keep" -- "$@"
fi

# Steps return on the first failure; set -e does not apply inside a function
# called from an || list, so every step checks its own status.
rotate_file() {
  local file="$1" pending="$1.rotating" size
  if [ ! -f "$pending" ]; then
    [ -f "$file" ] || return 0
    size=$(wc -c < "$file" | tr -d ' ') || return 1
    case "$size" in '' | *[!0-9]*) return 1 ;; esac
    [ "$size" -gt "$max_bytes" ] || return 0
    cp -p "$file" "$pending" || { rm -f "$pending"; return 1; }
    : > "$file" || return 1
    echo "rotated $file ($((size / 1024 / 1024)) MB)"
  fi
  gzip -c "$pending" > "$pending.gz" || { rm -f "$pending.gz"; return 1; }
  make_room "$file" 1 || return 1
  mv "$pending.gz" "$file.1.gz" && rm -f "$pending"
}

# Free slot FILE.<n>.gz by shifting the copies above it up one, dropping the one
# in the last slot. It shifts only what is in the way, so after a run that
# failed half-way the next run picks up where it stopped without losing a copy.
make_room() {
  local file="$1" n="$2"
  [ -e "$file.$n.gz" ] || return 0
  if [ "$n" -ge "$keep" ]; then rm -f "$file.$n.gz"; return; fi
  make_room "$file" $((n + 1)) && mv "$file.$n.gz" "$file.$((n + 1)).gz"
}

status=0
for file in "$@"; do
  rotate_file "$file" || { echo "failed to rotate $file" >&2; status=1; }
done
exit "$status"
