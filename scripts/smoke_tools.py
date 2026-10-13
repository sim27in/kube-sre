# Manual smoke check against a live cluster (not part of pytest).
# Usage: uv run python scripts/smoke_tools.py [namespace]
import sys

from kube_sre.config import get_apps_api, get_core_api
from kube_sre.tools.k8s_read import get_events_impl, get_owner_impl, get_pod_logs_impl, list_pods_impl

ns = sys.argv[1] if len(sys.argv) > 1 else "default"
core, apps = get_core_api(), get_apps_api()

pods = list_pods_impl(core, ns, only_unhealthy=True)
print(pods.model_dump_json(indent=2))

for pod in pods.pods:
    print(f"\n--- {pod.name} ---")
    print(get_owner_impl(core, apps, pod.name, ns).workload)
    for e in get_events_impl(core, ns, pod.name, kind="Pod").events[:5]:
        print(f"  [{e.type}] {e.reason}: {e.message}")
    restarted = any(c.last_termination for c in pod.containers)
    if pod.containers:
        logs = get_pod_logs_impl(core, pod.name, ns, container=pod.containers[0].name, previous=restarted)
        print(f"  logs (previous={restarted}):\n{logs.text[-500:]}")
