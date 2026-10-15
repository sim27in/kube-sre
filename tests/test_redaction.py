from collections import Counter

import pytest

from kube_sre.guardrails.redaction import is_secret_name, redact, redact_argv, redact_obj

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


def test_redact_obj_keeps_identifiers_and_redacts_names_in_free_text():
    # Presidio tags "grafana" and "kafka" as PERSON; pseudonymising them breaks follow-up tool calls.
    out = redact_obj(
        {
            "workload": {"kind": "Deployment", "name": "grafana"},
            "containers": [{"name": "kafka", "message": "User John Smith reported the checkout failure"}],
            "text": "User John Smith reported the checkout failure",
        }
    )
    assert out["workload"]["name"] == "grafana"
    assert out["containers"][0]["name"] == "kafka"
    assert "John Smith" not in out["containers"][0]["message"]
    assert "John Smith" not in out["text"]


def test_object_names_from_the_same_result_kept_in_free_text():
    # O3: "Deployment: grafana" became "Deployment: <person:...>" inside an event message.
    assert redact("Deployment: grafana").text != "Deployment: grafana"  # Presidio tags it on its own
    out = redact_obj(
        {"involved": {"kind": "Deployment", "name": "grafana-7d9f8c6b5"}, "message": "Deployment: grafana"}
    )
    assert out["message"] == "Deployment: grafana"
    out = redact_obj({"pod": "kafka-0", "message": "Deployment: kafka, owner Michael Johnson"})
    assert "Deployment: kafka" in out["message"]
    assert "Michael Johnson" not in out["message"]


def test_object_names_are_only_known_within_one_result():
    out = redact_obj({"name": "web", "message": "Deployment: grafana, owner Michael Johnson"})
    assert "Michael Johnson" not in out["message"]


def test_secrets_redacted_in_identifier_fields_too():
    assert "hunter2" not in redact_obj({"name": "password=hunter2"})["name"]


@pytest.mark.parametrize(
    "name", ["DB_PASSWORD", "STRIPE_API_KEY", "GITHUB_TOKEN", "--client-secret", "--secret-key", "PGPASSWORD"]
)
def test_secret_names(name):
    assert is_secret_name(name)


def test_glued_secret_names_in_text():
    assert redact("export PGPASSWORD=hunter2").text == "export PGPASSWORD=<redacted:secret>"
    assert "hunter2" not in redact("REDISCLI_AUTH=hunter2").text
    assert redact("passwordless=true").text == "passwordless=true"


@pytest.mark.parametrize("name", ["PASSWORD_FILE", "MAX_TOKENS", "LOG_LEVEL", "--port", "PASSWORDLESS"])
def test_names_that_are_not_secrets(name):
    assert not is_secret_name(name)


def test_redact_argv_hides_value_after_secret_flag():
    argv = ["serve", "--db-password", "hunter2", "--port", "8080", "--token=abc"]
    assert redact_argv(argv) == ["serve", "--db-password", "<redacted:secret>", "--port", "8080", "--token=abc"]
    assert redact_argv(["--password", "$(DB_PASSWORD)"]) == ["--password", "$(DB_PASSWORD)"]
    assert redact_argv(None) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('sh -c "app --db-password hunter2 --port 80"', 'sh -c "app --db-password <redacted:secret> --port 80"'),
        ("app --password 'two words' --verbose", "app --password <redacted:secret> --verbose"),
        ("server -token s3cret", "server -token <redacted:secret>"),
        ("app --client-secret abc123 && run", "app --client-secret <redacted:secret> && run"),
        ("app --secret-key=abc123", "app --secret-key=<redacted:secret>"),
    ],
)
def test_secret_flag_inside_a_string(line, expected):
    assert redact(line, names=False).text == expected


@pytest.mark.parametrize(
    "line",
    [
        "app --password $(DB_PASSWORD)",  # a reference, not the secret
        "app --password-file /etc/pw",  # the flag takes a path
        "app --password --port 80",  # no value
        "app --max-tokens 512",
    ],
)
def test_secret_flag_rule_keeps_non_secrets(line):
    assert redact(line, names=False).text == line


def test_redact_obj_walks_nested_structures():
    counts = Counter()
    out = redact_obj({"logs": ["password=abc123"], "n": 3, "nested": {"k": "AKIAIOSFODNN7EXAMPLE"}}, counts)
    assert out["n"] == 3
    assert "abc123" not in out["logs"][0]
    assert "AKIA" not in out["nested"]["k"]
    assert counts["key_value"] == 1 and counts["aws_access_key"] == 1
