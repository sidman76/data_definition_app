"""
A2A Market Data Agent — Port 8001
===================================
Implements the A2A (Agent-to-Agent) protocol to serve real-time stock market
data. Fetches live data from Yahoo Finance via yfinance, then uses Claude to
add analytical commentary (volume anomalies, 52-week position, P/E context).

A2A endpoints:
  GET  /.well-known/agent.json  — agent card (discovery)
  POST /                         — JSON-RPC 2.0 task handler

Run standalone: python a2a_market_data_agent.py
"""

import asyncio
import os
import re
import uuid
import anthropic
import uvicorn
import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic

load_dotenv()

os.environ.setdefault("LANGSMITH_PROJECT", "a2a-agents")

PORT = 8001
BASE_URL = f"http://localhost:{PORT}"

AGENT_CARD = {
    "name": "Market Data Agent",
    "description": (
        "Provides real-time stock market data with AI-powered analytical commentary. "
        "Fetches live data from Yahoo Finance and uses Claude to interpret volume "
        "anomalies, 52-week positioning, P/E context, and other key signals."
    ),
    "url": BASE_URL,
    "version": "2.0.0",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "skills": [
        {
            "id": "get_stock_data",
            "name": "Get Stock Data",
            "description": (
                "Fetch real-time price, volume, market cap, P/E, and 52-week range "
                "for a stock ticker, with Claude-generated analytical commentary."
            ),
            "inputModes": ["text"],
            "outputModes": ["text"],
            "examples": [
                "Get market data for AAPL",
                "What is the current price and P/E ratio of TSLA?",
                "Show me MSFT stock fundamentals",
            ],
        }
    ],
}

app = FastAPI(title="Market Data Agent")
claude = wrap_anthropic(anthropic.AsyncAnthropic())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_ticker(query: str) -> str:
    match = re.search(r'\bfor\s+([A-Z]{1,5})\b', query.upper())
    if match:
        return match.group(1)
    words = query.upper().split()
    return words[-1] if words else "AAPL"


def _fmt_volume(v: int) -> str:
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _fmt_cap(c: int) -> str:
    if c >= 1_000_000_000_000:
        return f"${c / 1_000_000_000_000:.2f}T"
    if c >= 1_000_000_000:
        return f"${c / 1_000_000_000:.2f}B"
    return f"${c / 1_000_000:.2f}M"


def fetch_raw_data(ticker: str) -> tuple[str, dict]:
    """
    Fetch real-time data from Yahoo Finance.
    Returns (formatted_string, derived_signals) where derived_signals gives
    Claude the pre-computed context it needs to reason about the stock.
    """
    t = yf.Ticker(ticker)
    info = t.info

    company = info.get("longName") or info.get("shortName", ticker)
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or price
    change = price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0
    sign = "+" if change >= 0 else ""

    volume = info.get("volume") or info.get("regularMarketVolume") or 0
    avg_volume = info.get("averageVolume") or 0
    market_cap = info.get("marketCap") or 0
    pe = info.get("trailingPE") or info.get("forwardPE")
    forward_pe = info.get("forwardPE")
    eps = info.get("trailingEps")
    high_52 = info.get("fiftyTwoWeekHigh")
    low_52 = info.get("fiftyTwoWeekLow")
    div_yield = info.get("dividendYield") or 0.0
    sector = info.get("sector", "N/A")
    beta = info.get("beta")
    short_ratio = info.get("shortRatio")
    summary = info.get("longBusinessSummary", "")
    if summary:
        summary = summary.split(".")[0] + "."

    raw = (
        f"Ticker: {ticker}\n"
        f"Company: {company}\n"
        f"Price: ${price:.2f}\n"
        f"Change: {sign}${change:.2f} ({sign}{change_pct:.2f}%)\n"
        f"Volume: {_fmt_volume(volume)}\n"
        f"Avg Volume (30d): {_fmt_volume(avg_volume)}\n"
        f"Market Cap: {_fmt_cap(market_cap)}\n"
        f"P/E Ratio (TTM): {f'{pe:.2f}' if pe else 'N/A'}\n"
        f"P/E Ratio (Forward): {f'{forward_pe:.2f}' if forward_pe else 'N/A'}\n"
        f"EPS (TTM): {f'${eps:.2f}' if eps else 'N/A'}\n"
        f"52-Week High: {f'${high_52:.2f}' if high_52 else 'N/A'}\n"
        f"52-Week Low: {f'${low_52:.2f}' if low_52 else 'N/A'}\n"
        f"Dividend Yield: {div_yield:.2f}%\n"
        f"Beta: {f'{beta:.2f}' if beta else 'N/A'}\n"
        f"Short Ratio: {f'{short_ratio:.1f}' if short_ratio else 'N/A'}\n"
        f"Sector: {sector}\n"
        f"Summary: {summary}"
    )

    # Pre-compute derived signals for Claude to reason about
    signals = {
        "volume_vs_avg_pct": round((volume / avg_volume - 1) * 100, 1) if avg_volume else None,
        "pct_from_52w_high": round((price / high_52 - 1) * 100, 1) if high_52 else None,
        "pct_from_52w_low": round((price / low_52 - 1) * 100, 1) if low_52 else None,
        "range_position_pct": round((price - low_52) / (high_52 - low_52) * 100, 1) if (high_52 and low_52 and high_52 != low_52) else None,
        "pe": pe,
        "beta": beta,
        "sector": sector,
    }

    return raw, signals


