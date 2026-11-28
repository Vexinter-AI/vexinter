#!/bin/bash
# Resumable 4B benchmark runner. Sentinels .done_<dom>_<tag>_<cfg> mark finished
# configs; systemd Restart=on-failure resumes after crashes/reboots.
cd "$(dirname "$0")/.." || exit 1
export VEXINTER_DEV_MODE=1
B="http://localhost:1234/v1"
PY=.venv/bin/python
D=bench-results
ENSURE=$D/lms_ensure.sh

run_config() {  # $1=dom $2=model $3=tag $4=name $5...=extra bench args
  local DOM=$1 M=$2 TAG=$3 NAME=$4; shift 4
  local LOG=$D/${DOM}-${TAG}-${NAME}.log SENT=$D/.done_${DOM}_${TAG}_${NAME}
  [ -f "$SENT" ] && { echo "skip $DOM/$TAG/$NAME (done)"; return 0; }
  for attempt in 1 2 3; do
    echo "=== $TAG $DOM $NAME attempt=$attempt $(date) ==="
    $ENSURE "$M" || { echo "ENSURE FAILED $M"; sleep 30; continue; }
    echo "$M" > /tmp/lms_wanted_model
    $PY -m examples.bench_domains --domain $DOM --n 5 --model $M --base-url $B --config ${DOM}-${TAG}-${NAME} "$@" \
      > "$LOG" 2>&1
    if tail -5 "$LOG" | grep -q "tokens: input="; then
      touch "$SENT"; return 0
    fi
    echo "config incomplete (no footer) — retrying"
    sleep 10
  done
  echo "config $DOM/$TAG/$NAME FAILED after 3 attempts"; return 1
}

run_set() {  # $1=model $2=tag
  local M=$1 TAG=$2 DOM
  for DOM in inventario helpdesk; do
    run_config $DOM $M $TAG main || return 1
    for ADV in liar cycler cascade; do
      run_config $DOM $M $TAG $ADV --faults control,false_success,lost_response --adversary $ADV || return 1
    done
    run_config $DOM $M $TAG poison --faults control,false_success,lost_response --poison || return 1
  done
}

# nemotron phase
$HOME/.lmstudio/bin/lms unload nvidia/nemotron-3-nano-4b 2>/dev/null
$HOME/.lmstudio/bin/lms unload google/gemma-4-e2b 2>/dev/null
$HOME/.lmstudio/bin/lms unload qwen2.5-3b-instruct-abliterated 2>/dev/null
$HOME/.lmstudio/bin/lms unload qwen2.5-7b-instruct-abliterated-v2 2>/dev/null
$HOME/.lmstudio/bin/lms unload qwen/qwen3-vl-4b 2>/dev/null
echo "nvidia/nemotron-3-nano-4b" > /tmp/lms_wanted_model
run_set nvidia/nemotron-3-nano-4b nemotron || exit 1

# gemma phase
$HOME/.lmstudio/bin/lms unload nvidia/nemotron-3-nano-4b 2>/dev/null
echo "google/gemma-4-e2b" > /tmp/lms_wanted_model
run_set google/gemma-4-e2b gemma || exit 1

rm -f /tmp/lms_wanted_model
echo "ALL DONE 4B" > $D/DONE-4B
echo "=== FIN $(date) ==="
