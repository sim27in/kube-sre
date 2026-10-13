"""Integration tests against a live kind cluster. Run with: uv run pytest -m kind"""

import time
import uuid
from pathlib import Path

import pytest
import yaml
from kubernetes.client import V1Namespace, V1ObjectMeta

from kube_sre.config import get_apps_api, get_core_api
from kube_sre.tools.k8s_read import get_events_impl, get_owner_impl, get_pod_logs_impl, list_pods_impl

pytestmark = pytest.mark.kind

K8S_DIR = Path(__file__).parents[2] / "k8s"


@pytest.fixture
def namespace():
    core = get_core_api()
    name = f"kube-sre-it-{uuid.uuid4().hex[:6]}"
    core.create_namespace(V1Namespace(metadata=V1ObjectMeta(name=name)))
    yield name
    core.delete_namespace(name)


def _deploy(manifest: str, namespace: str, command: list[str] | None = None) -> str:
    dep = yaml.safe_load((K8S_DIR / manifest).read_text())
    dep["metadata"]["namespace"] = namespace
    if command:
        dep["spec"]["template"]["spec"]["containers"][0]["command"] = command
    get_apps_api().create_namespaced_deployment(namespace, dep)
    return dep["metadata"]["name"]


def _wait_for(predicate, namespace, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pod in list_pods_impl(get_core_api(), namespace).pods:
            if predicate(pod):
                return pod
        time.sleep(3)
    pytest.fail(f"condition not met within {timeout}s")


def test_oom_previous_logs_and_owner(namespace):
    name = _deploy(
        "oom-app.yaml",
        namespace,
        command=["python", "-c", "print('allocating buffer', flush=True); x = 'A' * 100 * 1024 * 1024"],
    )
    pod = _wait_for(
        lambda p: any(c.last_termination and c.last_termination.reason == "OOMKilled" for c in p.containers),
        namespace,
    )
    assert pod.containers[0].last_termination.exit_code == 137

    logs = get_pod_logs_impl(get_core_api(), pod.name, namespace, previous=True)
    assert "allocating buffer" in logs.text

    owner = get_owner_impl(get_core_api(), get_apps_api(), pod.name, namespace)
    assert owner.workload.kind == "Deployment" and owner.workload.name == name


def test_image_pull_events(namespace):
    _deploy("broken-app.yaml", namespace)
    pod = _wait_for(
        lambda p: any(c.reason in ("ErrImagePull", "ImagePullBackOff") for c in p.containers),
        namespace,
    )
    reasons = {e.reason for e in get_events_impl(get_core_api(), namespace, pod.name, kind="Pod").events}
    assert reasons & {"Failed", "BackOff"}
