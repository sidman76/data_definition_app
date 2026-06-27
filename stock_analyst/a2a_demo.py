"""
A2A Protocol Demo — Full End-to-End Walkthrough
=================================================
Starts both agents, then acts as a client to drive the full A2A flow.

Architecture:
                          ┌─────────────────────────┐
  You (client)            │  Investment Advisor Agent │          ┌──────────────────────┐
       │                  │       Port 8002           │          │  Market Data Agent   │
       │  A2A             │                           │  A2A     │      Port 8001        │
       │  tasks/send ────▶│  1. Receives task         │          │                      │
       │                  │  2. Discovers peer agent ─┼─────────▶│  /.well-known/       │
       │                  │  3. Sends A2A task ───────┼─────────▶│  agent.json          │
       │                  │  4. Extracts artifacts ◀──┼──────────┤  POST /              │
       │◀─ artifacts ─────┤  5. Generates advice      │          └──────────────────────┘
       │                  └─────────────────────────-─┘
       │
  Displays report

Usage:
  python a2a_demo.py [TICKER]
  python a2a_demo.py AAPL
  python a2a_demo.py NVDA
"""

import sys
import time
import uuid
import signal
import asyncio
import subprocess
import httpx

MARKET_DATA_URL = "http://localhost:8001"
ADVISOR_URL = "http://localhost:8002"
BAR = "─" * 68


async def wait_for_agent(url: str, name: str, retries: int = 40) -> bool:
    """Poll agent card endpoint until the server is ready."""
    async with httpx.AsyncClient() as client:
        for i in range(retries):
            try:
                r = await client.get(f"{url}/.well-known/agent.json", timeout=2.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    print(f"ERROR: {name} at {url} did not become ready in time.")
    return False


async def run_demo(ticker: str) -> None:
    print(f"\n{'═' * 68}")
    print("  A2A (Agent-to-Agent) Protocol Demo")
    print(f"{'═' * 68}\n")

    print("Architecture:\n")
    print("  Client")
    print("    │")
    print("    │── A2A tasks/send ──────────────────────────────────────────▶")
    print("    │                               Investment Advisor Agent (8002)")
    print("    │                                         │")
    print("    │              A2A GET /.well-known/agent.json ──────────────▶")
    print("    │                                         │  Market Data (8001)")
    print("    │              A2A tasks/send ────────────┼──────────────────▶")
    print("    │                                         │         │")
    print("    │◀── artifacts ───────────────────────────┘◀────────┘")
    print()

    # ─── STEP 1: Agent discovery ──────────────────────────────────────────────
    print(BAR)
    print("STEP 1  Client discovers the Investment Advisor Agent")
    print(BAR)

    async with httpx.AsyncClient(timeout=60.0) as client:
        card_resp = await client.get(f"{ADVISOR_URL}/.well-known/agent.json")
        advisor_card = card_resp.json()

        print(f"\n  GET {ADVISOR_URL}/.well-known/agent.json")
        print(f"\n  Agent Card received:")
        print(f"    name        : {advisor_card['name']}")
        print(f"    description : {advisor_card['description'][:72]}...")
        print(f"    url         : {advisor_card['url']}")
        print(f"    skills      : {[s['id'] for s in advisor_card['skills']]}")

        # ─── STEP 2: Send task to Advisor Agent ───────────────────────────────
        print(f"\n{BAR}")
        print("STEP 2  Client sends a task to the Investment Advisor Agent")
        print(BAR)

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
                        {"type": "text", "text": f"Should I invest in {ticker}?"}
                    ],
                },
            },
        }

        print(f"\n  POST {advisor_card['url']}")
        print(f"  JSON-RPC method : tasks/send")
        print(f"  task_id         : {task_id[:8]}...")
        print(f"  message         : Should I invest in {ticker}?")

        # ─── STEP 3: Internal A2A call (logged by Advisor Agent) ──────────────
        print(f"\n{BAR}")
        print("STEP 3  Advisor Agent calls Market Data Agent via A2A (see below)")
        print(BAR)

        response = await client.post(advisor_card["url"], json=a2a_request)
        result = response.json()

        # ─── STEP 4: Display final results ────────────────────────────────────
        print(f"\n{BAR}")
        print("STEP 4  Final artifacts returned to client")
        print(BAR)

        final_result = result.get("result", {})
        print(f"\n  task_id : {final_result.get('id', '')[:8]}...")
        print(f"  status  : {final_result.get('status', {}).get('state', 'unknown')}")

        for artifact in final_result.get("artifacts", []):
            print(f"\n  ── Artifact: {artifact.get('name', 'result')} ──\n")
            for part in artifact.get("parts", []):
                if part.get("type") == "text":
                    print(part["text"])

    print(f"\n{'═' * 68}")
    print("  Demo complete.  A2A flow:")
    print("  Client → Advisor Agent  →[A2A]→  Market Data Agent")
    print(f"{'═' * 68}\n")


def main() -> None:
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"

    procs: list[subprocess.Popen] = []

    def cleanup(*_):
        print("\n[Demo] Shutting down agents...")
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Start both agent servers as subprocesses.
    # Stdout/stderr are NOT redirected so startup errors and agent logs are
    # visible directly in the terminal — essential for debugging.
    print("[Demo] Starting Market Data Agent (port 8001)...")
    procs.append(
        subprocess.Popen([sys.executable, "a2a_market_data_agent.py"])
    )

    print("[Demo] Starting Investment Advisor Agent (port 8002)...")
    procs.append(
        subprocess.Popen([sys.executable, "a2a_advisor_agent.py"])
    )

    # Give processes a moment to either crash or begin binding their ports.
    time.sleep(1.5)
    for p, name in zip(procs, ["Market Data Agent", "Investment Advisor Agent"]):
        if p.returncode is not None:
            print(f"[Demo] ERROR: {name} exited immediately (code {p.returncode}).")
            print("[Demo] Check that dependencies are installed:")
            print("       pip install -r requirements-a2a.txt")
            cleanup()

    # Poll both health endpoints until servers accept connections.
    print("[Demo] Waiting for agents to be ready...")

    async def wait_for_both():
        ok1 = await wait_for_agent(MARKET_DATA_URL, "Market Data Agent")
        ok2 = await wait_for_agent(ADVISOR_URL, "Investment Advisor Agent")
        return ok1 and ok2

    ready = asyncio.run(wait_for_both())
    if not ready:
        cleanup()

    print("[Demo] Both agents are ready.\n")

    # Run the demo
    try:
        asyncio.run(run_demo(ticker))
    finally:
        cleanup()


if __name__ == "__main__":
    main()
