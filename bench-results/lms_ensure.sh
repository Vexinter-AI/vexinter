#!/bin/bash
# lms_ensure.sh <model-id>: guarantee LM Studio server is up and <model> loaded.
M="$1"
LMS=$HOME/.lmstudio/bin/lms
API="http://localhost:1234"

for i in $(seq 1 30); do
  curl -sf -m 5 "$API/api/v0/models" >/dev/null 2>&1 && break
  $LMS server start >/dev/null 2>&1
  sleep 5
done
curl -sf -m 5 "$API/api/v0/models" >/dev/null 2>&1 || { echo "lms_ensure: server never came up"; exit 1; }

state() {
  curl -sf -m 5 "$API/api/v0/models" | python3 -c "
import json,sys
for m in json.load(sys.stdin).get('data',[]):
    if m['id']=='$M': print(m.get('state','?')); break
else: print('missing')" 2>/dev/null
}

if [ "$(state)" != "loaded" ]; then
  for other in $(curl -sf -m 5 "$API/api/v0/models" | python3 -c "
import json,sys
for m in json.load(sys.stdin).get('data',[]):
    if m.get('state')=='loaded' and m['id']!='$M': print(m['id'])"); do
    $LMS unload "$other" >/dev/null 2>&1
  done
  $LMS load "$M" --context-length 4096 --parallel 1 -y >/dev/null 2>&1
fi

for i in $(seq 1 90); do
  [ "$(state)" = "loaded" ] && exit 0
  sleep 5
done
echo "lms_ensure: $M failed to load"; exit 1
