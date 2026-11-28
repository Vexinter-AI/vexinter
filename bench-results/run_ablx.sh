#!/bin/bash
# Abliterated-model benchmark: Qwen2.5-3B-Instruct-abliterated on llama.cpp.
# Tag "ablx". Runs main + attacker per domain; resumable via sentinels.
cd "$(dirname "$0")/.." || exit 1
export VEXINTER_DEV_MODE=1
B="http://127.0.0.1:8080/v1"
PY=.venv/bin/python
D=bench-results
ENSURE=$D/llama_ensure.sh

ALIAS=qwen2.5-3b-abliterated
GGUF=$HOME/.lmstudio/models/mradermacher/Qwen2.5-3B-Instruct-abliterated-GGUF/Qwen2.5-3B-Instruct-abliterated.Q4_K_S.gguf
TAG=ablx

run_config() {  # $1=dom $2=name $3...=extra bench args
  local DOM=$1 NAME=$2; shift 2
  local LOG=$D/${DOM}-${TAG}-${NAME}.log SENT=$D/.done_${DOM}_${TAG}_${NAME}
  [ -f "$SENT" ] && { echo "skip $DOM/$TAG/$NAME (done)"; return 0; }
  for attempt in 1 2 3; do
    echo "=== $TAG $DOM $NAME attempt=$attempt $(date) ==="
    $ENSURE "$ALIAS" "$GGUF" || { echo "ENSURE FAILED $ALIAS"; sleep 30; continue; }
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

for DOM in inventory helpdesk; do
  run_config $DOM main || exit 1
  run_config $DOM attacker --faults control,false_success,lost_response --adversary attacker || exit 1
done

echo "ALL DONE ABLX" > $D/DONE-ABLX
echo "=== FIN $(date) ==="
