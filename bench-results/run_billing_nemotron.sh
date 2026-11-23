#!/bin/bash
# Billing simulator benchmark: NVIDIA Nemotron-3-Nano-4B via local
# llama.cpp Vulkan server. 5 fault classes x 4 arms x n=10 = 200 runs.
# Resumable: completed runs are folded from --resume-log on restart.
# Intended to run under systemd (vexbench-billing-nemotron.service).
cd /home/facu/Proyectos/Vexinter || exit 1
export VEXINTER_DEV_MODE=1

B="http://127.0.0.1:8080/v1"
PY=.venv/bin/python
D=bench-results
ENSURE=$D/llama_ensure.sh
DONE=$D/DONE-BILLING-NEMOTRON

ALIAS=nemotron-3-nano
GGUF=/home/facu/.lmstudio/models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf
TAG=billing-nemotron-main
LOG=$D/${TAG}.log
FAULTS=control,false_success,lost_response,cut_before,blind_false_success

[ -f "$DONE" ] && { echo "skip $TAG (done)"; exit 0; }

for attempt in 1 2 3 4 5; do
  echo "=== $TAG attempt=$attempt $(date) ===" | tee -a "$LOG"
  $ENSURE "$ALIAS" "$GGUF" || { echo "ENSURE FAILED $ALIAS"; sleep 30; continue; }
  $PY -m examples.bench_llm --n 10 --faults "$FAULTS" \
    --model "$ALIAS" --base-url "$B" --config "$TAG" \
    --resume-log "$LOG" >> "$LOG" 2>&1
  # footer alone is not enough: an all-ERROR pass also prints it.
  # Require >=90% of run lines to be non-error.
  RUNS=$(grep -c "^\[" "$LOG" || true)
  ERRS=$(grep -c " ERROR" "$LOG" || true)
  OK=$(( RUNS - ERRS ))
  if [ "$RUNS" -gt 0 ] && [ $(( OK * 10 )) -ge $(( RUNS * 9 )) ]; then
    touch "$DONE"; echo "ALL DONE BILLING-NEMOTRON"; exit 0
  fi
  echo "config $TAG incomplete or too many errors ($ERRS/$RUNS) — retrying"
  sleep 10
done
echo "$TAG FAILED after 5 attempts"; exit 1
