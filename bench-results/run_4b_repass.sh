#!/bin/bash
# Waits for the main 4B run, then re-runs any config whose log has a
# suspiciously high ERROR rate (server crashes), then exits.
D=/home/facu/Proyectos/Vexinter/bench-results
while [ ! -f "$D/DONE-4B" ]; do sleep 60; done
sleep 30
cd "$D" || exit 1
for log in *-nemotron-*.log *-gemma-*.log; do
  [ -f "$log" ] || continue
  total=$(grep -c '^\[' "$log" || true)
  errs=$(grep -c 'ERROR' "$log" || true)
  [ "$total" -eq 0 ] && continue
  if [ "$errs" -gt 3 ] && [ $((errs * 20)) -gt "$total" ]; then
    base=${log%.log}
    echo "dirty config: $base ($errs/$total errors) — scheduling rerun"
    rm -f ".done_$(echo "$base" | tr '-' '_')" "$log"
  fi
done
rm -f "$D/DONE-4B"
exec "$D/run_4b.sh"
