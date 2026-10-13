from langchain_core.messages import AIMessage, ToolMessage

from kube_sre.citations import invalid_citations, received_evidence_ids
from kube_sre.schemas import TriageReport

TOOLS = {"list_pods", "get_events"}
REAL, FAKE = "ev-0123456789", "ev-abcdefabcd"


def _report(*ids):
    incident = {
        "resource_kind": "Deployment",
        "resource_name": "api",
        "pod_name": "api-1",
        "namespace": "default",
        "category": "oom_killed",
        "severity": "high",
        "root_cause": "Memory limit too low.",
        "evidence": [{"evidence_id": i, "source": "pod_status", "detail": "OOMKilled"} for i in ids],
        "proposed_actions": [{"action_type": "patch_resources", "description": "raise", "suggested_command": "k"}],
        "confidence": 0.8,
    }
    return TriageReport(namespace="default", summary="s", incidents=[incident])


def test_only_tool_results_count_as_received():
    messages = [
        AIMessage(content="looking"),
        ToolMessage(content=f'{{"evidence_id": "{REAL}"}}', name="list_pods", tool_call_id="1"),
        # The structured-output step echoes the report, including any invented id.
        ToolMessage(content=f"Returning structured response: {FAKE}", name="TriageReport", tool_call_id="2"),
    ]
    assert received_evidence_ids(messages, TOOLS) == {REAL}


def test_invalid_citations_listed():
    assert invalid_citations(_report(REAL, FAKE), {REAL}) == [FAKE]
    assert invalid_citations(_report(REAL), {REAL}) == []
