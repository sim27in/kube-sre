import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from kubernetes.client import (
    CoreV1Event,
    CoreV1EventList,
    V1Container,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateTerminated,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1EnvVar,
    V1EnvVarSource,
    V1EventSource,
    V1HTTPGetAction,
    V1LabelSelector,
    V1ObjectMeta,
    V1ObjectReference,
    V1OwnerReference,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimList,
    V1PersistentVolumeClaimSpec,
    V1PersistentVolumeClaimStatus,
    V1Pod,
    V1PodCondition,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
    V1Probe,
    V1ReplicaSet,
    V1ReplicaSetSpec,
    V1ResourceRequirements,
    V1SecretKeySelector,
    V1VolumeResourceRequirements,
)
from kubernetes.client.rest import ApiException

from kube_sre.config import get_settings
from kube_sre.tools import k8s_read
from kube_sre.tools.k8s_read import (
    describe_workload_impl,
    get_events_impl,
    get_owner_impl,
    get_pod_logs_impl,
    list_pods_impl,
    list_pvcs_impl,
    list_warning_events_impl,
)

NS = "demo"


def _owner(kind, name):
    return [V1OwnerReference(api_version="apps/v1", kind=kind, name=name, uid="u", controller=True)]


def _pod(name, containers, statuses, phase="Running", owner=None, conditions=None):
    return V1Pod(
        metadata=V1ObjectMeta(name=name, namespace=NS, owner_references=owner),
        spec=V1PodSpec(containers=[V1Container(name=c, image="img") for c in containers], node_name="node-1"),
        status=V1PodStatus(phase=phase, container_statuses=statuses, conditions=conditions),
    )


def _running(name, restarts=0, last=None):
    return V1ContainerStatus(
        name=name,
        image="img",
        image_id="",
        ready=restarts == 0,
        restart_count=restarts,
        state=V1ContainerState(running=V1ContainerStateRunning()),
        last_state=V1ContainerState(terminated=last) if last else V1ContainerState(),
    )


OOM_POD = _pod(
    "analytics-7d9f-abc12",
    ["memory-hog"],
    [
        V1ContainerStatus(
            name="memory-hog",
            image="python:3.11-slim",
            image_id="",
            ready=False,
            restart_count=4,
            state=V1ContainerState(waiting=V1ContainerStateWaiting(reason="CrashLoopBackOff")),
            last_state=V1ContainerState(terminated=V1ContainerStateTerminated(reason="OOMKilled", exit_code=137)),
        )
    ],
    owner=_owner("ReplicaSet", "analytics-7d9f"),
)
HEALTHY_POD = _pod("web-1", ["web"], [_running("web")])
SIDECAR_POD = _pod("api-1", ["api", "proxy"], [_running("api"), _running("proxy")])
PENDING_POD = _pod(
    "big-1",
    ["big"],
    None,
    phase="Pending",
    conditions=[
        V1PodCondition(
            type="PodScheduled", status="False", reason="Unschedulable", message="0/1 nodes: Insufficient cpu"
        )
    ],
)


class FakeCore:
    def __init__(self, pods, logs=None, events=(), pvcs=()):
        self.pods = {p.metadata.name: p for p in pods}
        self.logs = logs or {}
        self.events = list(events)
        self.pvcs = list(pvcs)
        self.log_calls = []
        self.selectors = []

    def list_namespaced_pod(self, namespace, label_selector=None):
        return V1PodList(items=list(self.pods.values()))

    def read_namespaced_pod(self, name, namespace):
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return self.pods[name]

    def read_namespaced_pod_log(self, name, namespace, **kw):
        self.log_calls.append(kw)
        return self.logs.get((name, kw["container"], kw["previous"]), "")

    def list_namespaced_event(self, namespace, field_selector):
        self.selectors.append(field_selector)
        return CoreV1EventList(items=self.events)

    def list_namespaced_persistent_volume_claim(self, namespace):
        return V1PersistentVolumeClaimList(items=self.pvcs)


class FakeApps:
    def __init__(self, replica_sets=(), deployments=()):
        self.rs = {r.metadata.name: r for r in replica_sets}
        self.deployments = {d.metadata.name: d for d in deployments}

    def read_namespaced_replica_set(self, name, namespace):
        if name not in self.rs:
            raise ApiException(status=404, reason="Not Found")
        return self.rs[name]

    def read_namespaced_deployment(self, name, namespace):
        return self.deployments[name]


