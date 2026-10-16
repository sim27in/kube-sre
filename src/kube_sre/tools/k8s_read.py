"""Read-only Kubernetes tools, served over MCP.

Each tool is a thin MCP wrapper around a plain function that takes the API client as an argument,
so the logic can be unit-tested with fake clients. All results go through `finalize` (redaction +
evidence id) before leaving the server.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastmcp import FastMCP
from kubernetes.client import AppsV1Api, CoreV1Api
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import MaxRetryError

from kube_sre.config import get_apps_api, get_core_api, get_settings
from kube_sre.guardrails.redaction import SECRET_MARKER, is_secret_name, redact_argv
from kube_sre.tools.boundary import fail, finalize
from kube_sre.tools.models import (
    Backend,
    ConfigInfo,
    ContainerSpec,
    ContainerStatus,
    EnvRef,
    EventInfo,
    EventList,
    NodeInfo,
    NodeList,
    ObjectRef,
    OwnerChain,
    PodList,
    PodLogs,
    PodSummary,
    ProbeSpec,
    PVCInfo,
    PVCList,
    ServiceInfo,
    TerminationInfo,
    VolumeRef,
    WarningEventList,
    WorkloadKind,
    WorkloadSpec,
)

mcp = FastMCP("kube-sre-k8s-read")

_MAX_ENV_VALUE = 200
_MAX_CONFIG_VALUE = 500
_MAX_CONFIG_KEYS = 50
_LOGS_GONE = re.compile(r"^unable to retrieve container logs for \S+\s*$")


def _now() -> datetime:
    """Current time. The eval replay server pins it to when the snapshot was recorded."""
    return datetime.now(UTC)


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


def _recently_restarted(c: ContainerStatus) -> bool:
    if c.restart_count == 0:
        return False
    finished = c.last_termination.finished_at if c.last_termination else None
    if finished is None:
        return True
    age = _now() - datetime.fromisoformat(finished)
    return age.total_seconds() < get_settings().recent_restart_seconds


def _is_unhealthy(p: PodSummary) -> bool:
    if p.phase == "Succeeded":
        return False  # finished (e.g. a Job); its containers are never ready again
    if p.phase != "Running":
        return True
    return any(not c.ready or _recently_restarted(c) for c in p.containers)


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
    # Drop the partial first line, unless it's all there is: one line longer than the cap keeps its tail.
    return (cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut), True


def get_pod_logs_impl(
    core: CoreV1Api,
    pod_name: str,
    namespace: str,
    container: str | None = None,
    previous: bool = False,
    tail_lines: int = 100,
    since_seconds: int | None = None,
) -> PodLogs:
    if tail_lines < 1:
        fail("tail_lines must be at least 1.")
    if since_seconds is not None and since_seconds < 1:
        fail("since_seconds must be at least 1.")
    pod = core.read_namespaced_pod(pod_name, namespace)
    names = [c.name for c in pod.spec.containers]
    all_names = names + [c.name for c in pod.spec.init_containers or []]
    if container is None:
        if len(names) != 1:
            fail(f"Pod {pod_name} has containers {names}; pass container=<name>.")
        container = names[0]
    elif container not in all_names:
        fail(f"Pod {pod_name} has no container {container!r}; containers are {all_names}.")

    statuses = (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])
    cs = next((s for s in statuses if s.name == container), None)
    has_died = bool(cs and cs.last_state and cs.last_state.terminated)
    # Dead (just terminated, or waiting in back-off) rather than running a fresh instance.
    not_running = bool(has_died and not (cs.state and cs.state.running))
    if previous and not has_died:
        fail(f"Container {container!r} has no previous instance (it has not restarted). Use previous=False.")

    settings = get_settings()

    def read(prev: bool) -> str:
        return core.read_namespaced_pod_log(
            pod_name,
            namespace,
            container=container,
            previous=prev,
            tail_lines=min(tail_lines, settings.max_log_lines),
            since_seconds=since_seconds,
        )

    note = None
    text = read(previous) or ""
    # While a crashed container is not running (just terminated, or waiting in back-off) the kubelet
    # serves the crashed instance as the "current" logs, and `previous` can point at an older instance
    # it has already garbage-collected. It reports that as log text, not an HTTP error.
    crashed_note = "Container is not running; these are the logs of its most recent crashed instance."
    if previous and not_running and _LOGS_GONE.match(text):
        previous, text, note = False, read(False) or "", crashed_note
    elif not previous and not_running:
        note = crashed_note
    if _LOGS_GONE.match(text):
        text, note = "", "Logs for this container instance are no longer available on the node."

    text, truncated = _tail_bytes(text, settings.max_log_bytes)
    return PodLogs(
        pod=pod_name,
        namespace=namespace,
        container=container,
        previous=previous,
        lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        truncated=truncated,
        note=note,
        text=text,
    )


# --- events -----------------------------------------------------------------

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _event_time(e) -> datetime:
    series_last = e.series.last_observed_time if e.series else None
    return e.last_timestamp or series_last or e.event_time or e.metadata.creation_timestamp or _EPOCH


def _event_info(e, with_object: bool = False) -> EventInfo:
    t, obj = _event_time(e), e.involved_object
    return EventInfo(
        type=e.type,
        reason=e.reason,
        message=e.message,
        count=e.count or (e.series.count if e.series else 1),
        last_seen=_ts(t) if t != _EPOCH else None,
        component=(e.source.component if e.source else None) or e.reporting_component,
        involved=ObjectRef(kind=obj.kind or "Unknown", name=obj.name) if with_object and obj else None,
    )


def get_events_impl(core: CoreV1Api, namespace: str, name: str, kind: str | None = None) -> EventList:
    selector = f"involvedObject.name={name}" + (f",involvedObject.kind={kind}" if kind else "")
    items = sorted(core.list_namespaced_event(namespace, field_selector=selector).items, key=_event_time, reverse=True)
    cap = get_settings().max_events
    return EventList(
        namespace=namespace,
        object=ObjectRef(kind=kind or "*", name=name),
        truncated=len(items) > cap,
        events=[_event_info(e) for e in items[:cap]],
    )


def list_warning_events_impl(core: CoreV1Api, namespace: str, since_minutes: int = 60) -> WarningEventList:
    if since_minutes < 1:
        fail("since_minutes must be at least 1.")
    cutoff = _now() - timedelta(minutes=since_minutes)
    items = [
        e
        for e in core.list_namespaced_event(namespace, field_selector="type=Warning").items
        if _event_time(e) == _EPOCH or _event_time(e) >= cutoff  # keep events with no timestamp
    ]
    items.sort(key=_event_time, reverse=True)
    cap = get_settings().max_events
    return WarningEventList(
        namespace=namespace,
        since_minutes=since_minutes,
        truncated=len(items) > cap,
        events=[_event_info(e, with_object=True) for e in items[:cap]],
    )


# --- storage ----------------------------------------------------------------


def list_pvcs_impl(core: CoreV1Api, namespace: str) -> PVCList:
    pvcs = [
        PVCInfo(
            name=p.metadata.name,
            phase=p.status.phase or "Unknown",
            storage_class=p.spec.storage_class_name,
            requested=((p.spec.resources.requests or {}).get("storage") if p.spec.resources else None),
            capacity=(p.status.capacity or {}).get("storage"),
            access_modes=p.spec.access_modes or [],
            volume=p.spec.volume_name,
        )
        for p in core.list_namespaced_persistent_volume_claim(namespace).items
    ]
    cap = get_settings().max_pods  # same bound as pod listings
    return PVCList(namespace=namespace, truncated=len(pvcs) > cap, pvcs=pvcs[:cap])


# --- nodes ------------------------------------------------------------------


def _taint(t) -> str:
    return f"{t.key}={t.value}:{t.effect}" if t.value else f"{t.key}:{t.effect}"


def list_nodes_impl(core: CoreV1Api) -> NodeList:
    nodes = []
    for n in core.list_node().items:
        conditions = n.status.conditions or []
        nodes.append(
            NodeInfo(
                name=n.metadata.name,
                ready=any(c.type == "Ready" and c.status == "True" for c in conditions),
                unschedulable=bool(n.spec.unschedulable),
                taints=[_taint(t) for t in n.spec.taints or []],
                pressure=[c.type for c in conditions if c.type != "Ready" and c.status == "True"],
                allocatable={k: v for k, v in (n.status.allocatable or {}).items() if k in ("cpu", "memory", "pods")},
            )
        )
    cap = get_settings().max_pods
    return NodeList(total=len(nodes), truncated=len(nodes) > cap, nodes=nodes[:cap])


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
            value = SECRET_MARKER if e.value and is_secret_name(e.name) else (e.value or "")[:_MAX_ENV_VALUE]
            out.append(EnvRef(var=e.name, source="value", value=value))
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
        command=redact_argv(c.command),
        args=redact_argv(c.args),
        requests=dict(res.requests or {}) if res else {},
        limits=dict(res.limits or {}) if res else {},
        liveness=_probe(c.liveness_probe),
        readiness=_probe(c.readiness_probe),
        startup=_probe(c.startup_probe),
        env=_env(c),
    )


def _toleration(t) -> str:
    key = t.key or "*"
    match = f"{key}={t.value or ''}" if t.operator != "Exists" else key
    return f"{match}:{t.effect or '*'}"


def _volume(v) -> VolumeRef:
    if v.config_map:
        src = v.config_map
        return VolumeRef(
            name=v.name,
            source="configmap",
            source_name=src.name,
            keys=[i.key for i in src.items or []],
            optional=src.optional,
        )
    if v.secret:
        src = v.secret
        return VolumeRef(
            name=v.name,
            source="secret",
            source_name=src.secret_name,
            keys=[i.key for i in src.items or []],
            optional=src.optional,
        )
    if v.persistent_volume_claim:
        return VolumeRef(name=v.name, source="pvc", source_name=v.persistent_volume_claim.claim_name)
    if v.host_path:
        return VolumeRef(name=v.name, source="host_path", source_name=v.host_path.path)
    if v.empty_dir:
        return VolumeRef(name=v.name, source="empty_dir")
    return VolumeRef(name=v.name, source="other")


def _replica_counts(kind: WorkloadKind, obj) -> tuple[int | None, int | None, int | None, int | None]:
    """(desired, ready, available, updated) for a Deployment, StatefulSet or DaemonSet."""
    s = obj.status
    if kind == "DaemonSet":
        return s.desired_number_scheduled, s.number_ready, s.number_available, s.updated_number_scheduled
    return obj.spec.replicas, s.ready_replicas, s.available_replicas, s.updated_replicas


def describe_workload_impl(apps: AppsV1Api, kind: WorkloadKind, name: str, namespace: str) -> WorkloadSpec:
    if kind == "Deployment":
        obj = apps.read_namespaced_deployment(name, namespace)
    elif kind == "StatefulSet":
        obj = apps.read_namespaced_stateful_set(name, namespace)
    else:
        obj = apps.read_namespaced_daemon_set(name, namespace)
    s = obj.status
    desired, ready, available, updated = _replica_counts(kind, obj)
    pod = obj.spec.template.spec
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
        service_account=pod.service_account_name,
        image_pull_secrets=[r.name for r in pod.image_pull_secrets or []],
        node_selector=dict(pod.node_selector or {}),
        tolerations=[_toleration(t) for t in pod.tolerations or []],
        volumes=[_volume(v) for v in pod.volumes or []],
        init_containers=[_container_spec(c) for c in pod.init_containers or []],
        containers=[_container_spec(c) for c in pod.containers],
    )


# --- services and config ----------------------------------------------------


def _read_or_none(read, *args):
    """The object, or None if it doesn't exist. A missing object is a finding, not a tool error."""
    try:
        return read(*args)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def describe_service_impl(core: CoreV1Api, apps: AppsV1Api, name: str, namespace: str) -> ServiceInfo:
    svc = _read_or_none(core.read_namespaced_service, name, namespace)
    if svc is None:
        return ServiceInfo(name=name, namespace=namespace, exists=False)
    selector = dict(svc.spec.selector or {})
    info = ServiceInfo(
        name=name,
        namespace=namespace,
        exists=True,
        type=svc.spec.type,
        selector=selector,
        ports=[f"{p.port}->{p.target_port or p.port}/{p.protocol or 'TCP'}" for p in svc.spec.ports or []],
    )
    if not selector:
        return info

    pods = core.list_namespaced_pod(namespace, label_selector=",".join(f"{k}={v}" for k, v in selector.items()))
    for pod in pods.items:
        if any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or []):
            info.ready_pods += 1
        else:
            info.not_ready_pods += 1

    listers = (
        ("Deployment", apps.list_namespaced_deployment),
        ("StatefulSet", apps.list_namespaced_stateful_set),
        ("DaemonSet", apps.list_namespaced_daemon_set),
    )
    for kind, list_fn in listers:
        for obj in list_fn(namespace).items:
            labels = obj.spec.template.metadata.labels or {}
            if selector.items() <= labels.items():
                desired, ready, _, _ = _replica_counts(kind, obj)
                info.backends.append(Backend(kind=kind, name=obj.metadata.name, desired=desired, ready=ready or 0))
    return info


