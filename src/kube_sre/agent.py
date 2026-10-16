"""v1 single ReAct agent: the v0 agent on the fixed, typed tools. The evals compare it against baselines/v0."""

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.errors import GraphRecursionError

from kube_sre.citations import invalid_citations, received_evidence_ids
from kube_sre.config import get_settings
from kube_sre.llm import get_chat_model
from kube_sre.schemas import TriageReport

SYSTEM_PROMPT = (
    "You are an SRE investigating Kubernetes failures with read-only tools. "
    "Every tool result has an evidence_id; each piece of evidence in your report must cite the "
    "evidence_id of the tool result it came from. Report on the owning workload (use get_owner), not the pod.\n"
    "- Always call list_warning_events for the namespace, even if no pod looks unhealthy: failures that never "
    "create a pod (quota, admission) only show up there.\n"
    "- Report only workloads that are failing now. A pod that restarted long ago and has been Running and "
    "Ready since is history, not an incident. A container that keeps getting OOMKilled or crashing is failing "
    "now, even while it is Ready between restarts; list_pods(only_unhealthy=True) already tells the two apart.\n"
    "- Before blaming resource limits, requests, probes or configuration, read the workload spec with "
    "describe_workload and state the actual values in the root cause.\n"
    "- Confirm what you can check instead of hedging. If a referenced ConfigMap, Secret or image pull secret "
    "may be missing, check it with describe_config. If the app can't reach a dependency (connection refused, "
    "DNS failure, timeouts), check that Service with describe_service: why it has no ready pods is the root "
    "cause, and the fix is on the dependency.\n"
    "- Propose the smallest change that fixes the root cause. Don't invent values you haven't seen, such as "
    "image tags; if you don't know a valid one, propose a rollback (kubectl rollout undo). If memory grows "
    "steadily until the kill (a leak), raising the limit only delays it; say the code has to be fixed.\n"
    "- Fix the cause; never hide it. Don't propose removing an init container, probe, image pull secret or "
    "dependency check, marking a ConfigMap or Secret reference optional, creating an empty placeholder, or "
    "replacing the container command, not even as an extra option.\n"
    "- Every suggested command must work as written. Change containers with kubectl set image/resources/env "
    "or a strategic merge patch keyed by container name, never a --type=merge patch with a containers list "
    "(it replaces the whole list). Don't patch immutable fields such as a StatefulSet's volumeClaimTemplates; "
    "say what has to be recreated instead."
)


class AgentLimitError(Exception):
    """The agent hit the step limit or the timeout without producing a report. `state` is the last graph
    state reached, so a caller can still see the messages and tool calls up to that point."""

    def __init__(self, message: str, state: dict[str, Any]):
        super().__init__(message)
        self.state = state


def user_prompt(namespace: str) -> str:
    return (
        f"Inspect the '{namespace}' namespace for any failing or unhealthy pods. "
        "Investigate logs and warning events for affected pods, diagnose "
        "the root causes, and provide actionable remediation steps."
    )


def tool_server_env() -> dict[str, str]:
    """The MCP stdio client passes only a minimal env by default, which drops KUBECONFIG and KUBE_SRE_*.
    Pass ours through, minus API keys: the tool server never calls an LLM."""
    return {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}


def token_usage(messages) -> tuple[int, int]:
    usage = [m.usage_metadata for m in messages if isinstance(m, AIMessage) and m.usage_metadata]
    return sum(u["input_tokens"] for u in usage), sum(u["output_tokens"] for u in usage)


def build_agent(tools: list[BaseTool], system_prompt: str | None = SYSTEM_PROMPT, report_schema=TriageReport):
    return create_agent(
        get_chat_model(), tools, system_prompt=system_prompt, response_format=ToolStrategy(report_schema)
    )


async def invoke(agent, prompt: str) -> dict[str, Any]:
    """Run the agent within the configured step and time limits. Returns the final graph state."""
    settings = get_settings()
    state: dict[str, Any] = {"messages": []}

    async def run() -> None:
        nonlocal state
        config = {"recursion_limit": settings.agent_recursion_limit}
        async for update in agent.astream({"messages": [("user", prompt)]}, config=config, stream_mode="values"):
            state = update  # keep the latest, so a limit error can still report how far the agent got

    try:
        await asyncio.wait_for(run(), timeout=settings.agent_timeout_seconds)
    except GraphRecursionError:
        msg = f"Agent stopped after {settings.agent_recursion_limit} steps without a report."
        raise AgentLimitError(msg, state) from None
    except TimeoutError:
        raise AgentLimitError(f"Agent timed out after {settings.agent_timeout_seconds}s.", state) from None
    return state


def mcp_client(args: list[str]) -> MultiServerMCPClient:
    """Client for a tool server started as `python <args>` over stdio, under the name "k8s_read"."""
    return MultiServerMCPClient(
        {"k8s_read": {"command": sys.executable, "args": args, "transport": "stdio", "env": tool_server_env()}}
    )


async def main(namespace: str) -> None:
    # One session for the whole run. Tools from get_tools() start a new server process per call,
    # which reloads the Kubernetes client and the spaCy model every time.
    async with mcp_client(["-m", "kube_sre.tools.k8s_read"]).session("k8s_read") as session:
        await _investigate(await load_mcp_tools(session), namespace)


async def _investigate(tools: list[BaseTool], namespace: str) -> None:
    settings = get_settings()
    print(f"Loaded {len(tools)} tools from MCP server: {[t.name for t in tools]}")
    print(f"Model: {settings.agent_model}")

    prompt = user_prompt(namespace)
    print(f"\n--- Starting SRE Agent Investigation ---\nPrompt: {prompt}\n")
    try:
        response = await invoke(build_agent(tools), prompt)
    except AgentLimitError as e:
        raise SystemExit(str(e)) from None

    report: TriageReport = response["structured_response"]
    print("================ SRE DIAGNOSTIC REPORT ================")
    print(report.model_dump_json(indent=2))

    messages = response["messages"]
    received = received_evidence_ids(messages, {t.name for t in tools})
    cited = sum(len(i.evidence) for i in report.incidents)
    invalid = invalid_citations(report, received)
    if invalid:
        print(f"\nWARNING: {len(invalid)} of {cited} citations match no tool result: {invalid}")
    else:
        print(f"\nCitations: all {cited} match tool results.")
    tokens_in, tokens_out = token_usage(messages)
    print(f"Tokens: {tokens_in} input, {tokens_out} output")


if __name__ == "__main__":
    load_dotenv()  # OpenAI and LangSmith settings from .env; exported vars take precedence
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="default")
    asyncio.run(main(parser.parse_args().namespace))
