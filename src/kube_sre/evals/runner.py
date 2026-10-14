"""Run agents against scenario snapshots, score each run, and write the results.

Each run starts its own replay tool server, so runs are independent and can go in parallel. Both agents
get the same user prompt, model, step limit and timeout; they differ only in tools, schema and system prompt.
"""

import asyncio
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel

from kube_sre.agent import AgentLimitError, build_agent, invoke, mcp_client, token_usage, user_prompt
from kube_sre.citations import received_evidence_ids
from kube_sre.config import get_settings
from kube_sre.evals.judge import judge
from kube_sre.evals.scenarios import EVALS_DIR, Scenario
from kube_sre.evals.scoring import SPEC_TOOL, RunScore, find_leaks, match_incident, score_run

AGENTS = ("v0", "v1")
RESULTS_DIR = EVALS_DIR / "results"
_V0_SCHEMAS = Path(__file__).parents[3] / "baselines" / "v0" / "schemas.py"


class RunRecord(BaseModel):
    agent: str
    scenario: str
    repeat: int
    model: str
    error: str | None = None
    seconds: float
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: list[dict[str, Any]]
    tool_errors: list[str] = []
    report: dict[str, Any] | None
    score: RunScore


def _v0_report_schema():
    spec = importlib.util.spec_from_file_location("v0_schemas", _V0_SCHEMAS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TriageReport


def _build(agent: str, tools):
    if agent == "v1":
        return build_agent(tools)
    return build_agent(tools, system_prompt=None, report_schema=_v0_report_schema())  # v0 had no system prompt


def _tool_errors(messages, tool_names: set[str]) -> list[str]:
    return [
        f"{m.name}: {str(m.content)[:300]}"
        for m in messages
        if isinstance(m, ToolMessage) and m.name in tool_names and m.status == "error"
    ]


def _tool_calls(messages, tool_names: set[str]) -> list[dict[str, Any]]:
    return [
        {"name": c["name"], "args": c["args"]}
        for m in messages
        if isinstance(m, AIMessage)
        for c in m.tool_calls
        if c["name"] in tool_names
    ]


async def _judge_run(judge_model, scenario: Scenario, report: dict[str, Any] | None, score: RunScore) -> None:
    if report is None:
        return
    for expected, inc_score in zip(scenario.incidents, score.incidents, strict=True):
        if not inc_score.detected:
            continue
        incident = match_incident(expected, report.get("incidents", []))
        try:
            verdict = await judge(judge_model, expected, incident)
        except Exception as e:  # a judge failure must not lose the run; it shows up as an ungraded incident
            inc_score.judge_reasoning = f"JUDGE ERROR: {type(e).__name__}: {e}"
            continue
        inc_score.root_cause, inc_score.remediation = verdict.root_cause, verdict.remediation
        inc_score.judge_reasoning = verdict.reasoning


async def run_once(agent: str, scenario: Scenario, repeat: int, judge_model) -> RunRecord:
    server = ["-m", "kube_sre.evals.replay", agent, str(scenario.snapshot)]
    async with mcp_client(server).session("k8s_read") as session:
        tools = await load_mcp_tools(session)
        tool_names = {t.name for t in tools}
        start = time.monotonic()
        error, report, messages = None, None, []
        try:
            response = await invoke(_build(agent, tools), user_prompt(scenario.namespace))
            report = response["structured_response"].model_dump(mode="json")
            messages = response["messages"]
        except AgentLimitError as e:
            error, messages = str(e), e.state["messages"]
        except Exception as e:  # e.g. the model never produced a valid report
            error = f"{type(e).__name__}: {e}"
        seconds = time.monotonic() - start

    calls = _tool_calls(messages, tool_names)
    received = received_evidence_ids(messages, tool_names) if agent == "v1" else None
    score = score_run(scenario, report, calls, tool_names, received)
    # Everything the model saw or said, which is also what the LangSmith trace records.
    trace = "\n".join(m.model_dump_json() for m in messages)
    score.leaked_secrets = find_leaks(scenario.secrets, json.dumps(report), trace)
    if judge_model is not None:
        await _judge_run(judge_model, scenario, report, score)
    tokens_in, tokens_out = token_usage(messages)
    return RunRecord(
        agent=agent,
        scenario=scenario.id,
        repeat=repeat,
        model=get_settings().agent_model,
        error=error,
        seconds=round(seconds, 1),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tool_calls=calls,
        tool_errors=_tool_errors(messages, tool_names),
        report=report,
        score=score,
    )


def _progress(r: RunRecord) -> str:
    if r.error:
        status = f"ERROR {r.error[:80]}"
    else:
        rc = ",".join(
            f"{i.workload}:{'cat' if i.category_ok else 'CAT'}/rc{i.root_cause}/fix{i.remediation}"
            if i.detected
            else f"{i.workload}:MISSED"
            for i in r.score.incidents
        )
        fp = f" FP={r.score.false_positives}" if r.score.false_positives else ""
        leaks = f" LEAKED={len(r.score.leaked_secrets)}" if r.score.leaked_secrets else ""
        status = f"{'PASS' if r.score.passed else 'fail'} {rc}{fp}{leaks}"
    return f"[{r.agent} {r.scenario} #{r.repeat}] {r.seconds:.0f}s {len(r.tool_calls)} calls  {status}"


async def run_all(
    agents: list[str], scenarios: list[Scenario], repeats: int, concurrency: int, judge_model, label: str = ""
) -> Path:
    missing = [s.id for s in scenarios if not s.snapshot.exists()]
    if missing:
        raise SystemExit(f"No snapshot for {missing}; record them first: python -m kube_sre.evals record")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS_DIR / (f"{stamp}-{label}" if label else stamp)
    out_dir.mkdir(parents=True)
    runs_file = out_dir / "runs.jsonl"
    sem = asyncio.Semaphore(concurrency)

    async def one(agent: str, scenario: Scenario, repeat: int) -> None:
        async with sem:
            record = await run_once(agent, scenario, repeat, judge_model)
        with runs_file.open("a") as f:
            f.write(record.model_dump_json() + "\n")
        print(_progress(record), flush=True)

    jobs = [one(a, s, r) for r in range(1, repeats + 1) for s in scenarios for a in agents]
    print(f"{len(jobs)} runs -> {out_dir}")
    await asyncio.gather(*jobs)
    return out_dir


async def rescore(src_dir: Path, scenarios: dict[str, Scenario], judge_model, concurrency: int) -> Path:
    """Score existing runs again with the current scenario definitions and judge, without rerunning the agents.
    Citation results are carried over (the received evidence ids aren't stored). Writes a new results dir."""
    out_dir = src_dir.with_name(src_dir.name + "-rescored")
    out_dir.mkdir()
    sem = asyncio.Semaphore(concurrency)

    async def one(old: RunRecord) -> RunRecord:
        scenario = scenarios[old.scenario]
        tool_names = {c["name"] for c in old.tool_calls} | ({SPEC_TOOL} if old.agent == "v1" else set())
        score = score_run(scenario, old.report, old.tool_calls, tool_names, None)
        score.citations, score.invalid_citations = old.score.citations, old.score.invalid_citations
        # The trace isn't stored, so leaks found in it during the run are carried over.
        score.leaked_secrets = sorted(
            set(old.score.leaked_secrets) | set(find_leaks(scenario.secrets, json.dumps(old.report)))
        )
        async with sem:
            await _judge_run(judge_model, scenario, old.report, score)
        return old.model_copy(update={"score": score})

    records = await asyncio.gather(*(one(r) for r in load_runs(src_dir)))
    (out_dir / "runs.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in records))
    return out_dir


def load_runs(out_dir: Path) -> list[RunRecord]:
    lines = (out_dir / "runs.jsonl").read_text().splitlines()
    return [RunRecord.model_validate_json(line) for line in lines if line.strip()]
