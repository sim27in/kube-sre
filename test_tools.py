# test_tools.py
from mcp_server import get_cluster_pods, get_pod_events, get_pod_logs

print("--- TESTING: List Cluster Pods ---")
pods = get_cluster_pods(namespace="default")
print(pods)

# Find the broken payment-service pod
payment_pod = next((p["pod_name"] for p in pods if "payment-service" in p["pod_name"]), None)

if payment_pod:
    print(f"\n--- TESTING: Fetch Events for {payment_pod} ---")
    events = get_pod_events(pod_name=payment_pod)
    print(events)