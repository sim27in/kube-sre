"""v1 single ReAct agent: the v0 agent on the fixed, typed tools. Phase 2 compares it against baselines/v0."""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_mcp_adapters.client import MultiServerMCPClient

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


async def main(namespace: str) -> None:
    settings = get_settings()
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
    tools = await mcp_client.get_tools()
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

    response = await agent.ainvoke({"messages": [("user", prompt)]})

    report: TriageReport = response["structured_response"]
    print("================ SRE DIAGNOSTIC REPORT ================")
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    load_dotenv()  # LangSmith and AWS settings from .env; exported vars take precedence
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="default")
    asyncio.run(main(parser.parse_args().namespace))
