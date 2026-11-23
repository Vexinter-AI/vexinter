#!/bin/bash
# Real Invoice Ninja benchmark: Huihui Qwen3.5-4B abliterated via the
# local llama.cpp Vulkan server. All 7 fault classes x 4 arms x n=10
# (same matrix as the gpt-4o-mini run in BENCHMARK.md §4.2).
# Instruct model, not a reasoning model: Nemotron-3-Nano degenerated
# into multi-minute generations on the real adapter's larger schemas.
#
# Waits for DONE-BILLING-NEMOTRON first: the llama server has a single
# slot, so the two benchmarks must not overlap.
#
# Requires INVOICENINJA_URL + INVOICENINJA_API_TOKEN (see systemd
# EnvironmentFile). Resumable via --resume-log. Intended to run under
# systemd (vexbench-invoiceninja.service).
cd /home/facu/Proyectos/Vexinter || exit 1
export VEXINTER_DEV_MODE=1

: "${INVOICENINJA_URL:?set INVOICENINJA_URL to the local laboratory}"
: "${INVOICENINJA_API_TOKEN:?set INVOICENINJA_API_TOKEN}"

B="http://127.0.0.1:8080/v1"
PY=.venv/bin/python
D=bench-results
ENSURE=$D/llama_ensure.sh
DONE=$D/DONE-INVOICENINJA-Q35
BILLING_DONE=$D/DONE-BILLING-NEMOTRON

ALIAS=qwen3.5-4b-abliterated
GGUF=/home/facu/.var/app/ai.lmstudio.lm-studio/.lmstudio/models/mradermacher/Huihui-Qwen3.5-4B-abliterated-GGUF/Huihui-Qwen3.5-4B-abliterated.Q4_K_S.gguf
TAG=${TAG:-invoiceninja-q35-main}
LOG=$D/${TAG}.log
FAULTS=${FAULTS:-control,false_success,filtered_effect,lost_response,cut_before,blind_honest,blind_false_success}

[ -f "$DONE" ] && { echo "skip $TAG (done)"; exit 0; }

# single-slot llama server: wait for the billing run to finish
while [ ! -f "$BILLING_DONE" ]; do
  echo "waiting for $BILLING_DONE ($(date))"
  sleep 300
done

for attempt in 1 2 3 4 5; do
  echo "=== $TAG attempt=$attempt $(date) ===" | tee -a "$LOG"
  $ENSURE "$ALIAS" "$GGUF" || { echo "ENSURE FAILED $ALIAS"; sleep 30; continue; }
  $PY -m examples.bench_real --n "${N:-10}" --faults "$FAULTS" \
    --model "$ALIAS" --base-url "$B" --config "$TAG" \
    --resume-log "$LOG" >> "$LOG" 2>&1
  RUNS=$(grep -c "^\[" "$LOG" || true)
  ERRS=$(grep -c " ERROR" "$LOG" || true)
  OK=$(( RUNS - ERRS ))
  if [ "$RUNS" -gt 0 ] && [ $(( OK * 10 )) -ge $(( RUNS * 9 )) ]; then
    touch "$DONE"; echo "ALL DONE INVOICENINJA-NEMOTRON"; exit 0
  fi
  echo "config $TAG incomplete or too many errors ($ERRS/$RUNS) — retrying"
  sleep 10
done
echo "$TAG FAILED after 5 attempts"; exit 1
