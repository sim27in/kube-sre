"""Read-only Kubernetes tools, served over MCP.

Each tool is a thin MCP wrapper around a plain function that takes the API client as an argument,
so the logic can be unit-tested with fake clients. All results go through `finalize` (redaction +
evidence id) before leaving the server.
"""

import json
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from kubernetes.client import AppsV1Api, CoreV1Api
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import MaxRetryError

from kube_sre.config import get_apps_api, get_core_api, get_settings
from kube_sre.tools.boundary import fail, finalize
from kube_sre.tools.models import (
    ContainerSpec,
    ContainerStatus,
    EnvRef,
    EventInfo,
    EventList,
    ObjectRef,
    OwnerChain,
    PodList,
    PodLogs,
    PodSummary,
    ProbeSpec,
    TerminationInfo,
    WorkloadKind,
    WorkloadSpec,
)

mcp = FastMCP("kube-sre-k8s-read")

_MAX_ENV_VALUE = 200


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _api_error(e: ApiException) -> str:
    try:
        detail = json.loads(e.body).get("message", e.reason)
    except (TypeError, ValueError):
        detail = e.reason
    return f"Kubernetes API error ({e.status}): {detail}"


def _controller(meta) -> ObjectRef | None:
    refs = meta.owner_references or []
    ref = next((r for r in refs if r.controller), refs[0] if refs else None)
    return ObjectRef(kind=ref.kind, name=ref.name) if ref else None


# --- pods -------------------------------------------------------------------


def _container_status(cs) -> ContainerStatus:
    state, reason, message, exit_code = "unknown", None, None, None
    if cs.state and cs.state.running:
        state = "running"
    elif cs.state and cs.state.waiting:
        state, reason, message = "waiting", cs.state.waiting.reason, cs.state.waiting.message
    elif cs.state and cs.state.terminated:
        t = cs.state.terminated
        state, reason, message, exit_code = "terminated", t.reason, t.message, t.exit_code

    last = cs.last_state.terminated if cs.last_state else None
    return ContainerStatus(
        name=cs.name,
        image=cs.image,
        ready=bool(cs.ready),
        restart_count=cs.restart_count or 0,
        state=state,
        reason=reason,
        message=message,
        exit_code=exit_code,
        last_termination=TerminationInfo(
            reason=last.reason, exit_code=last.exit_code, message=last.message, finished_at=_ts(last.finished_at)
        )
        if last
        else None,
    )


def _pod_summary(pod) -> PodSummary:
    status = pod.status
    return PodSummary(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        phase=status.phase or "Unknown",
        node=pod.spec.node_name,
        owner=_controller(pod.metadata),
        failing_conditions=[
            f"{c.type}: {c.reason or ''} - {c.message or ''}".rstrip(" -")
            for c in status.conditions or []
            if c.status != "True"
        ],
        init_containers=[_container_status(cs) for cs in status.init_container_statuses or []],
        containers=[_container_status(cs) for cs in status.container_statuses or []],
    )


def _is_unhealthy(p: PodSummary) -> bool:
    if p.phase not in ("Running", "Succeeded"):
        return True
    return any(not c.ready or c.restart_count > 0 for c in p.containers)


def list_pods_impl(
    core: CoreV1Api, namespace: str, label_selector: str | None = None, only_unhealthy: bool = False
) -> PodList:
    pods = [_pod_summary(p) for p in core.list_namespaced_pod(namespace, label_selector=label_selector).items]
    if only_unhealthy:
        pods = [p for p in pods if _is_unhealthy(p)]
    cap = get_settings().max_pods
    return PodList(namespace=namespace, total=len(pods), truncated=len(pods) > cap, pods=pods[:cap])


def _tail_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the most recent output: drop from the front, at a line boundary."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[-max_bytes:].decode(errors="ignore")
    newline = cut.find("\n")
    return (cut[newline + 1 :] if newline != -1 else cut), True


def get_pod_logs_impl(
    core: CoreV1Api,
    pod_name: str,
    namespace: str,
    container: str | None = None,
    previous: bool = False,
    tail_lines: int = 100,
    since_seconds: int | None = None,
) -> PodLogs:
    pod = core.read_namespaced_pod(pod_name, namespace)
    names = [c.name for c in pod.spec.containers]
    all_names = names + [c.name for c in pod.spec.init_containers or []]
    if container is None:
        if len(names) != 1:
            fail(f"Pod {pod_name} has containers {names}; pass container=<name>.")
        container = names[0]
    elif container not in all_names:
        fail(f"Pod {pod_name} has no container {container!r}; containers are {all_names}.")

    if previous:
        statuses = (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])
        cs = next((s for s in statuses if s.name == container), None)
        if cs is None or not (cs.last_state and cs.last_state.terminated):
            fail(f"Container {container!r} has no previous instance (it has not restarted). Use previous=False.")

    settings = get_settings()
    text = core.read_namespaced_pod_log(
        pod_name,
        namespace,
        container=container,
        previous=previous,
        tail_lines=min(tail_lines, settings.max_log_lines),
        since_seconds=since_seconds,
    )
    text, truncated = _tail_bytes(text or "", settings.max_log_bytes)
    return PodLogs(
        pod=pod_name,
        namespace=namespace,
        container=container,
        previous=previous,
        lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        truncated=truncated,
        text=text,
    )


