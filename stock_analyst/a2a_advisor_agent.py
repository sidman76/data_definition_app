"""
A2A Investment Advisor Agent — Port 8002
==========================================
Provides investment advice by orchestrating a call to the Market Data Agent
via the A2A protocol, then analyzing the returned data with Claude.

This agent demonstrates the core A2A pattern:
  1. Discover a peer agent via its /.well-known/agent.json card
  2. Send it a task using JSON-RPC 2.0 (tasks/send)
  3. Extract artifacts from the response
  4. Use those results to produce its own output

A2A endpoints:
  GET  /.well-known/agent.json  — agent card (discovery)
  POST /                         — JSON-RPC 2.0 task handler

Run standalone: python a2a_advisor_agent.py
"""

import os
import uuid
import httpx
import anthropic
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic

load_dotenv()

os.environ.setdefault("LANGSMITH_PROJECT", "a2a-agents")

PORT = 8002
BASE_URL = f"http://localhost:{PORT}"
MARKET_DATA_AGENT_URL = "http://localhost:8001"

AGENT_CARD = {
    "name": "Investment Advisor Agent",
    "description": (
        "Provides AI-powered investment analysis and recommendations by "
        "orchestrating real-time market data from the Market Data Agent via A2A."
    ),
    "url": BASE_URL,
    "version": "1.0.0",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "skills": [
        {
            "id": "investment_advice",
            "name": "Investment Advice",
            "description": (
                "Analyze a stock and return a structured BUY / HOLD / SELL recommendation "
                "with investment thesis, key risks, valuation assessment, and outlook."
            ),
            "inputModes": ["text"],
            "outputModes": ["text"],
            "examples": [
                "Should I invest in AAPL?",
                "Analyze TSLA for long-term investment",
                "Give me an investment recommendation for NVDA",
            ],
        }
    ],
}

app = FastAPI(title="Investment Advisor Agent")
claude = wrap_anthropic(anthropic.AsyncAnthropic())


# ---------------------------------------------------------------------------
# A2A call helper — calls Market Data Agent following the A2A protocol
# ---------------------------------------------------------------------------

@traceable(name="fetch-market-data-via-a2a", run_type="tool")
async def fetch_market_data_via_a2a(ticker: str) -> tuple[str, str]:
    """
    Call the Market Data Agent using the A2A protocol.

    Protocol steps:
      1. GET /.well-known/agent.json  — discover the agent
      2. POST {agent.url}             — send a tasks/send JSON-RPC 2.0 request
      3. Extract text from returned artifacts

    Returns (market_data_text, task_id).
    """
    async with httpx.AsyncClient(timeout=30.0) as http:
        # ── Step 1: Discover the Market Data Agent ────────────────────────
        print(f"\n  [Advisor → Market Data] Fetching agent card...")
        card_response = await http.get(f"{MARKET_DATA_AGENT_URL}/.well-known/agent.json")
        card_response.raise_for_status()
        agent_card = card_response.json()

        print(f"  [Advisor → Market Data] Discovered agent: '{agent_card['name']}'")
        print(f"  [Advisor → Market Data] Skills: {[s['id'] for s in agent_card['skills']]}")
        print(f"  [Advisor → Market Data] Endpoint: {agent_card['url']}")

        # ── Step 2: Send A2A task (JSON-RPC 2.0) ─────────────────────────
        task_id = str(uuid.uuid4())
        a2a_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/send",
            "params": {
                "id": task_id,
                "message": {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": f"Get market data for {ticker}"}
                    ],
                },
            },
        }

        print(f"\n  [Advisor → Market Data] Sending A2A request:")
        print(f"    method  : tasks/send")
        print(f"    task_id : {task_id[:8]}...")
        print(f"    message : Get market data for {ticker}")

        resp = await http.post(agent_card["url"], json=a2a_request)
        resp.raise_for_status()
        a2a_response = resp.json()

        # ── Step 3: Extract artifacts ─────────────────────────────────────
        result = a2a_response.get("result", {})
        state = result.get("status", {}).get("state", "unknown")

        print(f"\n  [Market Data → Advisor] A2A response received:")
        print(f"    task_id : {result.get('id', '')[:8]}...")
        print(f"    status  : {state}")

        market_data = ""
        for artifact in result.get("artifacts", []):
            print(f"    artifact: {artifact.get('name', 'unnamed')}")
            for part in artifact.get("parts", []):
                if part.get("type") == "text":
                    market_data += part["text"]

        return market_data, task_id


