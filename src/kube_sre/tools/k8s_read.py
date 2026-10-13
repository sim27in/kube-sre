import sys
from typing import Any

from fastmcp import FastMCP
from kubernetes import client, config
from kubernetes.client.rest import ApiException

mcp = FastMCP("Kube-Tools")

try:
    config.load_kube_config()
    v1 = client.CoreV1Api()
    print("Successfully connected to local K8s cluster", file=sys.stderr)
except Exception:
    print("Failed to connect to local kubernetes cluster", file=sys.stderr)
    sys.exit(1)
    
@mcp.tool()
def get_cluster_pods(namespace: str = "default")-> list[dict[str, Any]]:
    """
    Lists all pods in a specific Kubernetes namespace along with their status, restart counts, and container details.

    Args:
        namespace (str, optional): the K8s namespace to query. Defaults to "Default".

    Returns:
        List[Dict[str, Any]]: List of pods with their information
    """
    
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
        pod_list=[]
        
        for pod in pods.items:
            status = pod.status.phase        
            container_statuses=[]
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    state_info = "Running"
                    if cs.state.waiting:
                        state_info = f"Waiting: {cs.state.waiting.reason}"
                    elif cs.state.terminated:
                        state_info= f"Terminated: {cs.state.terminated.reason}"
                        
                    container_statuses.append({
                        "container_name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "state": state_info
                    })
            pod_list.append({
                "pod_name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": status,
                "containers": container_statuses
            })
        return pod_list
    except ApiException as e:
        return [{"error": f"Kubernetes API error: {e.reason}"}]
    

@mcp.tool()
def get_pod_logs(pod_name: str, namespace: str = "default", tail_lines: int = 50) -> str:
    """
    Retrieves recent logs for a specific pod.
    
    Args:
        pod_name: Name of the target pod.
        namespace: The K8s namespace (defaults to "default").
        tail_lines: Number of log lines to retrieve from the end.
    """
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines
        )
        return logs if logs else "No logs produced by container."
    except ApiException as e:
        return f"Error fetching logs for {pod_name}: {e.reason}"
    

@mcp.tool()
def get_pod_events(pod_name: str, namespace: str = "default") -> list[dict[str, str]]:
    """
    Retrieves system warning events associated with a specific pod.
    Useful for diagnosing ImagePullBackOff, OOMKilled, or scheduling issues.
    
    Args:
        pod_name: Name of the target pod.
        namespace: The K8s namespace (defaults to "default").
    """
    try:
        events = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}"
        )
        
        event_list = []
        for event in events.items:
            event_list.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "count": str(event.count)
            })
            
        return event_list
    except ApiException as e:
        return [{"error": f"Error fetching events for {pod_name}: {e.reason}"}]


if __name__ == "__main__":
    # Runs the MCP server using Standard I/O (stdio)
    mcp.run()
    
    
