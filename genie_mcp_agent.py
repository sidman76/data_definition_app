"""
Databricks Genie MCP Agent — queries your Databricks data using natural language
via Genie's remote MCP server and Claude.

Setup:
    pip install -r requirements-genie-agent.txt

Required environment variables:
    ANTHROPIC_API_KEY        — Anthropic API key
    DATABRICKS_TOKEN         — Databricks personal access token

Optional:
    DATABRICKS_GENIE_MCP_URL — Override the Genie MCP endpoint URL
                               (default: https://dbc-f34bb270-dd82.cloud.databricks.com/api/2.0/mcp/genie)
    LANGSMITH_API_KEY         — Enable LangSmith tracing (set LANGSMITH_TRACING=true too)
    LANGSMITH_PROJECT         — LangSmith project name (default: genie-mcp-agent)
    LANGSMITH_TRACING         — Set to "true" to enable tracing

Usage:
    python genie_mcp_agent.py
    python genie_mcp_agent.py "What are the top 5 products by revenue last quarter?"
"""

import asyncio
import os
import sys

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

os.environ.setdefault("LANGSMITH_PROJECT", "genie-mcp-agent")

GENIE_MCP_URL = os.environ.get(
    "DATABRICKS_GENIE_MCP_URL",
    "https://dbc-f34bb270-dd82.cloud.databricks.com/api/2.0/mcp/genie",
)
DATABRICKS_TOKEN = os.environ["DATABRICKS_TOKEN"]

MODEL = "claude-sonnet-4-6"
SYSTEM_PROMPT = """You are a data analyst assistant connected to Databricks Genie.
Genie is a natural-language data interface that can query tables and return results.
When answering questions:
- Use the Genie MCP tools to ask questions and retrieve data
- If a result set is large, summarize the key findings rather than listing every row
- Present numbers clearly (format large figures, include units where relevant)
- If a question is ambiguous, make a reasonable assumption and state it explicitly"""


@traceable(name="genie-agent", run_type="chain")
async def run_agent(user_question: str, mcp_client: ClientSession) -> str:
    """Run one question through the Claude + Genie agent loop."""
    client = wrap_anthropic(AsyncAnthropic())

    tools_result = await mcp_client.list_tools()
    tools = [async_mcp_tool(t, mcp_client) for t in tools_result.tools]

    print(f"Genie MCP tools: {[t.name for t in tools_result.tools]}")
    print(f"\nQuestion: {user_question}\n")

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_question}],
        tools=tools,
    )

    final_text = ""
    async for message in runner:
        for block in message.content:
            if block.type == "text" and block.text:
                print(block.text, end="", flush=True)
                final_text = block.text

    return final_text


async def interactive_loop():
    """Connect to Genie once, then answer questions in a REPL."""
    print(f"Connecting to Databricks Genie MCP server at {GENIE_MCP_URL} ...")

    headers = {"Authorization": f"Bearer {DATABRICKS_TOKEN}"}

    async with streamablehttp_client(GENIE_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            print("Connected. Type your question (or 'quit' to exit).\n")

            while True:
                try:
                    question = input("You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nBye!")
                    break

                if not question or question.lower() in {"quit", "exit", "q"}:
                    print("Bye!")
                    break

                print("\nClaude: ", end="", flush=True)
                await run_agent(question, mcp_client)
                print("\n")


async def single_query(question: str):
    """Run a single question and exit — useful for scripting."""
    headers = {"Authorization": f"Bearer {DATABRICKS_TOKEN}"}

    async with streamablehttp_client(GENIE_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            await run_agent(question, mcp_client)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Non-interactive: python genie_mcp_agent.py "show me sales by region"
        asyncio.run(single_query(" ".join(sys.argv[1:])))
    else:
        # Interactive REPL
        asyncio.run(interactive_loop())
