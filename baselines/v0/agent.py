import asyncio
import sys

from dotenv import load_dotenv

# Updated Import: Use create_agent from langchain.agents
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from schemas import TriageReport


async def main():
    # 1. Connect to FastMCP server over stdio
    mcp_client = MultiServerMCPClient(
        {
            "k8s_tools": {
                "command": sys.executable,
                "args": ["mcp_server.py"],
                "transport": "stdio",
            }
        }
    )

    # 2. Discover MCP tools
    tools = await mcp_client.get_tools()
    print(
        f"Loaded {len(tools)} tools from MCP server: {[t.name for t in tools]}"
    )

    # 3. Initialize the model. Same model as v1, so evals compare the tools, not the models.
    llm = ChatOpenAI(model="gpt-5-mini")

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
    load_dotenv()  # finds the repo-root .env by walking up from this file; exported vars take precedence
    asyncio.run(main())