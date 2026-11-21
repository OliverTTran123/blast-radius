#!/bin/sh
# Mounted read-only into the container by scripts/eval.sh, never copied into
# the image. Runs the image's own command, then prints the container's cgroup
# CPU counters to stderr. usage_usec is user + system for every process in the
# container since it started, which is what SCORING.md says the board adds.
"$@"
status=$?
echo "=== cpu.stat ===" >&2
cat /sys/fs/cgroup/cpu.stat >&2
exit $status
