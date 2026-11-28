#!/bin/bash
# Memory watchdog for the vexbench units: pause a benchmark only under
# SUSTAINED real pressure (the bench itself keeps MemAvailable in the
# ~500-900MB range during normal operation, so a single low reading is
# not a signal). Resume when there is clear headroom.
# Per-run resume in bench_domains makes each pause cost ~1 run.
# unit -> done marker file. Units whose marker exists are finished:
# never stop OR start them (auto-starting a finished unit makes its
# script reload the model every tick -> memory churn -> OOM loop).
UNITS="vexbench-llama.service bench-results/DONE-LLAMA
vexbench-cycler.service bench-results/CYCLER-VK-DONE2
vexbench-attacker.service bench-results/DONE-ABLX
vexbench-q35.service bench-results/DONE-Q35
vexbench-billing-nemotron.service bench-results/DONE-BILLING-NEMOTRON
vexbench-invoiceninja.service bench-results/DONE-INVOICENINJA-Q35"
AVAIL_KB=$(awk '/MemAvailable/{print $2}' /proc/meminfo)

# Guard: LM Studio spawns its own llama-server instances (always with
# --api-key in cmdline; ours never has it). Kill them — they eat ~4GB
# each and starve the benchmark.
for pid in $(pgrep -f "llama-server.*--api-key"); do
  echo "$(date '+%F %T') killing foreign llama-server (LM Studio) pid=$pid"
  kill "$pid" 2>/dev/null
done

while read -r UNIT DONE_MARK; do
  [ -z "$UNIT" ] && continue
  [ -f "$(dirname "$0")/../$DONE_MARK" ] && continue
  STATE=/tmp/vexbench_watchdog_low_$(echo "$UNIT" | tr '.-' '__')
  ACTIVE=$(systemctl --user is-active "$UNIT" 2>/dev/null)
  if [ "$ACTIVE" = "active" ]; then
    if [ "$AVAIL_KB" -lt 400000 ]; then
      # require 3 consecutive low readings (~6min) before pausing
      n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$STATE"
      if [ "$n" -ge 3 ]; then
        echo "$(date '+%F %T') sustained low memory (${AVAIL_KB}kB) — stopping $UNIT"
        systemctl --user stop "$UNIT"
        rm -f "$STATE"
      fi
    else
      rm -f "$STATE"
    fi
  elif [ "$ACTIVE" != "active" ] && [ "$AVAIL_KB" -gt 2000000 ]; then
    # only auto-start units that were meant to run: skip ones with no
    # pending work would just no-op anyway, so starting is harmless.
    echo "$(date '+%F %T') memory recovered (${AVAIL_KB}kB) — starting $UNIT"
    systemctl --user start "$UNIT"
  fi
done <<EOF
$UNITS
EOF
