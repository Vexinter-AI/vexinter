#!/bin/bash
# llama_ensure.sh <alias> <gguf-path>: guarantee llama-server is up on :8080
# serving the requested model. Uses LM Studio's bundled Vulkan llama-server
# binary directly (no Electron/Chromium app). Switches models by restart.
ALIAS="$1"; GGUF="$2"
BIN=$HOME/.lmstudio/extensions/backends/llama.cpp-linux-x86_64-vulkan-avx2-2.73.0/llama-server
API="http://127.0.0.1:8080"
LOGDIR=$HOME/local/llama-logs
mkdir -p "$LOGDIR"

loaded() { curl -sf -m 5 "$API/v1/models" | grep -q "\"id\"[[:space:]]*:[[:space:]]*\"$ALIAS\""; }

if ! curl -sf -m 5 "$API/health" >/dev/null 2>&1; then
  pkill -f "llama-server.*--port 8080" 2>/dev/null
  sleep 2
fi

if ! loaded; then
  pkill -f "port 8080" 2>/dev/null
  sleep 3
  nohup "$BIN" -m "$GGUF" --alias "$ALIAS" \
    --host 127.0.0.1 --port 8080 -c 2048 -np 1 --jinja \
    -ngl 99 --flash-attn on \
    > "$LOGDIR/$ALIAS.log" 2>&1 &
  for i in $(seq 1 90); do
    loaded && exit 0
    pgrep -f "llama-server.*$ALIAS" >/dev/null || { sleep 2; break; }
    sleep 5
  done
  echo "llama_ensure: $ALIAS failed to load"; exit 1
fi
exit 0
