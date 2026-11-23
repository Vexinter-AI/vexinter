#!/bin/bash
# Stop vexbench as soon as inventario-gemma-main completes.
while [ ! -f .done_inventario_gemma_main ]; do sleep 20; done
sleep 8   # let run_config return; next config may start — service stop kills it cleanly
systemctl --user stop vexbench.service
echo "stopped $(date)" > .stopped_after_inventario_gemma_main
