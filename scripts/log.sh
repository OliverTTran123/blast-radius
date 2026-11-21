#!/bin/sh
# Append one attempt to decisions.tsv. The CHECK line is not typed in: it is
# read from the last scripts/eval.sh run, so a row can only hold a real result.
#
# Usage: scripts/log.sh <kept|reverted> "<change>" "<note>"
set -eu
cd "$(dirname "$0")/.."
LOG=decisions.tsv
CHECK_FILE=${EVAL_OUT:-/tmp/blast-radius-eval}/check.txt

[ $# -eq 3 ] || { echo "usage: $0 <kept|reverted> \"<change>\" \"<note>\"" >&2; exit 2; }
case "$1" in
  kept | reverted) ;;
  *) echo "verdict must be kept or reverted, not '$1'" >&2; exit 2 ;;
esac
[ -s "$CHECK_FILE" ] || { echo "no CHECK line at $CHECK_FILE: run scripts/eval.sh first" >&2; exit 1; }
check=$(head -n 1 "$CHECK_FILE")
case "$check" in
  "CHECK "*) ;;
  *) echo "$CHECK_FILE does not hold a CHECK line" >&2; exit 1 ;;
esac

clean() {
  # one line, no tabs, and neutralize spreadsheet formula starts
  value=$(printf '%s' "$1" | tr '\t\n' '  ')
  case "$value" in
    =* | +* | -* | @*) value="'$value" ;;
  esac
  printf '%s' "$value"
}

[ -f "$LOG" ] || printf 'date\tchange\tcheck\tverdict\tnote\n' >"$LOG"
printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(clean "$2")" "$check" "$1" "$(clean "$3")" >>"$LOG"
echo "logged ($1): $check"
