#!/bin/bash
# LLM-agent variants for comms + memory layers (q35, resumable)
cd "$(dirname "$0")/.."
export VEXINTER_DEV_MODE=1
PY=.venv/bin/python
URL=http://127.0.0.1:8080/v1
M=q35

$PY -m examples.bench_comms_llm --base-url $URL --model $M --n 10 \
  --arms raw,vexinter --resume-log bench-results/layer2/comms_llm.log
$PY -m examples.bench_memory_llm --base-url $URL --model $M --n 10 \
  --arms naive,vexinter --resume-log bench-results/layer2/memory_llm.log
echo DONE-LAYER2 > bench-results/layer2/DONE
