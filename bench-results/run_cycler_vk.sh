#!/bin/bash
# nemlx cycler re-run under Vulkan — systemd unit vexbench-cycler.service.
# Resumable: --resume-log skips completed runs, retries ERROR slots.
cd /home/facu/Proyectos/Vexinter || exit 1
export VEXINTER_DEV_MODE=1
B="http://127.0.0.1:8080/v1"
GGUF=$HOME/.lmstudio/models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf
LOG=bench-results/inventory-nemlx-cycler-vk.log
for attempt in 1 2 3 4 5 6 7 8; do
  bench-results/llama_ensure.sh nemotron-3-nano "$GGUF" || { sleep 30; continue; }
  .venv/bin/python -m examples.bench_domains --domain inventory --n 5 \
    --model nemotron-3-nano --base-url $B \
    --config inventory-nemlx-cycler-vk \
    --faults control,false_success,lost_response --adversary cycler \
    --resume-log $LOG >> $LOG 2>&1
  OK=$(grep "^\[" $LOG | grep -vc " ERROR")
  echo "attempt=$attempt ok_runs=$OK $(date)"
  [ "$OK" -ge 60 ] && { echo "cycler-vk COMPLETE" > bench-results/CYCLER-VK-DONE2; exit 0; }
  sleep 15
done
echo "cycler-vk INCOMPLETE after 8 attempts" > bench-results/CYCLER-VK-DONE2
exit 1
