"""Secret and PII redaction, applied at the tool boundary so nothing reaches the LLM or traces unredacted.

Secrets are replaced with a typed marker (`<redacted:jwt>`). PII is replaced with a stable pseudonym
(`<email:3fa2c1>`) so the agent can still correlate the same value across tool calls, and recorded
cassettes replay identically.
"""

import hashlib
import hmac
import ipaddress
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from kube_sre.config import get_settings

# (name, pattern, replacement). Order matters: specific patterns run before generic key=value.
_SECRET_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "<redacted:private_key>",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "<redacted:jwt>",
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted:aws_access_key>"),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "<redacted:github_token>"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted:slack_token>"),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 <redacted:token>",
    ),
    (
        "conn_string",
        re.compile(r"\b([a-z][a-z0-9+.-]*://)[^:/\s@]+:[^@\s/]+@"),
        r"\1<redacted:credentials>@",
    ),
    (
        "key_value",
        re.compile(
            # Lookbehind instead of \b so prefixed names (DB_PASSWORD, STRIPE_API_KEY) match.
            r"(?i)(?<![a-z0-9])((?:aws_)?(?:secret(?:_access)?_key|password|passwd|pwd|secret|token|api[_-]?key"
            r"|access[_-]?key|client[_-]?secret))(\"?\s*[=:]\s*\"?)([^\s\"',;&]+)"
        ),
        r"\1\2<redacted:secret>",
    ),
]

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass
class RedactionResult:
    text: str
    counts: Counter[str] = field(default_factory=Counter)


def _pseudonym(kind: str, value: str) -> str:
    salt = get_settings().pseudonym_salt.encode()
    digest = hmac.new(salt, value.encode(), hashlib.sha256).hexdigest()[:6]
    return f"<{kind}:{digest}>"


def _redact_ip(match: re.Match[str], counts: Counter[str]) -> str:
    raw = match.group(0)
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw  # e.g. 999.1.1.1 or a version string
    # Cluster-internal addresses are diagnostic (kube-dns, pod IPs), not PII.
    if not ip.is_global:
        return raw
    counts["ip"] += 1
    return _pseudonym("ip", raw)


@lru_cache
def _person_analyzer():
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()
    return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])


def _redact_names(text: str, counts: Counter[str]) -> str:
    results = _person_analyzer().analyze(text=text, entities=["PERSON"], language="en", score_threshold=0.85)
    # Replace right to left so earlier offsets stay valid.
    for r in sorted(results, key=lambda r: r.start, reverse=True):
        value = text[r.start : r.end]
        if value.startswith("<"):  # already a placeholder
            continue
        counts["person"] += 1
        text = text[: r.start] + _pseudonym("person", value) + text[r.end :]
    return text


def redact(text: str) -> RedactionResult:
    counts: Counter[str] = Counter()
    for name, pattern, replacement in _SECRET_RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] += n

    def _email(m: re.Match[str]) -> str:
        counts["email"] += 1
        return _pseudonym("email", m.group(0))

    text = _EMAIL.sub(_email, text)
    text = _IPV4.sub(lambda m: _redact_ip(m, counts), text)
    if get_settings().redact_names and any(c.isalpha() for c in text):
        text = _redact_names(text, counts)
    return RedactionResult(text=text, counts=counts)


def redact_obj(obj: Any, counts: Counter[str] | None = None) -> Any:
    """Redact every string inside a JSON-like structure (dicts, lists, scalars)."""
    if counts is None:
        counts = Counter()
    if isinstance(obj, str):
        result = redact(obj)
        counts.update(result.counts)
        return result.text
    if isinstance(obj, dict):
        return {k: redact_obj(v, counts) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, counts) for v in obj]
    return obj
