"""Check that a report's evidence cites tool results the agent actually received."""

import re
from collections.abc import Iterable

from langchain_core.messages import BaseMessage, ToolMessage

from kube_sre.schemas import TriageReport

_EVIDENCE_ID = re.compile(r"\bev-[0-9a-f]{10}\b")


def received_evidence_ids(messages: Iterable[BaseMessage], tool_names: set[str]) -> set[str]:
    """Evidence ids returned by the given tools. Only those tools count: the structured-output step also
    produces a ToolMessage, and it echoes the report's own citations."""
    ids: set[str] = set()
    for m in messages:
        if isinstance(m, ToolMessage) and m.name in tool_names:
            ids.update(_EVIDENCE_ID.findall(str(m.content)))
    return ids


def invalid_citations(report: TriageReport, received: set[str]) -> list[str]:
    """Evidence ids cited in the report that no tool returned (invented or mangled by the model)."""
    return [e.evidence_id for i in report.incidents for e in i.evidence if e.evidence_id not in received]