# --- list_pods --------------------------------------------------------------


def test_list_pods_reports_last_termination_and_owner():
    result = list_pods_impl(FakeCore([OOM_POD]), NS)
    c = result.pods[0].containers[0]
    assert c.reason == "CrashLoopBackOff"
    assert c.last_termination.reason == "OOMKilled"
    assert c.last_termination.exit_code == 137
    assert result.pods[0].owner.kind == "ReplicaSet"


def test_list_pods_only_unhealthy():
    result = list_pods_impl(FakeCore([OOM_POD, HEALTHY_POD, PENDING_POD]), NS, only_unhealthy=True)
    assert {p.name for p in result.pods} == {OOM_POD.metadata.name, "big-1"}


def _restarted_ready_pod(minutes_ago):
    last = V1ContainerStateTerminated(
        reason="Error", exit_code=1, finished_at=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    status = _running("web", restarts=1, last=last).model_copy(update={"ready": True})
    return _pod(f"web-{minutes_ago}", ["web"], [status])


def test_old_restart_is_not_unhealthy():
    pods = [_restarted_ready_pod(minutes_ago=3 * 24 * 60), _restarted_ready_pod(minutes_ago=5)]
    result = list_pods_impl(FakeCore(pods), NS, only_unhealthy=True)
    assert [p.name for p in result.pods] == ["web-5"]


def test_pending_pod_explains_scheduling_failure():
    pod = list_pods_impl(FakeCore([PENDING_POD]), NS).pods[0]
    assert pod.failing_conditions == ["PodScheduled: Unschedulable - 0/1 nodes: Insufficient cpu"]


# --- get_pod_logs -----------------------------------------------------------


def test_previous_logs_read_from_dead_instance():
    core = FakeCore([OOM_POD], logs={("analytics-7d9f-abc12", "memory-hog", True): "MemoryError\n"})
    logs = get_pod_logs_impl(core, "analytics-7d9f-abc12", NS, previous=True)
    assert logs.text == "MemoryError\n"
    assert logs.container == "memory-hog"  # single container picked automatically
    assert core.log_calls[0]["previous"] is True


GONE = "unable to retrieve container logs for containerd://6a1ee389f188"


def test_previous_falls_back_to_crashed_instance_during_backoff():
    # Observed on kind: in CrashLoopBackOff, previous=True hits an already-removed container and the
    # kubelet returns an error string as the log body; the crashed instance is served as "current".
    pod = OOM_POD.metadata.name
    core = FakeCore([OOM_POD], logs={(pod, "memory-hog", True): GONE, (pod, "memory-hog", False): "allocating\n"})
    logs = get_pod_logs_impl(core, pod, NS, previous=True)
    assert logs.text == "allocating\n"
    assert logs.previous is False
    assert "most recent crashed instance" in logs.note


def test_current_logs_during_backoff_are_labelled():
    pod = OOM_POD.metadata.name
    core = FakeCore([OOM_POD], logs={(pod, "memory-hog", False): "allocating\n"})
    assert "most recent crashed instance" in get_pod_logs_impl(core, pod, NS).note


def test_kubelet_error_string_never_returned_as_log_text():
    pod = OOM_POD.metadata.name
    core = FakeCore([OOM_POD], logs={(pod, "memory-hog", True): GONE, (pod, "memory-hog", False): GONE})
    logs = get_pod_logs_impl(core, pod, NS, previous=True)
    assert logs.text == ""
    assert "no longer available" in logs.note


def test_previous_rejected_when_container_never_restarted():
    with pytest.raises(ToolError, match="no previous instance"):
        get_pod_logs_impl(FakeCore([HEALTHY_POD]), "web-1", NS, previous=True)


def test_multi_container_pod_requires_container():
    with pytest.raises(ToolError, match=r"\['api', 'proxy'\]"):
        get_pod_logs_impl(FakeCore([SIDECAR_POD]), "api-1", NS)
    logs = get_pod_logs_impl(FakeCore([SIDECAR_POD]), "api-1", NS, container="proxy")
    assert logs.container == "proxy"


@pytest.mark.parametrize("args", [{"tail_lines": 0}, {"tail_lines": -5}, {"since_seconds": 0}])
def test_invalid_log_window_rejected(args):
    with pytest.raises(ToolError, match="at least 1"):
        get_pod_logs_impl(FakeCore([HEALTHY_POD]), "web-1", NS, **args)


def test_unknown_container_rejected():
    with pytest.raises(ToolError, match="no container 'nope'"):
        get_pod_logs_impl(FakeCore([SIDECAR_POD]), "api-1", NS, container="nope")


def test_logs_capped_keeping_most_recent_lines(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_log_bytes", 40)
    text = "".join(f"line {i:03d}\n" for i in range(100))
    core = FakeCore([HEALTHY_POD], logs={("web-1", "web", False): text})
    logs = get_pod_logs_impl(core, "web-1", NS, tail_lines=10_000)
    assert logs.truncated
    assert logs.text.endswith("line 099\n")
    assert logs.text.startswith("line ")  # cut at a line boundary
    assert len(logs.text.encode()) <= 40
    assert core.log_calls[0]["tail_lines"] == get_settings().max_log_lines


# --- get_events -------------------------------------------------------------


def _event(reason, minutes_ago, count=1, kind=None, name="x"):
    return CoreV1Event(
        metadata=V1ObjectMeta(name=reason),
        involved_object=V1ObjectReference(kind=kind, name=name),
        type="Warning",
        reason=reason,
        message=reason.lower(),
        count=count,
        last_timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source=V1EventSource(component="kubelet"),
    )


def test_events_most_recent_first_and_filtered_by_kind():
    core = FakeCore([], events=[_event("Pulling", 10), _event("BackOff", 1, count=7), _event("Failed", 5)])
    result = get_events_impl(core, NS, "x", kind="Pod")
    assert [e.reason for e in result.events] == ["BackOff", "Failed", "Pulling"]
    assert result.events[0].count == 7
    assert core.selectors == ["involvedObject.name=x,involvedObject.kind=Pod"]


def test_warning_events_namespace_wide_with_object_and_window():
    core = FakeCore(
        [],
        events=[
            _event("FailedCreate", 2, kind="ReplicaSet", name="api-6b9"),
            _event("ProvisioningFailed", 10, kind="PersistentVolumeClaim", name="data"),
            _event("BackOff", 3 * 60, kind="Pod", name="old-1"),
        ],
    )
    result = list_warning_events_impl(core, NS, since_minutes=60)
    assert [(e.reason, e.involved.kind, e.involved.name) for e in result.events] == [
        ("FailedCreate", "ReplicaSet", "api-6b9"),
        ("ProvisioningFailed", "PersistentVolumeClaim", "data"),
    ]
    assert core.selectors == ["type=Warning"]


# --- list_pvcs --------------------------------------------------------------


def test_list_pvcs_reports_pending_claim():
    pvc = V1PersistentVolumeClaim(
        metadata=V1ObjectMeta(name="data"),
        spec=V1PersistentVolumeClaimSpec(
            storage_class_name="fast-ssd",
            access_modes=["ReadWriteOnce"],
            resources=V1VolumeResourceRequirements(requests={"storage": "10Gi"}),
        ),
        status=V1PersistentVolumeClaimStatus(phase="Pending"),
    )
    claim = list_pvcs_impl(FakeCore([], pvcs=[pvc]), NS).pvcs[0]
    assert (claim.name, claim.phase, claim.storage_class, claim.requested, claim.volume) == (
        "data",
        "Pending",
        "fast-ssd",
        "10Gi",
        None,
    )


# --- get_owner --------------------------------------------------------------


def test_owner_resolves_through_replicaset():
    rs = V1ReplicaSet(
        metadata=V1ObjectMeta(name="analytics-7d9f", owner_references=_owner("Deployment", "analytics")),
        spec=V1ReplicaSetSpec(selector=V1LabelSelector()),
    )
    result = get_owner_impl(FakeCore([OOM_POD]), FakeApps([rs]), OOM_POD.metadata.name, NS)
    assert [r.kind for r in result.chain] == ["ReplicaSet", "Deployment"]
    assert result.workload.name == "analytics"


def test_bare_pod_is_its_own_workload():
    result = get_owner_impl(FakeCore([HEALTHY_POD]), FakeApps(), "web-1", NS)
    assert result.chain == [] and result.workload.kind == "Pod"


# --- describe_workload ------------------------------------------------------


def test_describe_workload_shows_limits_probes_and_secret_refs_not_values():
    container = V1Container(
        name="api",
        image="api:1.2",
        resources=V1ResourceRequirements(limits={"memory": "32Mi"}),
        liveness_probe=V1Probe(http_get=V1HTTPGetAction(path="/healthz", port=8080), failure_threshold=3),
        env=[
            V1EnvVar(name="LOG_LEVEL", value="debug"),
            V1EnvVar(
                name="DB_PASS",
                value_from=V1EnvVarSource(secret_key_ref=V1SecretKeySelector(name="db", key="password")),
            ),
        ],
    )
    dep = V1Deployment(
        metadata=V1ObjectMeta(name="api", annotations={"deployment.kubernetes.io/revision": "3"}),
        spec=V1DeploymentSpec(
            replicas=2,
            selector=V1LabelSelector(),
            template=V1PodTemplateSpec(spec=V1PodSpec(containers=[container])),
        ),
        status=V1DeploymentStatus(ready_replicas=1, available_replicas=1, updated_replicas=2),
    )
    spec = describe_workload_impl(FakeApps(deployments=[dep]), "Deployment", "api", NS)
    c = spec.containers[0]
    assert (spec.revision, spec.desired, spec.ready) == ("3", 2, 1)
    assert c.limits == {"memory": "32Mi"}
    assert c.liveness.target == "/healthz:8080"
    secret_ref = next(e for e in c.env if e.var == "DB_PASS")
    assert (secret_ref.source, secret_ref.source_name, secret_ref.key, secret_ref.value) == (
        "secret",
        "db",
        "password",
        None,
    )


# --- MCP boundary -----------------------------------------------------------


def _call(tool, args):
    async def go():
        async with Client(k8s_read.mcp) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    return asyncio.run(go())


def test_mcp_tool_output_is_redacted_and_has_evidence_id(monkeypatch):
    leaked = "connecting with password=hunter2 to db\n"
    core = FakeCore([HEALTHY_POD], logs={("web-1", "web", False): leaked})
    monkeypatch.setattr(k8s_read, "get_core_api", lambda: core)

    first = _call("get_pod_logs", {"pod_name": "web-1", "namespace": NS})
    data = first.structured_content
    assert "hunter2" not in data["text"]
    assert data["evidence_id"].startswith("ev-")
    # Same call, same output -> same id (needed for cassette replay).
    assert (
        _call("get_pod_logs", {"pod_name": "web-1", "namespace": NS}).structured_content["evidence_id"]
        == (data["evidence_id"])
    )


def test_mcp_api_errors_returned_as_tool_errors(monkeypatch):
    monkeypatch.setattr(k8s_read, "get_core_api", lambda: FakeCore([]))
    result = _call("get_pod_logs", {"pod_name": "missing", "namespace": NS})
    assert result.is_error
    assert "404" in result.content[0].text


def test_mcp_missing_kubeconfig_is_a_tool_error(monkeypatch):
    from kubernetes.config.config_exception import ConfigException

    def no_config():
        raise ConfigException("Invalid kube-config file. No configuration found.")

    monkeypatch.setattr(k8s_read, "get_core_api", no_config)
    result = _call("list_pods", {"namespace": NS})
    assert result.is_error
    assert "Cannot reach the Kubernetes API" in result.content[0].text


def test_previous_falls_back_when_container_just_terminated():
    # The window between the crash and back-off: state is terminated, not waiting.
    status = OOM_POD.status.container_statuses[0].model_copy(
        update={"state": V1ContainerState(terminated=V1ContainerStateTerminated(reason="OOMKilled", exit_code=137))}
    )
    pod = OOM_POD.model_copy(update={"status": V1PodStatus(phase="Running", container_statuses=[status])})
    name = pod.metadata.name
    core = FakeCore([pod], logs={(name, "memory-hog", True): GONE, (name, "memory-hog", False): "allocating\n"})
    logs = get_pod_logs_impl(core, name, NS, previous=True)
    assert logs.text == "allocating\n" and logs.note
