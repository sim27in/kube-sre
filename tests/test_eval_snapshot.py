import json

import pytest
from kubernetes.client.rest import ApiException

from kube_sre.evals.scenarios import load_scenarios
from kube_sre.evals.snapshot import FakeAppsApi, FakeCoreApi, captured_at
from kube_sre.tools import k8s_read
from kube_sre.tools.boundary import finalize
from kube_sre.tools.k8s_read import (
    describe_workload_impl,
    get_events_impl,
    get_owner_impl,
    get_pod_logs_impl,
    list_nodes_impl,
    list_pods_impl,
    list_warning_events_impl,
)

NS = "demo"


def _pod(name, containers=("app",), labels=None):
    return {
        "metadata": {"name": name, "namespace": NS, "labels": labels or {}},
        "spec": {"containers": [{"name": c, "image": "img"} for c in containers]},
        "status": {"phase": "Running"},
    }


def _event(name, kind, reason, type_="Warning"):
    return {
        "metadata": {"name": f"{name}.{reason}", "namespace": NS},
        "involvedObject": {"kind": kind, "name": name, "namespace": NS},
        "reason": reason,
        "type": type_,
        "message": f"{reason} happened",
        "lastTimestamp": "2026-10-14T11:59:00Z",
    }


def _snapshot(**overrides):
    snap = {
        "version": 1,
        "namespace": NS,
        "captured_at": "2026-10-14T12:00:00+00:00",
        "pods": [_pod("web-1", labels={"app": "web"}), _pod("multi", containers=("a", "b"))],
        "events": [_event("web-1", "Pod", "BackOff"), _event("web-1", "Pod", "Pulled", "Normal")],
        "pvcs": [],
        "replicasets": [],
        "deployments": [],
        "statefulsets": [],
        "daemonsets": [],
        "logs": {
            "web-1/app/current": {
                "text": "2026-10-14T11:50:00.123456789Z old line\n"
                "2026-10-14T11:59:30.000000001Z new line\n"
                "2026-10-14T11:59:31Z newest line\n"
            },
            "web-1/app/previous": {"error": {"status": 400, "reason": "Bad Request", "body": '{"message": "gone"}'}},
        },
    }
    snap.update(overrides)
    return snap


def test_logs_strip_timestamps_and_apply_tail():
    core = FakeCoreApi(_snapshot())
    assert core.read_namespaced_pod_log("web-1", NS) == "old line\nnew line\nnewest line\n"
    assert core.read_namespaced_pod_log("web-1", NS, tail_lines=1) == "newest line\n"


def test_logs_since_seconds_is_relative_to_capture_time():
    core = FakeCoreApi(_snapshot())
    assert core.read_namespaced_pod_log("web-1", NS, since_seconds=60) == "new line\nnewest line\n"


def test_untimestamped_kubelet_text_is_kept():
    snap = _snapshot()
    snap["logs"]["web-1/app/current"] = {"text": "unable to retrieve container logs for containerd://abc"}
    assert FakeCoreApi(snap).read_namespaced_pod_log("web-1", NS) == (
        "unable to retrieve container logs for containerd://abc\n"
    )


def test_recorded_log_errors_are_raised():
    with pytest.raises(ApiException) as e:
        FakeCoreApi(_snapshot()).read_namespaced_pod_log("web-1", NS, previous=True)
    assert e.value.status == 400 and "gone" in e.value.body


def test_logs_need_container_name_for_multi_container_pods():
    with pytest.raises(ApiException) as e:
        FakeCoreApi(_snapshot()).read_namespaced_pod_log("multi", NS)
    assert e.value.status == 400


def test_event_field_selectors():
    core = FakeCoreApi(_snapshot())
    warnings = core.list_namespaced_event(NS, field_selector="type=Warning").items
    assert [e.reason for e in warnings] == ["BackOff"]
    by_object = core.list_namespaced_event(NS, field_selector="involvedObject.name=web-1,involvedObject.kind=Pod")
    assert len(by_object.items) == 2
    with pytest.raises(ApiException):
        core.list_namespaced_event(NS, field_selector="source=kubelet")


