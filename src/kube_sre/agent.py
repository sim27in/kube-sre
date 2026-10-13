import asyncio
import sys

# Updated Import: Use create_agent from langchain.agents
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_aws import ChatBedrockConverse
from langchain_mcp_adapters.client import MultiServerMCPClient

from kube_sre.schemas import TriageReport


async def main():
    # 1. Connect to FastMCP server over stdio
    mcp_client = MultiServerMCPClient(
        {
            "k8s_tools": {
                "command": sys.executable,
                "args": ["-m", "kube_sre.tools.k8s_read"],
                "transport": "stdio",
            }
        }
    )

    # 2. Discover MCP tools
    tools = await mcp_client.get_tools()
    print(
        f"Loaded {len(tools)} tools from MCP server: {[t.name for t in tools]}"
    )

    # 3. Initialize AWS Bedrock Model
    llm = ChatBedrockConverse(
        model="amazon.nova-lite-v1:0",
        region_name="us-east-1",
    )

    # 4. Create Agent using create_agent; final answer must match TriageReport
    agent = create_agent(llm, tools, response_format=ToolStrategy(TriageReport))

    # 5. Run SRE Investigation Query
    prompt = (
        "Inspect the 'default' namespace for any failing or unhealthy pods. "
        "Investigate logs and warning events for affected pods, diagnose "
        "the root causes, and provide actionable remediation steps."
    )

    print(f"\n--- Starting SRE Agent Investigation ---\nPrompt: {prompt}\n")

    response = await agent.ainvoke({"messages": [("user", prompt)]})

    # Output final diagnostic report
    report: TriageReport = response["structured_response"]
    print("================ SRE DIAGNOSTIC REPORT ================")
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())