#!/usr/bin/env bash
# Makes and labels the mutant validation set. Run it through WSL:
#
#   wsl -d Ubuntu -- bash eval/mutants/run.sh generate   commits/*.patch and manifest.json
#   wsl -d Ubuntu -- bash eval/mutants/run.sh baseline   the unmutated code, in the labeling set-up
#   wsl -d Ubuntu -- bash eval/mutants/run.sh label      baseline, then every mutant, then answers.json
#
# Labeling runs the whole sqlglot suite once per mutant in the labeling
# environment (eval/mutants/Dockerfile): LANES containers at a time, each pinned
# to its own CPUS_EACH CPUs so sqlglot's process pools stay that small, with no
# process limit and no network. The unmutated code goes through the very same
# set-up first, once in every lane at the same time. If any of those runs shows
# a failing test the labels would be noise, so nothing is labeled.
#
# Raw logs and per-mutant results go to $MUTANT_OUT (default
# /tmp/blast-radius-mutants), outside the repo. A mutant that already has a
# result there is skipped, so an interrupted run can be resumed.
#
# Then: wsl -d Ubuntu -- bash scripts/eval.sh mutants
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$PWD
IMAGE=blast-radius-label
OUT=${MUTANT_OUT:-/tmp/blast-radius-mutants}
LANES=4
CPUS_EACH=4

in_image() {
  docker run --rm --init --platform linux/amd64 --network none --volume "$ROOT:/repo:ro" "$@"
}

# lane <number> <name>...: label each in turn on this lane's own CPUs. A name is
# a mutant id, or baseN for the unmutated code.
lane() {
  local number=$1 first=$(($1 * CPUS_EACH)) what
  shift
  for what in "$@"; do
    [ -f "$OUT/$what.json" ] && continue
    local arguments=("$what")
    [[ "$what" == base* ]] && arguments=(--base "${what#base}")
    in_image --cpuset-cpus "$first-$((first + CPUS_EACH - 1))" --volume "$OUT:/out" \
      "$IMAGE" python /repo/eval/mutants/label.py run "${arguments[@]}" \
      >>"$OUT/lane$number.log" 2>&1 || echo "LABELING FAILED: $what" >>"$OUT/lane$number.log"
  done
}

baseline() {
  echo "baseline: the unmutated code in $LANES containers at once, $CPUS_EACH CPUs each" >&2
  rm -f "$OUT"/base*.json
  for number in $(seq 0 $((LANES - 1))); do
    lane "$number" "base$((number + 1))" &
  done
  wait
  python3 - "$OUT" "$LANES" <<'PY'
import json, sys
from pathlib import Path

out, lanes = Path(sys.argv[1]), int(sys.argv[2])
clean = True
for number in range(1, lanes + 1):
    path = out / f"base{number}.json"
    if not path.exists():
        print(f"baseline {number}: no result, see {out}/lane{number - 1}.log", file=sys.stderr)
        clean = False
        continue
    label = json.loads(path.read_text())
    bad = label["failing"] or label["uncollected"] or label["failed_but_not_in_tests_json"]
    print(f"baseline {number}: {label['summary']} | {label['seconds']} s | "
          f"{label['tests_that_reported']} tests reported | failing {label['failing']}", file=sys.stderr)
    if bad or label["tests_that_reported"] < 1620:
        clean = False
sys.exit(0 if clean else 1)
PY
}

mkdir -p "$OUT"
case "${1:-}" in
  generate)
    docker build --platform linux/amd64 -f eval/mutants/Dockerfile -t "$IMAGE" . >&2
    in_image --volume "$ROOT/eval/mutants:/target" "$IMAGE" \
      python /repo/eval/mutants/generate.py --out /target
    ;;
  baseline)
    docker build --platform linux/amd64 -f eval/mutants/Dockerfile -t "$IMAGE" . >&2
    baseline
    ;;
  label)
    docker build --platform linux/amd64 -f eval/mutants/Dockerfile -t "$IMAGE" . >&2
    started=$(date +%s)
    baseline || {
      echo "the unmutated code does not come out clean in this set-up: not labeling" >&2
      exit 1
    }
    mapfile -t ids < <(python3 -c "
import json
for entry in json.load(open('eval/mutants/manifest.json'))['mutants']:
    print(entry['id'])")
    echo "labeling ${#ids[@]} mutants" >&2
    for number in $(seq 0 $((LANES - 1))); do
      mine=()
      for index in "${!ids[@]}"; do
        [ $((index % LANES)) -eq "$number" ] && mine+=("${ids[$index]}")
      done
      lane "$number" "${mine[@]}" &
    done
    wait
    in_image --volume "$OUT:/out" --volume "$ROOT/eval/mutants:/target" "$IMAGE" \
      python /repo/eval/mutants/label.py collect /target
    echo "wall time: $(($(date +%s) - started)) s" >&2
    ;;
  *)
    echo "usage: $0 generate|baseline|label" >&2
    exit 2
    ;;
esac
