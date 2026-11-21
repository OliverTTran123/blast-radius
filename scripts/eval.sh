#!/usr/bin/env bash
# The one measurement for this track. Run it through WSL:
#
#   wsl -d Ubuntu -- bash scripts/eval.sh
#
# Steps, each of which fails loudly:
#   1. every frozen file still has the sha256 recorded in scripts/frozen.sha256
#   2. the Dockerfile cannot pull the sample answers into the image
#   3. build with the Makefile's own build command
#   4. run the image 3 times under the Makefile's exact RUN_FLAGS, inside the
#      3 minute limit, reading each run's CPU from the container's cgroup
#   5. score every output with score.py
#
# stdout carries exactly one line:
#
#   CHECK simulated=<s> misses=<n> cpu_median=<s> total_median=<s> missed_commits=[ids]
#
# Progress and the per-commit table go to stderr. Artifacts go to $EVAL_OUT
# (default /tmp/blast-radius-eval), outside the repo.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
OUT=${EVAL_OUT:-/tmp/blast-radius-eval}
RUNS=3
LIMIT_S=${LIMIT_S:-180} # README: 3 minutes for the whole batch
STDOUT_LIMIT=2000000    # README: stdout may hold 2 MB
export PYTHONDONTWRITEBYTECODE=1 # no __pycache__ in the repo

# The repo sits on a OneDrive-synced /mnt/c mount. When OneDrive re-tags a
# directory, a process already inside it can lose getcwd() (seen once as a
# FileNotFoundError at Python start-up). So every step re-enters the root and
# programs are called by absolute path.
log() { echo "eval: $*" >&2; }
step() {
  cd "$ROOT"
  log "$*"
}
fail() {
  log "FAILED: $*"
  echo "CHECK FAILED $*" | tee "$OUT/check.txt"
  exit 1
}

mkdir -p "$OUT"
rm -f "$OUT"/run*.jsonl "$OUT"/run*.stderr "$OUT"/run*.score \
  "$OUT/check.txt" "$OUT/per_commit.tsv" "$OUT/build.log"

step "1/5 frozen files"
sha256sum --check --strict --quiet scripts/frozen.sha256 >&2 ||
  fail "a frozen file does not match scripts/frozen.sha256"
recorded=$(sed -n 's/^[0-9a-f]\{64\}  \(sample\/.*\)$/\1/p' scripts/frozen.sha256 | LC_ALL=C sort)
present=$(find sample -type f | LC_ALL=C sort)
[ "$recorded" = "$present" ] || fail "files were added to or removed from sample/"

step "2/5 Dockerfile"
# Join continuation lines so an instruction cannot hide its source on the next line.
flat=$(sed -e ':a' -e '/\\$/N' -e 's/\\\n/ /' -e 'ta' Dockerfile)
whole_context='^[[:space:]]*(COPY|ADD)[[:space:]]+(--[^[:space:]]+[[:space:]]+)*(\[[[:space:]]*")?(\.|\./|\.\.|\*)("|[[:space:]]|$)'
if grep -nEi "$whole_context" <<<"$flat" >&2; then
  fail "Dockerfile copies the whole build context; give each path its own COPY line"
fi
if grep -nEi 'answers|^[[:space:]]*(COPY|ADD)[[:space:]].*sample|type=bind' <<<"$flat" >&2; then
  fail "Dockerfile mentions the answers, copies from sample/ or bind-mounts the context"
fi

make_var() { make --no-print-directory --eval "eval-print: ; @echo \$($1)" eval-print; }
IMAGE=$(make_var IMAGE)
read -r -a RUN_FLAGS <<<"$(make_var RUN_FLAGS)"
[ -n "$IMAGE" ] && [ "${#RUN_FLAGS[@]}" -gt 0 ] || fail "could not read IMAGE and RUN_FLAGS from the Makefile"

step "3/5 build $IMAGE"
make --no-print-directory build >"$OUT/build.log" 2>&1 || {
  tail -n 40 "$OUT/build.log" >&2
  fail "docker build failed"
}

# The image's own entrypoint and command, so the wrapper runs exactly what the scorer would.
mapfile -d '' -t IMAGE_ARGV < <(docker inspect "$IMAGE" | python3 -c '
import json, sys
config = json.load(sys.stdin)[0]["Config"]
words = (config.get("Entrypoint") or []) + (config.get("Cmd") or [])
sys.stdout.write("".join(word + "\0" for word in words))')
[ "${#IMAGE_ARGV[@]}" -gt 0 ] || fail "the image has no command"

step "4/5 $RUNS runs under the Makefile's RUN_FLAGS: ${IMAGE_ARGV[*]}"
for run in $(seq 1 "$RUNS"); do
  cd "$ROOT"
  name="blast-radius-eval-$$-$run"
  started=$(date +%s.%N)
  set +e
  timeout --kill-after=5 "$LIMIT_S" docker run --name "$name" "${RUN_FLAGS[@]}" \
    --volume "$ROOT/scripts/cpu_wrap.sh:/eval/cpu_wrap.sh:ro" \
    --entrypoint /bin/sh "$IMAGE" /eval/cpu_wrap.sh "${IMAGE_ARGV[@]}" \
    >"$OUT/run$run.jsonl" 2>"$OUT/run$run.stderr"
  status=$?
  set -e
  wall=$(python3 -c "import sys; print('%.3f' % (float(sys.argv[2]) - float(sys.argv[1])))" "$started" "$(date +%s.%N)")
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    docker kill "$name" >/dev/null 2>&1 || true
    fail "run $run did not finish within ${LIMIT_S} s"
  fi
  if [ "$status" -ne 0 ]; then
    tail -n 20 "$OUT/run$run.stderr" >&2
    fail "run $run exited with status $status"
  fi

  bytes=$(stat -c %s "$OUT/run$run.jsonl")
  [ "$bytes" -le "$STDOUT_LIMIT" ] || fail "run $run printed $bytes bytes, over the $STDOUT_LIMIT byte stdout limit"

  cpu_usec=$(awk '/^=== cpu.stat ===$/ { seen = 1 } seen && /^usage_usec / { value = $2 } END { print value }' "$OUT/run$run.stderr")
  [[ "$cpu_usec" =~ ^[0-9]+$ ]] || fail "run $run: no usage_usec in the container's cpu.stat"
  cpu=$(printf '%d.%06d' $((cpu_usec / 1000000)) $((cpu_usec % 1000000)))

  python3 "$ROOT/score.py" "$ROOT/sample" "$ROOT/sample/answers.json" "$OUT/run$run.jsonl" \
    --cpu-seconds "$cpu" >"$OUT/run$run.score" 2>&1 || {
    cat "$OUT/run$run.score" >&2
    fail "run $run: $(head -n 1 "$OUT/run$run.score")"
  }
  log "    run $run: cpu=${cpu}s wall=${wall}s stdout=${bytes}B"
done

step "5/5 report"
set +e
python3 "$ROOT/scripts/eval_report.py" "$OUT" "$RUNS" | tee "$OUT/check.txt"
status=${PIPESTATUS[0]}
set -e
log "selector stderr of run 1: $OUT/run1.stderr"
exit "$status"
