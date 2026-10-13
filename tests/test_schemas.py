import pytest
from pydantic import ValidationError

from kube_sre.schemas import FailureCategory, IncidentReport, Severity


def _report(**overrides):
    data = {
        "resource_kind": "Deployment",
        "resource_name": "payment-service",
        "pod_name": "payment-service-abc12",
        "namespace": "default",
        "category": "oom_killed",
        "severity": "high",
        "root_cause": "Container exceeds its 64Mi memory limit.",
        "evidence": [{"evidence_id": "ev-0123456789", "source": "events", "detail": "OOMKilled, exit code 137"}],
        "proposed_actions": [
            {
                "action_type": "patch_resources",
                "description": "Raise the memory limit.",
                "suggested_command": "kubectl set resources deploy/payment-service --limits=memory=256Mi",
            }
        ],
        "confidence": 0.8,
    }
    data.update(overrides)
    return IncidentReport.model_validate(data)


def test_valid_report_parses_enums():
    report = _report()
    assert report.category is FailureCategory.OOM_KILLED
    assert report.severity is Severity.HIGH


def test_report_requires_evidence():
    with pytest.raises(ValidationError):
        _report(evidence=[])


def test_confidence_bounded():
    with pytest.raises(ValidationError):
        _report(confidence=1.5)
