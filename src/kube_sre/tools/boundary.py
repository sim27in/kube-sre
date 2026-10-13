"""The tool boundary: every tool result is redacted and stamped with an evidence id before it leaves the server."""

import hashlib
import json
import sys
from collections import Counter
from typing import Any, NoReturn

from fastmcp.exceptions import ToolError

from kube_sre.guardrails.redaction import redact, redact_obj
from kube_sre.tools.models import ToolResult


def evidence_id(tool: str, args: dict[str, Any], payload: dict[str, Any]) -> str:
    """Deterministic id: the same call with the same output gets the same id, so cassettes replay cleanly."""
    blob = json.dumps({"tool": tool, "args": args, "payload": payload}, sort_keys=True, default=str)
    return "ev-" + hashlib.sha256(blob.encode()).hexdigest()[:10]


def finalize[T: ToolResult](tool: str, args: dict[str, Any], result: T) -> T:
    counts: Counter[str] = Counter()
    payload = redact_obj(result.model_dump(mode="json", exclude={"evidence_id"}), counts)
    if counts:
        print(f"[redaction] {tool}: {dict(counts)}", file=sys.stderr)
    return type(result).model_validate({**payload, "evidence_id": evidence_id(tool, args, payload)})


def fail(message: str) -> NoReturn:
    """Raise a tool error the agent sees as a message. Error text can echo user data, so redact it too."""
    raise ToolError(redact(message).text)
