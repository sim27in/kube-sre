from collections import Counter

import pytest

from kube_sre.guardrails.redaction import redact, redact_obj

# (seeded line, the secret substring that must not survive, expected marker)
SEEDED_SECRETS = [
    (
        "auth header: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "eyJhbGciOiJIUzI1NiJ9",
        "<redacted:",
    ),
    ("using key AKIAIOSFODNN7EXAMPLE for s3", "AKIAIOSFODNN7EXAMPLE", "<redacted:aws_access_key>"),
    (
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "<redacted:secret>",
    ),
    ("connecting to postgres://app:s3cr3tP4ss@db:5432/orders", "s3cr3tP4ss", "<redacted:credentials>"),
    ("DB_PASSWORD=hunter2 loaded", "hunter2", "<redacted:secret>"),
    ("STRIPE_API_KEY: sk_live_51Habc", "sk_live_51Habc", "<redacted:secret>"),
    ('{"api_key": "sk-live-abcdef123456"}', "sk-live-abcdef123456", "<redacted:secret>"),
    ("token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "<redacted:"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA==", "<redacted:token>"),
    (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "MIIEpAIBAAKCAQEA",
        "<redacted:private_key>",
    ),
]


@pytest.mark.parametrize(("line", "secret", "marker"), SEEDED_SECRETS)
def test_seeded_secrets_are_removed(line, secret, marker):
    out = redact(line).text
    assert secret not in out
    assert marker in out


def test_email_pseudonym_is_stable():
    a = redact("alert sent to oncall@example.com").text
    b = redact("oncall@example.com acknowledged").text
    assert "oncall@example.com" not in a
    token = a.split()[-1]
    assert token.startswith("<email:") and token in b


def test_private_ips_kept_public_ips_pseudonymised():
    out = redact("dial tcp 10.96.0.10:53: i/o timeout; client 8.8.4.4 rejected").text
    assert "10.96.0.10" in out  # cluster DNS: diagnostic, not PII
    assert "8.8.4.4" not in out and "<ip:" in out


def test_person_names_redacted():
    out = redact("User John Smith reported the checkout failure").text
    assert "John Smith" not in out
    assert "<person:" in out


def test_k8s_lines_left_intact():
    line = "Back-off restarting failed container memory-hog in pod analytics-processor-7d9f"
    assert redact(line).text == line


def test_redact_obj_walks_nested_structures():
    counts = Counter()
    out = redact_obj({"logs": ["password=abc123"], "n": 3, "nested": {"k": "AKIAIOSFODNN7EXAMPLE"}}, counts)
    assert out["n"] == 3
    assert "abc123" not in out["logs"][0]
    assert "AKIA" not in out["nested"]["k"]
    assert counts["key_value"] == 1 and counts["aws_access_key"] == 1