async def analyze_with_claude(ticker: str, raw_data: str, signals: dict) -> str:
    """Ask Claude to interpret the real data and add analytical commentary."""
    sig_lines = []
    if signals["volume_vs_avg_pct"] is not None:
        direction = "above" if signals["volume_vs_avg_pct"] > 0 else "below"
        sig_lines.append(f"- Today's volume is {abs(signals['volume_vs_avg_pct'])}% {direction} the 30-day average")
    if signals["range_position_pct"] is not None:
        sig_lines.append(f"- Price is at {signals['range_position_pct']}% of its 52-week range (0%=low, 100%=high)")
    if signals["pct_from_52w_high"] is not None:
        sig_lines.append(f"- Price is {abs(signals['pct_from_52w_high'])}% below the 52-week high")
    if signals["beta"] is not None:
        sig_lines.append(f"- Beta: {signals['beta']} (market volatility reference = 1.0)")

    signal_block = "\n".join(sig_lines) if sig_lines else "No derived signals available."

    response = await claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        system=(
            "You are a market data analyst. Given real-time stock data and pre-computed "
            "signals, provide a concise analytical commentary in 4-5 bullet points. Focus on:\n"
            "- Volume anomalies (unusually high/low vs average)\n"
            "- Price positioning within the 52-week range\n"
            "- P/E valuation context for the sector\n"
            "- Volatility (beta) and any notable signals\n"
            "Be factual and specific. Do not give buy/sell advice here — that is for the advisor."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Stock: {ticker}\n\n"
                f"Real-time data:\n{raw_data}\n\n"
                f"Pre-computed signals:\n{signal_block}\n\n"
                f"Provide analytical commentary on these data points."
            ),
        }],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# A2A endpoints
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json")
async def agent_card():
    return JSONResponse(AGENT_CARD)


@app.post("/")
@traceable(name="market-data-agent", run_type="chain")
async def handle_task(request: Request):
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
            },
            status_code=200,
        )

    task_id = params.get("id", str(uuid.uuid4()))
    parts = params.get("message", {}).get("parts", [])
    query = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")

    print(f"  [Market Data Agent] Task {task_id[:8]}... received: {query!r}")

    ticker = _extract_ticker(query)
    print(f"  [Market Data Agent] Fetching real-time data for {ticker} via yfinance...")

    raw_data, signals = await asyncio.to_thread(fetch_raw_data, ticker)

    print(f"  [Market Data Agent] Analyzing data with Claude...")
    commentary = await analyze_with_claude(ticker, raw_data, signals)

    market_data = f"{raw_data}\n\n### Analyst Commentary\n{commentary}"
    print(f"  [Market Data Agent] Task {task_id[:8]}... completed ({len(market_data)} chars)")

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": task_id,
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "name": "market_data",
                        "description": f"Real-time market data with analytical commentary for {ticker}",
                        "index": 0,
                        "parts": [{"type": "text", "text": market_data}],
                    }
                ],
            },
        }
    )


if __name__ == "__main__":
    print(f"Market Data Agent starting on {BASE_URL}")
    print(f"Agent card: {BASE_URL}/.well-known/agent.json\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
