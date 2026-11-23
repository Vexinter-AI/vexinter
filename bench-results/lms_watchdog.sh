#!/bin/bash
# Keeps /tmp/lms_wanted_model alive forever; empty file = idle.
while true; do
  M=$(cat /tmp/lms_wanted_model 2>/dev/null)
  [ -n "$M" ] && /home/facu/Proyectos/Vexinter/bench-results/lms_ensure.sh "$M" >/dev/null 2>&1
  sleep 30
done
