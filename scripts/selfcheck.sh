#!/usr/bin/env bash
# Runs the runner's self-check in the scored image, under the Makefile's own
# RUN_FLAGS (2 CPUs, 256 processes, read-only disk, 64 MB /tmp, user nobody):
# every test once, alone, on the unpatched pinned code. Run it through WSL:
#
#   wsl -d Ubuntu -- bash scripts/selfcheck.sh [arguments for runner.py selfcheck]
#
# It writes map/never_verify.json, the tests the runner must never judge by
# running them, each with its reasons, and prints what the pass measured: the
# parent's start-up CPU, the CPU of a fork, and the CPU a test takes here
# against the seconds tests.json records for it. The whole report goes to
# $EVAL_OUT/selfcheck.json (default /tmp/blast-radius-eval), outside the repo.
#
# With arguments (say --only test_athena to try a few tests) the pass is
# partial, so map/never_verify.json is left alone.
# SELFCHECK_DOCKER lists extra docker run flags, for example a --cpuset-cpus.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
OUT=${EVAL_OUT:-/tmp/blast-radius-eval}
mkdir -p "$OUT"

make_var() { make --no-print-directory --eval "eval-print: ; @echo \$($1)" eval-print; }
IMAGE=$(make_var IMAGE)
read -r -a RUN_FLAGS <<<"$(make_var RUN_FLAGS)"
read -r -a EXTRA <<<"${SELFCHECK_DOCKER:-}"

make --no-print-directory build >"$OUT/selfcheck-build.log" 2>&1 || {
  tail -n 40 "$OUT/selfcheck-build.log" >&2
  exit 1
}
started=$(date +%s)
docker run "${RUN_FLAGS[@]}" "${EXTRA[@]}" "$IMAGE" python /app/runner.py selfcheck "$@" \
  >"$OUT/selfcheck.json" 2>"$OUT/selfcheck.stderr" || {
  tail -n 40 "$OUT/selfcheck.stderr" >&2
  exit 1
}
echo "selfcheck: $(($(date +%s) - started)) s under: ${RUN_FLAGS[*]} ${EXTRA[*]}" >&2
target=$ROOT/map/never_verify.json
[ "$#" -eq 0 ] || target=-
python3 "$ROOT/scripts/selfcheck_report.py" "$OUT/selfcheck.json" "$target"