def describe_config_impl(
    core: CoreV1Api, kind: Literal["ConfigMap", "Secret"], name: str, namespace: str
) -> ConfigInfo:
    if kind == "Secret":
        # The values reach this process but are never put in the result.
        secret = _read_or_none(core.read_namespaced_secret, name, namespace)
        if secret is None:
            return ConfigInfo(kind=kind, name=name, namespace=namespace, exists=False)
        keys = sorted({*(secret.data or {}), *(secret.string_data or {})})
        return ConfigInfo(kind=kind, name=name, namespace=namespace, exists=True, type=secret.type, keys=keys)

    cm = _read_or_none(core.read_namespaced_config_map, name, namespace)
    if cm is None:
        return ConfigInfo(kind=kind, name=name, namespace=namespace, exists=False)
    data = cm.data or {}
    values = {
        k: SECRET_MARKER if v and is_secret_name(k) else v[:_MAX_CONFIG_VALUE]
        for k, v in sorted(data.items())[:_MAX_CONFIG_KEYS]
    }
    keys = sorted({*data, *(cm.binary_data or {})})
    return ConfigInfo(kind=kind, name=name, namespace=namespace, exists=True, keys=keys, values=values)


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
    If a workload has fewer pods than expected, or no pods at all, use list_warning_events.

    Args:
        namespace: Kubernetes namespace.
        label_selector: Optional selector, e.g. "app=payment-service".
        only_unhealthy: Return only pods that are not Running/Succeeded, not ready, or restarted recently.
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

    For a container that crashed and is running again, set previous=True to read the instance that died.
    For a container that is not running (terminated or in CrashLoopBackOff), the default call already
    returns the crashed instance's logs (previous=True falls back to it automatically). `note` says which
    instance the logs came from.
    Output is capped; the most recent lines are kept.

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
def list_warning_events(namespace: str = "default", since_minutes: int = 60) -> WarningEventList:
    """List Warning events for every object in a namespace, most recent first, each with the object it is
    about. Catches failures that never produce a pod: ReplicaSet FailedCreate (quota, admission webhooks,
    missing ServiceAccount), PVC ProvisioningFailed, and failed scheduling.

    Args:
        namespace: Kubernetes namespace.
        since_minutes: Only events seen within this many minutes.
    """
    return _run("list_warning_events", locals(), list_warning_events_impl, get_core_api)


