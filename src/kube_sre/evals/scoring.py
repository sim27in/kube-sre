"""Deterministic scoring of one run: which expected incidents were found, labels, false positives,
citations, and whether the agent read the spec when the answer is only there. The judge fills in the
root cause and remediation grades afterwards."""

import re
from typing import Any

from pydantic import BaseModel

from kube_sre.evals.scenarios import ExpectedIncident, Scenario

SPEC_TOOL = "describe_workload"


class IncidentScore(BaseModel):
    workload: str
    detected: bool
    workload_named: bool = False  # resource_name is the workload itself, not a pod or ReplicaSet
    category: str | None = None
    category_ok: bool = False
    spec_checked: bool | None = None  # None: the scenario doesn't need it, or the agent has no such tool
    root_cause: int = 0  # judge, 0-2
    remediation: int = 0  # judge, 0-2
    judge_reasoning: str | None = None


class RunScore(BaseModel):
    completed: bool
    incidents: list[IncidentScore]
    false_positives: list[str]
    citations: int | None = None  # None for agents whose schema has no evidence ids (v0)
    invalid_citations: list[str] | None = None

    @property
    def passed(self) -> bool:
        """Everything right: each incident found, labelled, diagnosed and fixed, and nothing invented."""
        return (
            self.completed
            and not self.false_positives
            and all(i.detected and i.category_ok and i.root_cause == 2 and i.remediation == 2 for i in self.incidents)
        )


# Suffixes Kubernetes generates for a workload's ReplicaSets and pods: hashes in its vowel-free alphabet
# (web-7d9f8c6b5, web-7d9f8c6b5-x2x4z) or a StatefulSet ordinal (db-0). A plain prefix match would take
# api-gateway for a pod of api.
_GENERATED_SUFFIX = r"(?:-[bcdfghjklmnpqrstvwxz2456789]{5,10}){1,2}|-\d+"


def _refers_to(incident: dict[str, Any], workload: str) -> bool:
    """True if the reported incident is about `workload`, named directly or through one of its pods/ReplicaSets."""
    pattern = re.compile(rf"{re.escape(workload.lower())}(?:{_GENERATED_SUFFIX})?")
    names = [str(incident.get("resource_name", "")).lower(), str(incident.get("pod_name", "")).lower()]
    return any(pattern.fullmatch(n) for n in names)


def match_incident(expected: ExpectedIncident, incidents: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [i for i in incidents if _refers_to(i, expected.workload)]
    exact = [i for i in matches if str(i.get("resource_name", "")).lower() == expected.workload.lower()]
    return (exact or matches or [None])[0]


def _spec_checked(expected: ExpectedIncident, tool_calls: list[dict[str, Any]], tool_names: set[str]) -> bool | None:
    if not expected.needs_spec or SPEC_TOOL not in tool_names:
        return None
    return any(c["name"] == SPEC_TOOL and c["args"].get("name") == expected.workload for c in tool_calls)


def score_run(
    scenario: Scenario,
    report: dict[str, Any] | None,
    tool_calls: list[dict[str, Any]],
    tool_names: set[str],
    received_ids: set[str] | None,
) -> RunScore:
    if report is None:
        return RunScore(
            completed=False,
            incidents=[IncidentScore(workload=e.workload, detected=False) for e in scenario.incidents],
            false_positives=[],
        )

    reported = report.get("incidents", [])
    scores = []
    for expected in scenario.incidents:
        found = match_incident(expected, reported)
        if found is None:
            scores.append(IncidentScore(workload=expected.workload, detected=False))
            continue
        category = found.get("category")
        scores.append(
            IncidentScore(
                workload=expected.workload,
                detected=True,
                workload_named=str(found.get("resource_name", "")).lower() == expected.workload.lower(),
                category=category,
                category_ok=category in expected.categories,
                spec_checked=_spec_checked(expected, tool_calls, tool_names),
            )
        )

    false_positives = [
        str(i.get("resource_name") or i.get("pod_name"))
        for i in reported
        if not any(_refers_to(i, e.workload) for e in scenario.incidents)
    ]

    if received_ids is None:
        return RunScore(completed=True, incidents=scores, false_positives=false_positives)
    cited = [e["evidence_id"] for i in reported for e in i.get("evidence", [])]
    return RunScore(
        completed=True,
        incidents=scores,
        false_positives=false_positives,
        citations=len(cited),
        invalid_citations=[c for c in cited if c not in received_ids],
    )
