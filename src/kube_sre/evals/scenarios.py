"""Eval scenarios: a broken setup (manifest.yaml) and the known answer (scenario.yaml), one directory each."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from kube_sre.schemas import FailureCategory

EVALS_DIR = Path(__file__).parents[3] / "evals"
SCENARIOS_DIR = EVALS_DIR / "scenarios"
SNAPSHOTS_DIR = EVALS_DIR / "snapshots"


class WaitCondition(BaseModel):
    """One condition the recorder waits for before taking the snapshot. Set exactly one field."""

    container_reason: str | list[str] | None = None  # any (init) container's state or last-termination reason
    event_reason: str | None = None  # any event in the namespace
    restarts: int | None = None  # any (init) container restarted at least this often
    pod_phase: str | None = None  # any pod in this phase
    all_ready: bool | None = None  # at least one pod, and every container ready


class ExpectedIncident(BaseModel):
    workload: str
    kind: str = "Deployment"
    categories: list[FailureCategory] = Field(min_length=1, description="Accepted labels; the first is preferred.")
    needs_spec: bool = False  # the key facts are only visible in the workload spec (describe_workload)
    root_cause: str
    key_facts: list[str]
    fixes: list[str]


class Scenario(BaseModel):
    id: str
    namespace: str
    summary: str
    wait: list[WaitCondition]
    settle_seconds: int = 5
    incidents: list[ExpectedIncident]
    secrets: list[str] = []  # seeded fake credentials that must not reach the model or the report
    also_ok: list[str] = []  # workloads that may be reported too (a broken dependency), not false positives
    # kubectl args run before applying and after recording, for state outside the namespace (a node taint).
    # A scenario with setup is recorded on its own.
    setup: list[list[str]] = []
    teardown: list[list[str]] = []

    @property
    def manifest(self) -> Path:
        return SCENARIOS_DIR / self.id / "manifest.yaml"

    @property
    def snapshot(self) -> Path:
        return SNAPSHOTS_DIR / f"{self.id}.json"


def load_scenario(scenario_id: str) -> Scenario:
    data = yaml.safe_load((SCENARIOS_DIR / scenario_id / "scenario.yaml").read_text())
    return Scenario.model_validate({"id": scenario_id, **data})


def load_scenarios(ids: list[str] | None = None) -> list[Scenario]:
    available = sorted(p.parent.name for p in SCENARIOS_DIR.glob("*/scenario.yaml"))
    unknown = set(ids or []) - set(available)
    if unknown:
        raise ValueError(f"Unknown scenarios {sorted(unknown)}; available: {available}")
    return [load_scenario(i) for i in (ids or available)]
