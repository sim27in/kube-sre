"""v1 single ReAct agent: the v0 agent on the fixed, typed tools. Phase 2 compares it against baselines/v0."""

import argparse
import asyncio
import os
import sys

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
    "evidence_id of the tool result it came from. Report on the owning workload (use get_owner), not the pod."
)


def _tool_server_env() -> dict[str, str]:
    """The MCP stdio client passes only a minimal env by default, which drops KUBECONFIG and KUBE_SRE_*.
    Pass ours through, minus API keys: the tool server never calls an LLM."""
    return {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}


def _token_usage(messages) -> tuple[int, int]:
    usage = [m.usage_metadata for m in messages if isinstance(m, AIMessage) and m.usage_metadata]
    return sum(u["input_tokens"] for u in usage), sum(u["output_tokens"] for u in usage)


async def main(namespace: str) -> None:
    mcp_client = MultiServerMCPClient(
        {
            "k8s_read": {
                "command": sys.executable,
                "args": ["-m", "kube_sre.tools.k8s_read"],
                "transport": "stdio",
                "env": _tool_server_env(),
            }
        }
    )
    # One session for the whole run. Tools from get_tools() start a new server process per call,
    # which reloads the Kubernetes client and the spaCy model every time.
    async with mcp_client.session("k8s_read") as session:
        await _investigate(await load_mcp_tools(session), namespace)


async def _investigate(tools: list[BaseTool], namespace: str) -> None:
    settings = get_settings()
    print(f"Loaded {len(tools)} tools from MCP server: {[t.name for t in tools]}")
    print(f"Model: {settings.llm_provider}/{settings.agent_model}")

    agent = create_agent(
        get_chat_model(), tools, system_prompt=SYSTEM_PROMPT, response_format=ToolStrategy(TriageReport)
    )

    prompt = (
        f"Inspect the '{namespace}' namespace for any failing or unhealthy pods. "
        "Investigate logs and warning events for affected pods, diagnose "
        "the root causes, and provide actionable remediation steps."
    )
    print(f"\n--- Starting SRE Agent Investigation ---\nPrompt: {prompt}\n")

    try:
        response = await asyncio.wait_for(
            agent.ainvoke({"messages": [("user", prompt)]}, config={"recursion_limit": settings.agent_recursion_limit}),
            timeout=settings.agent_timeout_seconds,
        )
    except GraphRecursionError:
        raise SystemExit(f"Agent stopped after {settings.agent_recursion_limit} steps without a report.") from None
    except TimeoutError:
        raise SystemExit(f"Agent timed out after {settings.agent_timeout_seconds}s.") from None

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
    tokens_in, tokens_out = _token_usage(messages)
    print(f"Tokens: {tokens_in} input, {tokens_out} output")


if __name__ == "__main__":
    load_dotenv()  # LangSmith and AWS settings from .env; exported vars take precedence
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="default")
    asyncio.run(main(parser.parse_args().namespace))
