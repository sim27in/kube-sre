"""MCP tool server that answers from a snapshot instead of a cluster.

    python -m kube_sre.evals.replay v1 evals/snapshots/oom-killed.json
    python -m kube_sre.evals.replay v0 evals/snapshots/oom-killed.json

v1 runs the real tool server (redaction, evidence ids and all) with the API clients and the clock swapped.
v0 runs the frozen baselines/v0/mcp_server.py unchanged, with kubernetes' config loader and client patched.
"""

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

from kube_sre.evals.snapshot import FakeAppsApi, FakeCoreApi, captured_at

V0_SERVER = Path(__file__).parents[3] / "baselines" / "v0" / "mcp_server.py"


def serve_v1(snapshot: dict) -> None:
    from kube_sre.tools import k8s_read

    core, apps, now = FakeCoreApi(snapshot), FakeAppsApi(snapshot), captured_at(snapshot)
    k8s_read.get_core_api = lambda: core
    k8s_read.get_apps_api = lambda: apps
    k8s_read._now = lambda: now
    k8s_read.mcp.run(show_banner=False)


def serve_v0(snapshot: dict) -> None:
    import kubernetes

    core = FakeCoreApi(snapshot)
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"  # v0 calls mcp.run() with the banner on
    kubernetes.config.load_kube_config = lambda *a, **k: None
    kubernetes.client.CoreV1Api = lambda *a, **k: core
    sys.argv = [str(V0_SERVER)]
    runpy.run_path(str(V0_SERVER), run_name="__main__")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=["v0", "v1"])
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    (serve_v0 if args.agent == "v0" else serve_v1)(snapshot)
