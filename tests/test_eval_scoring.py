import pytest

from kube_sre.config import get_settings
from kube_sre.evals.judge import get_judge_model, judge_input
from kube_sre.evals.report import summarize
from kube_sre.evals.runner import RunRecord
from kube_sre.evals.scenarios import load_scenario
from kube_sre.evals.scoring import score_run

V1_TOOLS = {"list_pods", "get_pod_logs", "get_owner", "describe_workload"}


POD = "report-builder-7d9f8c6b5-x2x4z"


def _incident(resource_name="report-builder", pod_name=POD, category="oom_killed", ev="ev-1"):
    return {
        "resource_kind": "Deployment",
        "resource_name": resource_name,
        "pod_name": pod_name,
        "category": category,
        "root_cause": "32Mi limit, allocates 100MB",
        "evidence": [{"evidence_id": ev, "source": "spec", "detail": "limits.memory=32Mi"}],
        "proposed_actions": [
            {"action_type": "patch_resources", "description": "raise limit", "suggested_command": "kubectl ..."}
        ],
    }


def _report(*incidents):
    return {"namespace": "analytics", "summary": "", "incidents": list(incidents)}


@pytest.fixture
def oom():
    return load_scenario("oom-killed")


def test_correct_report(oom):
    calls = [{"name": "describe_workload", "args": {"name": "report-builder", "kind": "Deployment"}}]
    score = score_run(oom, _report(_incident()), calls, V1_TOOLS, {"ev-1"})
    inc = score.incidents[0]
    assert inc.detected and inc.workload_named and inc.category_ok and inc.spec_checked
    assert score.false_positives == [] and score.invalid_citations == []
    assert not score.passed  # the judge hasn't graded it yet
    inc.root_cause = inc.remediation = 2
    assert score.passed


def test_pod_named_report_is_detected_but_not_workload_named(oom):
    score = score_run(oom, _report(_incident(resource_name=POD)), [], V1_TOOLS, {"ev-1"})
    assert score.incidents[0].detected and not score.incidents[0].workload_named
    assert score.incidents[0].spec_checked is False
    replica_set = score_run(oom, _report(_incident("report-builder-7d9f8c6b5", "?")), [], V1_TOOLS, {"ev-1"})
    assert replica_set.incidents[0].detected


def test_workload_whose_name_extends_the_expected_one_is_a_false_positive(oom):
    # "report-builder-api" starts with "report-builder-" but is a different workload, not a generated suffix.
    report = _report(_incident("report-builder-api", "report-builder-api-6b8f75c545-n9d2d"))
    score = score_run(oom, report, [], V1_TOOLS, {"ev-1"})
    assert not score.incidents[0].detected
    assert score.false_positives == ["report-builder-api"]


def test_statefulset_pod_ordinal_matches():
    pvc = load_scenario("pvc-pending")
    report = _report(_incident("inventory-db-0", "inventory-db-0"))
    assert score_run(pvc, report, [], V1_TOOLS, {"ev-1"}).incidents[0].detected


def test_wrong_category_false_positive_and_invented_citation(oom):
    report = _report(_incident(category="crash_loop", ev="ev-made-up"), _incident("status-page", "status-page-x"))
    score = score_run(oom, report, [], V1_TOOLS, {"ev-1"})
    assert not score.incidents[0].category_ok
    assert score.false_positives == ["status-page"]
    assert score.invalid_citations == ["ev-made-up"]


def test_v0_has_no_citation_or_spec_metrics(oom):
    report = _report({k: v for k, v in _incident().items() if k != "evidence"})
    score = score_run(oom, report, [], {"get_cluster_pods", "get_pod_logs", "get_pod_events"}, None)
    assert score.citations is None and score.incidents[0].spec_checked is None


def test_missing_report_scores_nothing(oom):
    score = score_run(oom, None, [], V1_TOOLS, set())
    assert not score.completed and not score.incidents[0].detected and not score.passed


def test_healthy_scenario_passes_only_with_no_incidents():
    healthy = load_scenario("healthy")
    assert score_run(healthy, _report(), [], V1_TOOLS, set()).passed
    assert not score_run(healthy, _report(_incident("docs-site", "docs-site-1")), [], V1_TOOLS, {"ev-1"}).passed


def test_judge_input_has_ground_truth_and_diagnosis(oom):
    text = judge_input(oom.incidents[0], _incident())
    assert "the memory limit is 32Mi" in text and "32Mi limit, allocates 100MB" in text and "patch_resources" in text


def test_judge_must_differ_from_agent(monkeypatch):
    monkeypatch.setattr(get_settings(), "judge_model", get_settings().agent_model)
    with pytest.raises(ValueError):
        get_judge_model()


def test_summary_has_a_column_per_agent(oom):
    runs = [
        RunRecord(
            agent=agent,
            scenario="oom-killed",
            repeat=1,
            model="m",
            seconds=1.0,
            tool_calls=[],
            report=_report(_incident()),
            score=score_run(oom, _report(_incident()), [], tools, received),
        )
        for agent, tools, received in [("v0", set(), None), ("v1", V1_TOOLS, {"ev-1"})]
    ]
    text = summarize(runs)
    assert "| Metric | v0 | v1 |" in text and "| oom-killed |" in text
