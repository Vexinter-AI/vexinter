# Benchmark methodology

Harness: `examples/bench_domains.py` / `bench_llm.py` (campaigns A–B at
commit `df30aa4`; campaigns C–D on the current harness, which adds
per-run JSONL traces, `--base-url` and `--resume-log`).
Four arms (`solo`, `solo+read`, `vexinter`, `vexinter+read`), objective
state-based oracle, n = 5 per cell, temperature 1.0, no seed.
Tool calls: OpenAI `tools` API, `parallel_tool_calls=false`, no
structured-output grammar — chat template per backend.

## A. Qwen 3B abliterated (published §4.1, §4.4)

- Model: `qwen2.5-3b-instruct-abliterated`, GGUF `Q4_K_S` (1.83 GB)
- Backend: LM Studio `:1234`, llama.cpp engine 2.73.0 (CLI `43d4b26`)
- Context 4,096 / 1 slot — Date: 2026-11-12/13 — Runs: 320
  (inventory + helpdesk, 8 failure classes)

## B. 4B set — llama.cpp (in progress, tags `gemmalx`/`nemlx`)

- Models: `gemma-4-E2B-it` Q4_K_M (3.18 GiB),
  `NVIDIA-Nemotron-3-Nano-4B` Q4_K_M (~2.4 GiB)
- Backend: `llama-server` standalone, binary
  `llama.cpp-linux-x86_64-vulkan-avx2-2.73.0` (`0.9.0-dev`, commit
  `494d0f4`). First configs ran with Vulkan GPU offload (`-ngl 99`,
  Intel Iris Xe); from 2026-11-14 ~22:20 all runs are CPU-only
  (`-ngl 0 --device none`) — same weights/quant/template, backend does
  not affect output distribution
- Flags: `-c 4096 -np 1 --jinja --flash-attn on` — Date: from
  2026-11-14 16:33 -03 — Runner: `run_llama.sh` / `vexbench-llama.service`
- Configs per model+domain: `main` (160 runs) + `liar`/`cycler`/
  `cascade`/`poison` (60 each) = 400 → 1,600 total

## C. Nemotron-3-Nano-4B — billing simulator (published §4.7)

- Model: `NVIDIA-Nemotron-3-Nano-4B` GGUF `Q4_K_M` (~2.4 GiB)
- Backend: `llama-server` Vulkan, `-c 2048 -np 1 --jinja
  --flash-attn on`, alias `nemotron-3-nano`, OpenAI-compatible
  `http://127.0.0.1:8080/v1`
- Harness: `examples.bench_llm`, temperature 1.0,
  `parallel_tool_calls=false`, faults `control,false_success,
  lost_response,cut_before,blind_false_success`, n=10 per cell
- Date: 2026-11-23 — Runs: 200 — Traces:
  `traces/billing/billing-nemotron-main/`, log
  `billing-nemotron-main.log`

## D. Qwen3.5-4B abliterated — real Invoice Ninja (published §4.8)

- Model: `Huihui-Qwen3.5-4B-abliterated` GGUF `Q4_K_S` (2.56 GB)
- Backend: `llama-server` Vulkan, same flags as §4.6, alias
  `qwen3.5-4b-abliterated`
- Target: self-hosted Invoice Ninja v5 (`invoiceninja/invoiceninja-debian`
  + MySQL 8 + Redis) at `http://localhost`; oracle reads final state
  through the provider API, bypassing the adapter
- Harness: `examples.bench_real`, temperature 1.0, all seven fault
  classes, n=10 per cell
- Date: 2026-11-23/24 — Runs: 280 — Traces:
  `traces/invoiceninja/invoiceninja-q35-main/`, log
  `invoiceninja-q35-main.log`
- Runner: `run_invoiceninja.sh` / `vexbench-invoiceninja.service`
  (systemd user unit, `Restart=on-failure`, `--resume-log`)