# ---------------------------------------------------------------------------
# A2A endpoints
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json")
async def agent_card():
    """A2A discovery endpoint — returns the agent card."""
    return JSONResponse(AGENT_CARD)


@app.post("/")
@traceable(name="investment-advisor-agent", run_type="chain")
async def handle_task(request: Request):
    """A2A task endpoint — orchestrates market data fetch + Claude analysis."""
    body = await request.json()
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method != "tasks/send":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )

    task_id = params.get("id", str(uuid.uuid4()))
    parts = params.get("message", {}).get("parts", [])
    user_query = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")

    print(f"\n[Investment Advisor Agent] Task {task_id[:8]}... received: {user_query!r}")

    # Extract ticker symbol from free-text query
    ticker_resp = await claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=10,
        system=(
            "Extract the stock ticker symbol from the text. "
            "Reply with ONLY the ticker (e.g. AAPL). Default to AAPL if unclear."
        ),
        messages=[{"role": "user", "content": user_query}],
    )
    ticker = ticker_resp.content[0].text.strip().upper().rstrip(".")
    print(f"[Investment Advisor Agent] Ticker identified: {ticker}")

    # ── A2A CALL ─────────────────────────────────────────────────────────────
    print(f"[Investment Advisor Agent] Calling Market Data Agent via A2A protocol...")
    market_data, data_task_id = await fetch_market_data_via_a2a(ticker)
    # ─────────────────────────────────────────────────────────────────────────

    print(f"\n[Investment Advisor Agent] Generating investment analysis with Claude...")

    analysis_resp = await claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1200,
        system=(
            "You are a senior investment analyst. Based on the provided market data, "
            "produce a structured report in this exact format:\n\n"
            "## Investment Analysis: {TICKER}\n\n"
            "**Recommendation**: BUY / HOLD / SELL\n"
            "**Confidence**: High / Medium / Low\n\n"
            "### Investment Thesis\n"
            "{2-3 sentence thesis}\n\n"
            "### Key Strengths\n"
            "- {strength 1}\n- {strength 2}\n- {strength 3}\n\n"
            "### Key Risks\n"
            "- {risk 1}\n- {risk 2}\n- {risk 3}\n\n"
            "### Valuation\n"
            "{Brief valuation commentary based on P/E, market cap}\n\n"
            "### Outlook\n"
            "- **Short-term (3-6 months)**: {outlook}\n"
            "- **Long-term (1-3 years)**: {outlook}\n\n"
            "### Disclaimer\n"
            "This analysis is for educational purposes only and does not constitute financial advice."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Analyze {ticker} and provide an investment recommendation "
                    f"based on this market data:\n\n{market_data}"
                ),
            }
        ],
    )

    analysis = analysis_resp.content[0].text
    full_report = (
        f"### Market Data (retrieved via A2A from Market Data Agent)\n\n"
        f"{market_data}\n\n"
        f"---\n\n"
        f"{analysis}"
    )

    print(f"[Investment Advisor Agent] Task {task_id[:8]}... completed")

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": task_id,
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "name": "investment_report",
                        "description": f"Investment analysis for {ticker}",
                        "index": 0,
                        "parts": [{"type": "text", "text": full_report}],
                    }
                ],
            },
        }
    )


if __name__ == "__main__":
    print(f"Investment Advisor Agent starting on {BASE_URL}")
    print(f"Agent card: {BASE_URL}/.well-known/agent.json\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