@mcp.tool()
def list_pvcs(namespace: str = "default") -> PVCList:
    """List PersistentVolumeClaims with phase, storage class, requested size and bound volume. A Pending PVC
    keeps pods that mount it Pending; call get_events(name=<pvc>, kind="PersistentVolumeClaim") for why.

    Args:
        namespace: Kubernetes namespace.
    """
    return _run("list_pvcs", locals(), list_pvcs_impl, get_core_api)


@mcp.tool()
def list_nodes() -> NodeList:
    """List the cluster's nodes with readiness, taints, cordoning, pressure conditions (memory, disk, PID) and
    allocatable cpu/memory. Use it when a pod is Pending with "untolerated taint" or insufficient resources,
    or pods are evicted; scheduler messages don't say which taint.
    """
    return _run("list_nodes", {}, list_nodes_impl, get_core_api)


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
    (which Secret/ConfigMap keys are referenced; secret values are never read), init containers, volumes,
    service account, image pull secrets, node selector and tolerations, replica counts and conditions.

    Args:
        name: Workload name.
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Kubernetes namespace.
    """
    return _run("describe_workload", locals(), describe_workload_impl, get_apps_api)


@mcp.tool()
def describe_service(name: str, namespace: str = "default") -> ServiceInfo:
    """Check a Service an app depends on: whether it exists, its selector and ports, how many matching pods
    are Ready (its endpoints), and which workloads back it with their replica counts. Use it when logs show
    a dependency is unreachable (connection refused, DNS lookup failed, timeouts) to find out why.

    Args:
        name: Service name, as the app addresses it (e.g. "payments" for http://payments:8080).
        namespace: Kubernetes namespace.
    """
    return _run("describe_service", locals(), describe_service_impl, get_core_api, get_apps_api)


@mcp.tool()
def describe_config(
    name: str, kind: Literal["ConfigMap", "Secret"] = "ConfigMap", namespace: str = "default"
) -> ConfigInfo:
    """Check a ConfigMap or Secret a workload references: whether it exists and which keys it has.
    ConfigMap values are included (truncated); Secret values are never returned, only key names and type.
    Use it to confirm a missing object or key instead of guessing.

    Args:
        name: ConfigMap or Secret name (including image pull secrets).
        kind: ConfigMap or Secret.
        namespace: Kubernetes namespace.
    """
    return _run("describe_config", locals(), describe_config_impl, get_core_api)


if __name__ == "__main__":
    mcp.run(show_banner=False)
