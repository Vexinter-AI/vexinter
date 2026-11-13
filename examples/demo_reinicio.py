"""Persistence across service restarts (§15).

    python examples/demo_distribuido.py          # prints the task_id
    docker compose restart server
    python examples/demo_reinicio.py <task_id>
"""
import sys

from vexinter import Vexinter

ENDPOINT = "http://localhost:8000"
import os

ADMIN = os.environ.get("VEXINTER_ADMIN_TOKEN", "demo-admin-token")


def main(task_id: str):
    adm = Vexinter(endpoint=ENDPOINT, admin_token=ADMIN)

    print("agents rebuilt from the registry:",
        [a["agent_id"] for a in adm.remote.list_agents()])
    cases = adm.remote.pending_cases()
    print("human cases pending after restart:", len(cases))

    # the complete task survives in PostgreSQL and the chain stays intact
    v = adm.remote.admin_task(task_id)
    print(f"task {task_id}: state={v['state']}, "
        f"events={v['events']}, "
        f"chain_ok={v['audit']['ok']}")

    # and the service keeps operating: new task, new verdict
    print("metrics:", adm.remote.metrics()["final_states"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
        sys.exit("usage: demo_reinicio.py <task_id>"))
