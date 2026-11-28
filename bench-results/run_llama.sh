#!/bin/bash
# Resumable 4B benchmark runner on llama.cpp (llama-server :8080).
# Same sentinel scheme as run_4b.sh: .done_<dom>_<tag>_<cfg>.
# Tag "gemmalx" keeps llama.cpp results separate from the LM Studio ones.
cd "$(dirname "$0")/.." || exit 1
export VEXINTER_DEV_MODE=1
B="http://127.0.0.1:8080/v1"
PY=.venv/bin/python
D=bench-results
ENSURE=$D/llama_ensure.sh

GEMMA_GGUF=$HOME/.lmstudio/models/lmstudio-community/gemma-4-E2B-it-GGUF/gemma-4-E2B-it-Q4_K_M.gguf
NEMO_GGUF=$HOME/.lmstudio/models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf

run_config() {  # $1=dom $2=alias $3=gguf $4=tag $5=name $6...=extra bench args
  local DOM=$1 ALIAS=$2 GGUF=$3 TAG=$4 NAME=$5; shift 5
  local LOG=$D/${DOM}-${TAG}-${NAME}.log SENT=$D/.done_${DOM}_${TAG}_${NAME}
  [ -f "$SENT" ] && { echo "skip $DOM/$TAG/$NAME (done)"; return 0; }
  for attempt in 1 2 3; do
    echo "=== $TAG $DOM $NAME attempt=$attempt $(date) ==="
    $ENSURE "$ALIAS" "$GGUF" || { echo "ENSURE FAILED $ALIAS"; sleep 30; continue; }
    # Fresh start if the old log already has a footer (finished/corrupt
    # previous life); otherwise append so --resume-log can skip done runs.
    if [ -f "$LOG" ] && tail -5 "$LOG" | grep -q "tokens: input="; then
      : > "$LOG"
    fi
    $PY -m examples.bench_domains --domain $DOM --n 5 --model "$ALIAS" --base-url $B --config ${DOM}-${TAG}-${NAME} --resume-log "$LOG" "$@" \
      >> "$LOG" 2>&1
    if tail -5 "$LOG" | grep -q "tokens: input=" && ! tail -5 "$LOG" | grep -q "tokens: input=0,"; then
      # footer alone is not enough: an all-ERROR pass also prints it.
      # Require >=90% of run lines to be non-error.
      RUNS=$(grep -c "^\[" "$LOG" || true)
      ERRS=$(grep -c " ERROR" "$LOG" || true)
      OK=$(( RUNS - ERRS ))
      if [ "$RUNS" -gt 0 ] && [ $(( OK * 10 )) -ge $(( RUNS * 9 )) ]; then
        touch "$SENT"; return 0
      fi
      echo "config finished but $ERRS/$RUNS runs errored — retrying"
    fi
    echo "config incomplete (no footer) — retrying"
    sleep 10
  done
  echo "config $DOM/$TAG/$NAME FAILED after 3 attempts"; return 1
}

run_set() {  # $1=alias $2=gguf $3=tag
  local ALIAS=$1 GGUF=$2 TAG=$3 DOM
  for DOM in inventory helpdesk; do
    run_config $DOM $ALIAS $GGUF $TAG main || return 1
    for ADV in liar cycler cascade; do
      run_config $DOM $ALIAS $GGUF $TAG $ADV --faults control,false_success,lost_response --adversary $ADV || return 1
    done
    run_config $DOM $ALIAS $GGUF $TAG poison --faults control,false_success,lost_response --poison || return 1
  done
}

# gemma phase (llama.cpp)
run_set gemma-4-e2b "$GEMMA_GGUF" gemmalx || exit 1

# nemotron phase (llama.cpp) — clean re-run of the LM Studio column
run_set nemotron-3-nano "$NEMO_GGUF" nemlx || exit 1

rm -f /tmp/lms_wanted_model
echo "ALL DONE LLAMA" > $D/DONE-LLAMA
echo "=== FIN $(date) ==="
