"""Cluster snapshots: record a namespace's API state once, then serve it to the real tools offline.

A snapshot holds the raw API objects the tools read (pods, events, PVCs, workloads, nodes, Services, ConfigMaps,
and Secrets without their values) plus every container's current and previous logs. `FakeCoreApi` and
`FakeAppsApi` answer the same client calls from it, so the unchanged tool code (and the frozen v0 server) runs
against it with any arguments the agent picks.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.client.rest import ApiException

SNAPSHOT_VERSION = 2  # 2: Services, ConfigMaps and Secret keys
_MAX_LOG_LINES = 2000
_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$")
_SERIALIZER = ApiClient()


def _raw(obj) -> dict[str, Any]:
    data = _SERIALIZER.sanitize_for_serialization(obj)
    data.get("metadata", {}).pop("managedFields", None)  # large and never read
    return data


def _raw_node(node) -> dict[str, Any]:
    data = _raw(node)
    data.get("status", {}).pop("images", None)  # every cached image on the node; large and never read
    return data


def _raw_secret(secret) -> dict[str, Any]:
    """Name, type and key names only. Values never reach a snapshot (they're committed), and neither do the
    annotations, where kubectl's last-applied-configuration keeps a copy of them."""
    data = _raw(secret)
    data["metadata"].pop("annotations", None)
    data.pop("stringData", None)
    data["data"] = dict.fromkeys(data.get("data") or {}, "")
    return data


def _log_entry(core: CoreV1Api, pod: str, namespace: str, container: str, previous: bool) -> dict[str, Any]:
    try:
        text = core.read_namespaced_pod_log(
            pod, namespace, container=container, previous=previous, timestamps=True, tail_lines=_MAX_LOG_LINES
        )
        return {"text": text or ""}
    except ApiException as e:
        return {"error": {"status": e.status, "reason": e.reason, "body": e.body}}


def capture(core: CoreV1Api, apps: AppsV1Api, namespace: str) -> dict[str, Any]:
    """Read everything the tools can ask for in `namespace`."""
    pods = core.list_namespaced_pod(namespace).items
    logs: dict[str, Any] = {}
    for pod in pods:
        containers = (pod.spec.init_containers or []) + pod.spec.containers
        for c in containers:
            for previous in (False, True):
                key = _log_key(pod.metadata.name, c.name, previous)
                logs[key] = _log_entry(core, pod.metadata.name, namespace, c.name, previous)
    return {
        "version": SNAPSHOT_VERSION,
        "namespace": namespace,
        "captured_at": datetime.now(UTC).isoformat(),
        "pods": [_raw(p) for p in pods],
        "events": [_raw(e) for e in core.list_namespaced_event(namespace).items],
        "pvcs": [_raw(p) for p in core.list_namespaced_persistent_volume_claim(namespace).items],
        "replicasets": [_raw(r) for r in apps.list_namespaced_replica_set(namespace).items],
        "deployments": [_raw(d) for d in apps.list_namespaced_deployment(namespace).items],
        "statefulsets": [_raw(s) for s in apps.list_namespaced_stateful_set(namespace).items],
        "daemonsets": [_raw(d) for d in apps.list_namespaced_daemon_set(namespace).items],
        "nodes": [_raw_node(n) for n in core.list_node().items],
        "services": [_raw(s) for s in core.list_namespaced_service(namespace).items],
        "configmaps": [_raw(c) for c in core.list_namespaced_config_map(namespace).items],
        "secrets": [_raw_secret(s) for s in core.list_namespaced_secret(namespace).items],
        "logs": logs,
    }


# --- replay -----------------------------------------------------------------


def _log_key(pod: str, container: str, previous: bool) -> str:
    return f"{pod}/{container}/{'previous' if previous else 'current'}"


def _api_error(status: int, reason: str, message: str) -> ApiException:
    body = json.dumps({"kind": "Status", "status": "Failure", "message": message, "code": status})
    return ApiException(status=status, reason=reason, body=body)


def _not_found(resource: str, name: str) -> ApiException:
    return _api_error(404, "Not Found", f'{resource} "{name}" not found')


def _parse_selector(selector: str | None) -> list[tuple[str, str, str]]:
    """Equality selectors only ("a=b,c!=d"); that's all the tools and v0 use."""
    terms = []
    for term in filter(None, (selector or "").split(",")):
        m = re.fullmatch(r"\s*([\w./-]+)\s*(!=|==|=)\s*([\w./-]*)\s*", term)
        if not m:
            raise _api_error(400, "Bad Request", f"selector {term!r} is not supported by the snapshot replay")
        key, op, value = m.groups()
        terms.append((key, "!=" if op == "!=" else "=", value))
    return terms


def _matches(terms: list[tuple[str, str, str]], lookup) -> bool:
    return all((lookup(k) == v) == (op == "=") for k, op, v in terms)


def _parse_timestamp(token: str) -> datetime | None:
    m = _TIMESTAMP.match(token)
    if not m:
        return None
    whole, frac, tz = m.groups()
    return datetime.fromisoformat(whole + (frac or "")[:7] + ("+00:00" if tz == "Z" else tz))


class _Snapshot:
    def __init__(self, data: dict[str, Any]):
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError(f"Unsupported snapshot version {data.get('version')}")
        self.data = data
        self.namespace: str = data["namespace"]
        self.captured_at = datetime.fromisoformat(data["captured_at"])

    def items(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        return self.data[kind] if namespace == self.namespace else []

    def get(self, kind: str, resource: str, name: str, namespace: str) -> dict[str, Any]:
        for item in self.items(kind, namespace):
            if item["metadata"]["name"] == name:
                return item
        raise _not_found(resource, name)

    @staticmethod
    def build(klass: str, data: Any):
        return _SERIALIZER.deserialize(json.dumps(data), klass, "application/json")


class FakeCoreApi:
    """The CoreV1Api calls the tools make, answered from a snapshot."""

    def __init__(self, snapshot: dict[str, Any]):
        self._s = _Snapshot(snapshot)

    def list_namespaced_pod(self, namespace, label_selector=None, **_):
        terms = _parse_selector(label_selector)
        pods = [
            p
            for p in self._s.items("pods", namespace)
            if _matches(terms, lambda k, p=p: (p["metadata"].get("labels") or {}).get(k))
        ]
        return self._s.build("V1PodList", {"items": pods})

    def read_namespaced_pod(self, name, namespace, **_):
        return self._s.build("V1Pod", self._s.get("pods", "pods", name, namespace))

    def list_namespaced_event(self, namespace, field_selector=None, **_):
        fields = {
            "involvedObject.name": lambda e: e["involvedObject"].get("name"),
            "involvedObject.kind": lambda e: e["involvedObject"].get("kind"),
            "involvedObject.namespace": lambda e: e["involvedObject"].get("namespace"),
            "reason": lambda e: e.get("reason"),
            "type": lambda e: e.get("type"),
        }
        terms = _parse_selector(field_selector)
        for key, _op, _v in terms:
            if key not in fields:
                raise _api_error(400, "Bad Request", f'field label not supported: "{key}"')
        events = [e for e in self._s.items("events", namespace) if _matches(terms, lambda k, e=e: fields[k](e) or "")]
        return self._s.build("CoreV1EventList", {"items": events})

    def list_node(self, **_):
        return self._s.build("V1NodeList", {"items": self._s.data["nodes"]})

    def read_namespaced_service(self, name, namespace, **_):
        return self._s.build("V1Service", self._s.get("services", "services", name, namespace))

    def read_namespaced_config_map(self, name, namespace, **_):
        return self._s.build("V1ConfigMap", self._s.get("configmaps", "configmaps", name, namespace))

    def read_namespaced_secret(self, name, namespace, **_):
        return self._s.build("V1Secret", self._s.get("secrets", "secrets", name, namespace))

    def list_namespaced_persistent_volume_claim(self, namespace, **_):
        return self._s.build("V1PersistentVolumeClaimList", {"items": self._s.items("pvcs", namespace)})

    def read_namespaced_pod_log(
        self,
        name,
        namespace,
        container=None,
        previous=False,
        tail_lines=None,
        since_seconds=None,
        timestamps=False,
        **_,
    ) -> str:
        pod = self._s.get("pods", "pods", name, namespace)
        if container is None:
            names = [c["name"] for c in pod["spec"]["containers"]]
            if len(names) != 1:
                raise _api_error(
                    400, "Bad Request", f"a container name must be specified for pod {name}, choose one of: {names}"
                )
            container = names[0]
        entry = self._s.data["logs"].get(_log_key(name, container, bool(previous)))
        if entry is None:
            raise _api_error(400, "Bad Request", f"container {container} is not valid for pod {name}")
        if "error" in entry:
            err = entry["error"]
            raise ApiException(status=err["status"], reason=err["reason"], body=err["body"])

        cutoff = self._s.captured_at - timedelta(seconds=since_seconds) if since_seconds else None
        lines = []
        for line in entry["text"].splitlines():
            token, _, message = line.partition(" ")
            ts = _parse_timestamp(token)
            if ts is None:  # kubelet status text such as "unable to retrieve container logs"
                lines.append(line)
            elif cutoff is None or ts >= cutoff:
                lines.append(line if timestamps else message)
        if tail_lines:
            lines = lines[-tail_lines:]
        return "".join(f"{line}\n" for line in lines)


class FakeAppsApi:
    """The AppsV1Api calls the tools make, answered from a snapshot."""

    def __init__(self, snapshot: dict[str, Any]):
        self._s = _Snapshot(snapshot)

    def read_namespaced_replica_set(self, name, namespace, **_):
        return self._s.build("V1ReplicaSet", self._s.get("replicasets", "replicasets.apps", name, namespace))

    def read_namespaced_deployment(self, name, namespace, **_):
        return self._s.build("V1Deployment", self._s.get("deployments", "deployments.apps", name, namespace))

    def read_namespaced_stateful_set(self, name, namespace, **_):
        return self._s.build("V1StatefulSet", self._s.get("statefulsets", "statefulsets.apps", name, namespace))

    def read_namespaced_daemon_set(self, name, namespace, **_):
        return self._s.build("V1DaemonSet", self._s.get("daemonsets", "daemonsets.apps", name, namespace))

    def list_namespaced_deployment(self, namespace, **_):
        return self._s.build("V1DeploymentList", {"items": self._s.items("deployments", namespace)})

    def list_namespaced_stateful_set(self, namespace, **_):
        return self._s.build("V1StatefulSetList", {"items": self._s.items("statefulsets", namespace)})

    def list_namespaced_daemon_set(self, namespace, **_):
        return self._s.build("V1DaemonSetList", {"items": self._s.items("daemonsets", namespace)})


def captured_at(snapshot: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(snapshot["captured_at"])