def test_label_selector_and_other_namespaces():
    core = FakeCoreApi(_snapshot())
    assert [p.metadata.name for p in core.list_namespaced_pod(NS, label_selector="app=web").items] == ["web-1"]
    assert core.list_namespaced_pod("elsewhere").items == []
    with pytest.raises(ApiException) as e:
        core.read_namespaced_pod("web-1", "elsewhere")
    assert e.value.status == 404


def test_missing_workload_is_404_with_api_message():
    with pytest.raises(ApiException) as e:
        FakeAppsApi(_snapshot()).read_namespaced_deployment("nope", NS)
    assert e.value.status == 404 and json.loads(e.value.body)["message"] == 'deployments.apps "nope" not found'


def test_unknown_snapshot_version_rejected():
    with pytest.raises(ValueError):
        FakeCoreApi(_snapshot(version=99))


# --- the recorded scenarios -------------------------------------------------


@pytest.mark.parametrize("scenario", [s for s in load_scenarios() if s.snapshot.exists()], ids=lambda s: s.id)
def test_recorded_snapshot_shows_each_expected_failure(scenario, monkeypatch):
    """Every expected workload is visible to the tools: through an unhealthy pod's owner, or a warning event."""
    snap = json.loads(scenario.snapshot.read_text())
    core, apps, ns = FakeCoreApi(snap), FakeAppsApi(snap), scenario.namespace
    monkeypatch.setattr(k8s_read, "_now", lambda: captured_at(snap))

    unhealthy = list_pods_impl(core, ns, only_unhealthy=True).pods
    owners = {get_owner_impl(core, apps, p.name, ns).workload.name for p in unhealthy}
    warned = {e.involved.name for e in list_warning_events_impl(core, ns).events}
    for expected in scenario.incidents:
        assert expected.workload in owners or any(n.startswith(expected.workload + "-") for n in warned)
        assert describe_workload_impl(apps, expected.kind, expected.workload, ns).name == expected.workload
    if not scenario.incidents:
        assert unhealthy == []

    nodes = list_nodes_impl(core).nodes
    assert nodes and all(n.ready for n in nodes)
    if any(e.categories[0] == "scheduling" for e in scenario.incidents) and "untolerated taint" in str(snap):
        assert any(n.taints for n in nodes)  # the scheduler message doesn't name the taint; list_nodes does

    for pod in unhealthy:  # logs and events replay without unexpected errors
        get_events_impl(core, ns, pod.name, kind="Pod")
        for c in pod.containers:
            if c.state != "waiting" or c.restart_count:
                get_pod_logs_impl(core, pod.name, ns, container=c.name)


@pytest.mark.parametrize(
    "scenario", [s for s in load_scenarios() if s.secrets and s.snapshot.exists()], ids=lambda s: s.id
)
def test_seeded_secrets_never_leave_the_tool_boundary(scenario, monkeypatch):
    """Every tool, on every pod and workload, with redaction applied as the server does: no seeded secret survives."""
    snap = json.loads(scenario.snapshot.read_text())
    core, apps, ns = FakeCoreApi(snap), FakeAppsApi(snap), scenario.namespace
    monkeypatch.setattr(k8s_read, "_now", lambda: captured_at(snap))
    assert any(s in scenario.snapshot.read_text() for s in scenario.secrets)  # the snapshot really has them

    pods = list_pods_impl(core, ns, only_unhealthy=False)
    results = [pods, list_warning_events_impl(core, ns)]
    for pod in pods.pods:
        results.append(get_events_impl(core, ns, pod.name, kind="Pod"))
        for c in pod.containers:
            for previous in (False, True):
                results.append(get_pod_logs_impl(core, pod.name, ns, container=c.name, previous=previous))
    results += [describe_workload_impl(apps, e.kind, e.workload, ns) for e in scenario.incidents]

    text = "\n".join(finalize("tool", {}, r).model_dump_json() for r in results)
    assert [s for s in scenario.secrets if s in text] == []
