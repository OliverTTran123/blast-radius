#!/usr/bin/env bash
# Rebuilds map/ from the pinned sqlglot commit. Run it through WSL:
#
#   wsl -d Ubuntu -- bash tools/build_map/run.sh
#
# It builds the labeling environment (tools/build_map/Dockerfile), runs every
# test alone and the whole suite once under coverage, and writes the compact
# map the selector reads. About 30 minutes on a 22-thread machine.
#
# map/never_verify.json is not made here. It comes from running every test in
# the scored image, so after a rebuild run scripts/selfcheck.sh to remake it.
#
# Raw records go to a Docker volume, not to the repo. An isolated pass skips
# tests it already has a record for, so remove the volume to start clean:
#
#   docker volume rm blast-radius-map-work
#
# SEEDS lists the PYTHONHASHSEED values to run the isolated pass with; the map
# is the union. The committed map used 0 and 1, which agreed on every line of
# every test, so one seed gives the same files apart from the pass count in
# summary.json. JOBS is how many tests run at once.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$PWD
IMAGE=blast-radius-map
VOLUME=${MAP_VOLUME:-blast-radius-map-work}
JOBS=${JOBS:-18}
SEEDS=${SEEDS:-0 1}

docker build --platform linux/amd64 -f tools/build_map/Dockerfile -t "$IMAGE" .

# The builder sees the test list and nothing else from sample/.
in_image() {
  docker run --rm --init --platform linux/amd64 \
    --volume "$VOLUME:/work" \
    --volume "$ROOT/sample/tests.json:/data/tests.json:ro" \
    "$@"
}

isolated=()
for seed in $SEEDS; do
  in_image "$IMAGE" python /tool/collect.py isolated \
    --tests /data/tests.json --work "/work/iso$seed" --jobs "$JOBS" --seed "$seed"
  isolated+=("/work/iso$seed")
done

in_image "$IMAGE" python /tool/collect.py suite \
  --tests /data/tests.json --work /work/suite0 --seed 0

mkdir -p "$ROOT/map"
in_image --volume "$ROOT/map:/out" "$IMAGE" python /tool/assemble.py \
  --tests /data/tests.json --iso "${isolated[@]}" --suite /work/suite0 --out /out

ls -l "$ROOT/map"