# --- events -----------------------------------------------------------------

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _event_time(e) -> datetime:
    return e.last_timestamp or e.event_time or e.metadata.creation_timestamp or _EPOCH


def get_events_impl(core: CoreV1Api, namespace: str, name: str, kind: str | None = None) -> EventList:
    selector = f"involvedObject.name={name}" + (f",involvedObject.kind={kind}" if kind else "")
    items = sorted(core.list_namespaced_event(namespace, field_selector=selector).items, key=_event_time, reverse=True)
    cap = get_settings().max_events
    events = [
        EventInfo(
            type=e.type,
            reason=e.reason,
            message=e.message,
            count=e.count or (e.series.count if e.series else 1),
            last_seen=_ts(_event_time(e)) if _event_time(e) != _EPOCH else None,
            component=(e.source.component if e.source else None) or e.reporting_component,
        )
        for e in items[:cap]
    ]
    return EventList(
        namespace=namespace,
        object=ObjectRef(kind=kind or "*", name=name),
        truncated=len(items) > cap,
        events=events,
    )


# --- ownership --------------------------------------------------------------


def get_owner_impl(core: CoreV1Api, apps: AppsV1Api, pod_name: str, namespace: str) -> OwnerChain:
    pod = core.read_namespaced_pod(pod_name, namespace)
    chain: list[ObjectRef] = []
    ref = _controller(pod.metadata)
    while ref is not None:
        chain.append(ref)
        if ref.kind != "ReplicaSet":
            break  # StatefulSet, DaemonSet, Job: treat as the workload
        try:
            rs = apps.read_namespaced_replica_set(ref.name, namespace)
        except ApiException as e:
            if e.status == 404:
                break  # orphaned ReplicaSet
            raise
        ref = _controller(rs.metadata)
    return OwnerChain(
        pod=pod_name,
        namespace=namespace,
        chain=chain,
        workload=chain[-1] if chain else ObjectRef(kind="Pod", name=pod_name),
    )


# --- workload specs ---------------------------------------------------------


def _probe(p) -> ProbeSpec | None:
    if p is None:
        return None
    if p.http_get:
        handler, target = "http", f"{p.http_get.path or '/'}:{p.http_get.port}"
    elif p.tcp_socket:
        handler, target = "tcp", str(p.tcp_socket.port)
    elif p._exec:
        handler, target = "exec", " ".join(p._exec.command or [])
    elif p.grpc:
        handler, target = "grpc", str(p.grpc.port)
    else:
        handler, target = "unknown", None
    return ProbeSpec(
        handler=handler,
        target=target,
        initial_delay_seconds=p.initial_delay_seconds,
        period_seconds=p.period_seconds,
        timeout_seconds=p.timeout_seconds,
        failure_threshold=p.failure_threshold,
    )


def _env(c) -> list[EnvRef]:
    out: list[EnvRef] = []
    for e in c.env or []:
        vf = e.value_from
        if vf is None:
            out.append(EnvRef(var=e.name, source="value", value=(e.value or "")[:_MAX_ENV_VALUE]))
        elif vf.secret_key_ref:
            r = vf.secret_key_ref
            out.append(EnvRef(var=e.name, source="secret", source_name=r.name, key=r.key, optional=r.optional))
        elif vf.config_map_key_ref:
            r = vf.config_map_key_ref
            out.append(EnvRef(var=e.name, source="configmap", source_name=r.name, key=r.key, optional=r.optional))
        elif vf.field_ref:
            out.append(EnvRef(var=e.name, source="field", key=vf.field_ref.field_path))
        elif vf.resource_field_ref:
            out.append(EnvRef(var=e.name, source="resource", key=vf.resource_field_ref.resource))
    for ef in c.env_from or []:
        if ef.secret_ref:
            out.append(EnvRef(var=ef.prefix or "*", source="secret_all", source_name=ef.secret_ref.name))
        elif ef.config_map_ref:
            out.append(EnvRef(var=ef.prefix or "*", source="configmap_all", source_name=ef.config_map_ref.name))
    return out


def _container_spec(c) -> ContainerSpec:
    res = c.resources
    return ContainerSpec(
        name=c.name,
        image=c.image,
        command=c.command,
        args=c.args,
        requests=dict(res.requests or {}) if res else {},
        limits=dict(res.limits or {}) if res else {},
        liveness=_probe(c.liveness_probe),
        readiness=_probe(c.readiness_probe),
        startup=_probe(c.startup_probe),
        env=_env(c),
    )


