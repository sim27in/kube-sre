"""Record scenario snapshots from a live cluster: apply each manifest in its own namespace, wait until the
failure shows, snapshot the namespace, delete it. Needs kubectl and a reachable cluster (kind)."""

import json
import subprocess
import time
from collections import Counter

from kube_sre.config import get_apps_api, get_core_api, get_settings
from kube_sre.evals.scenarios import SNAPSHOTS_DIR, Scenario, WaitCondition
from kube_sre.evals.snapshot import capture


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    context = get_settings().kube_context
    cmd = ["kubectl", *(["--context", context] if context else []), *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _statuses(pod):
    return (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])


def _reasons(cs) -> set[str]:
    out = set()
    if cs.state and cs.state.waiting:
        out.add(cs.state.waiting.reason)
    if cs.state and cs.state.terminated:
        out.add(cs.state.terminated.reason)
    if cs.last_state and cs.last_state.terminated:
        out.add(cs.last_state.terminated.reason)
    return out


def condition_met(cond: WaitCondition, pods, events) -> bool:
    if cond.container_reason is not None:
        wanted = {cond.container_reason} if isinstance(cond.container_reason, str) else set(cond.container_reason)
        return any(_reasons(cs) & wanted for p in pods for cs in _statuses(p))
    if cond.event_reason is not None:
        return any(e.reason == cond.event_reason for e in events)
    if cond.restarts is not None:
        return any((cs.restart_count or 0) >= cond.restarts for p in pods for cs in _statuses(p))
    if cond.pod_phase is not None:
        return any(p.status.phase == cond.pod_phase for p in pods)
    if cond.all_ready is not None:
        ready = bool(pods) and all(
            p.status.container_statuses and all(cs.ready for cs in p.status.container_statuses) for p in pods
        )
        return ready == cond.all_ready
    raise ValueError(f"Empty wait condition: {cond}")


def _describe(namespace: str) -> str:
    return _kubectl("get", "pods,events", "-n", namespace, check=False).stdout


def record(scenarios: list[Scenario], keep: bool = False, timeout: int = 300, poll: float = 3.0) -> list[str]:
    """Record all scenarios in parallel. Returns the ids that failed to reach their wait conditions."""
    core, apps = get_core_api(), get_apps_api()
    if len(scenarios) > 1 and any(s.setup for s in scenarios):
        alone = [s.id for s in scenarios if s.setup]
        raise SystemExit(f"{alone} change cluster-wide state; record each of them on its own.")
    namespaces = Counter(s.namespace for s in scenarios)
    shared = sorted(s.id for s in scenarios if namespaces[s.namespace] > 1)
    if shared:
        # Both would be applied into one namespace, and the first to finish deletes it under the other.
        raise SystemExit(f"{shared} share a namespace; record them in separate batches.")
    for s in scenarios:
        for args in s.setup:
            _kubectl(*args)
        _kubectl("delete", "namespace", s.namespace, "--ignore-not-found", "--wait=true")
        _kubectl("create", "namespace", s.namespace)
        _kubectl("apply", "-n", s.namespace, "-f", str(s.manifest))
        print(f"[{s.id}] applied in namespace {s.namespace}")

    try:
        return _wait_and_capture(scenarios, core, apps, keep, timeout, poll)
    finally:
        for s in scenarios:
            for args in s.teardown:
                _kubectl(*args, check=False)


def _wait_and_capture(scenarios: list[Scenario], core, apps, keep: bool, timeout: int, poll: float) -> list[str]:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    pending = {s.id: s for s in scenarios}
    met_at: dict[str, float] = {}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for sid, s in list(pending.items()):
            if sid not in met_at:
                pods = core.list_namespaced_pod(s.namespace).items
                events = core.list_namespaced_event(s.namespace).items
                if all(condition_met(c, pods, events) for c in s.wait):
                    met_at[sid] = time.monotonic()
                    print(f"[{sid}] conditions met; settling {s.settle_seconds}s")
            if sid in met_at and time.monotonic() - met_at[sid] >= s.settle_seconds:
                snap = capture(core, apps, s.namespace)
                s.snapshot.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
                print(f"[{sid}] snapshot written: {len(snap['pods'])} pods, {len(snap['events'])} events")
                del pending[sid]
                if not keep:
                    _kubectl("delete", "namespace", s.namespace, "--wait=false")
        time.sleep(poll)

    for sid, s in pending.items():
        print(f"[{sid}] TIMED OUT after {timeout}s; namespace {s.namespace} kept for debugging.")
        print(_describe(s.namespace))
    return list(pending)