def describe_workload_impl(apps: AppsV1Api, kind: WorkloadKind, name: str, namespace: str) -> WorkloadSpec:
    if kind == "Deployment":
        obj = apps.read_namespaced_deployment(name, namespace)
        s = obj.status
        counts = (obj.spec.replicas, s.ready_replicas, s.available_replicas, s.updated_replicas)
    elif kind == "StatefulSet":
        obj = apps.read_namespaced_stateful_set(name, namespace)
        s = obj.status
        counts = (obj.spec.replicas, s.ready_replicas, s.available_replicas, s.updated_replicas)
    else:
        obj = apps.read_namespaced_daemon_set(name, namespace)
        s = obj.status
        counts = (s.desired_number_scheduled, s.number_ready, s.number_available, s.updated_number_scheduled)

    desired, ready, available, updated = counts
    return WorkloadSpec(
        kind=kind,
        name=name,
        namespace=namespace,
        revision=(obj.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
        desired=desired,
        ready=ready or 0,
        available=available or 0,
        updated=updated or 0,
        conditions=[
            f"{c.type}={c.status}: {c.reason or ''} - {c.message or ''}".rstrip(" -") for c in s.conditions or []
        ],
        containers=[_container_spec(c) for c in obj.spec.template.spec.containers],
    )


# --- MCP tools --------------------------------------------------------------


def _run(tool: str, args: dict[str, Any], fn, *api_factories):
    """Build the API clients lazily (so config errors are tool errors too), call the impl, finalize."""
    try:
        return finalize(tool, args, fn(*(f() for f in api_factories), **args))
    except ApiException as e:
        fail(_api_error(e))
    except (ConfigException, MaxRetryError) as e:
        fail(f"Cannot reach the Kubernetes API: {e}")


@mcp.tool()
def list_pods(namespace: str = "default", label_selector: str | None = None, only_unhealthy: bool = False) -> PodList:
    """List pods in a namespace with phase, container states, restart counts, exit codes and why the last
    instance died. Start an investigation here. `failing_conditions` explains Pending pods (e.g. Unschedulable).

    Args:
        namespace: Kubernetes namespace.
        label_selector: Optional selector, e.g. "app=payment-service".
        only_unhealthy: Return only pods that are not Running/Succeeded, not ready, or have restarted.
    """
    return _run("list_pods", locals(), list_pods_impl, get_core_api)


@mcp.tool()
def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str | None = None,
    previous: bool = False,
    tail_lines: int = 100,
    since_seconds: int | None = None,
) -> PodLogs:
    """Get recent logs from one container of a pod.

    For CrashLoopBackOff or OOMKilled containers the current instance has usually just restarted and has
    little output: set previous=True to read the logs of the instance that died. Output is capped; the
    most recent lines are kept.

    Args:
        pod_name: Pod name.
        namespace: Kubernetes namespace.
        container: Container name. Required only if the pod has more than one container.
        previous: Read the previous (terminated) instance of the container.
        tail_lines: Number of lines from the end (capped server-side).
        since_seconds: Only lines newer than this many seconds.
    """
    return _run("get_pod_logs", locals(), get_pod_logs_impl, get_core_api)


@mcp.tool()
def get_events(name: str, namespace: str = "default", kind: str | None = None) -> EventList:
    """Get Kubernetes events for an object, most recent first. Explains image pull failures, OOM kills,
    scheduling failures, failed probes and mount errors.

    Args:
        name: Object name (a pod, ReplicaSet, Deployment, PVC, ...).
        namespace: Kubernetes namespace.
        kind: Optional kind filter, e.g. "Pod" or "ReplicaSet".
    """
    return _run("get_events", locals(), get_events_impl, get_core_api)


@mcp.tool()
def get_owner(pod_name: str, namespace: str = "default") -> OwnerChain:
    """Resolve which workload owns a pod (Pod -> ReplicaSet -> Deployment). Reports and fixes target the
    workload, not the pod.

    Args:
        pod_name: Pod name.
        namespace: Kubernetes namespace.
    """
    return _run("get_owner", locals(), get_owner_impl, get_core_api, get_apps_api)


@mcp.tool()
def describe_workload(name: str, kind: WorkloadKind = "Deployment", namespace: str = "default") -> WorkloadSpec:
    """Get a workload's spec and rollout status: images, resource requests/limits, probes, env sources
    (which Secret/ConfigMap keys are referenced; secret values are never read), replica counts and conditions.

    Args:
        name: Workload name.
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Kubernetes namespace.
    """
    return _run("describe_workload", locals(), describe_workload_impl, get_apps_api)


if __name__ == "__main__":
    mcp.run()
