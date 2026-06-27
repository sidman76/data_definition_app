"""
Stock Analysis Web UI — Port 8000
===================================
Starts both A2A agents as subprocesses, then serves a web interface where
users enter a ticker and receive real-time market data + AI investment analysis.

Usage:
    python stock_webapp.py

Requires:
    pip install -r requirements-a2a.txt
    ANTHROPIC_API_KEY in .env
"""

import asyncio
import sys
import uuid
import subprocess
import httpx
import uvicorn
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

MARKET_DATA_URL = "http://localhost:8001"
ADVISOR_URL = "http://localhost:8002"
PORT = 8000

_procs: list[subprocess.Popen] = []


async def _wait_for_agent(url: str, retries: int = 60) -> bool:
    async with httpx.AsyncClient() as client:
        for _ in range(retries):
            try:
                r = await client.get(f"{url}/.well-known/agent.json", timeout=2.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[WebApp] Starting Market Data Agent (port 8001)...")
    _procs.append(subprocess.Popen([sys.executable, "a2a_market_data_agent.py"]))

    print("[WebApp] Starting Investment Advisor Agent (port 8002)...")
    _procs.append(subprocess.Popen([sys.executable, "a2a_advisor_agent.py"]))

    await asyncio.sleep(1.5)

    ok1 = await _wait_for_agent(MARKET_DATA_URL)
    ok2 = await _wait_for_agent(ADVISOR_URL)

    if ok1 and ok2:
        print(f"[WebApp] Both agents ready → http://localhost:{PORT}")
    else:
        print("[WebApp] WARNING: one or more agents failed to start")

    yield

    print("[WebApp] Shutting down agents...")
    for p in _procs:
        p.terminate()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Analysis Agent</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0a0e1a;
    --surface:  #111827;
    --card:     #1f2937;
    --border:   #374151;
    --text:     #f3f4f6;
    --muted:    #9ca3af;
    --accent:   #3b82f6;
    --green:    #10b981;
    --red:      #ef4444;
    --yellow:   #f59e0b;
    --radius:   12px;
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
  }

  /* ── Header ── */
  header {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    border-bottom: 1px solid var(--border);
    padding: 28px 24px 24px;
    text-align: center;
  }
  header h1 { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em; }
  header h1 span { color: var(--accent); }
  header p  { color: var(--muted); margin-top: 6px; font-size: 0.95rem; }

  /* ── Main container ── */
  .container { max-width: 900px; margin: 0 auto; padding: 32px 16px 64px; }

  /* ── Search ── */
  .search-box {
    display: flex; gap: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px;
    margin-bottom: 14px;
  }
  .search-box input {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: var(--text);
    font-size: 1.1rem;
    padding: 6px 10px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .search-box input::placeholder { text-transform: none; color: var(--muted); }
  .search-box button {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 10px 28px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .search-box button:hover  { opacity: 0.85; }
  .search-box button:active { opacity: 0.7; }

  .quick-picks { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .quick-picks span { color: var(--muted); font-size: 0.85rem; }
  .quick-picks button {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 4px 12px;
    font-size: 0.85rem;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .quick-picks button:hover { border-color: var(--accent); background: var(--surface); }

  /* ── Loading ── */
  .loading {
    display: flex; flex-direction: column; align-items: center; gap: 20px;
    padding: 60px 20px;
  }
  .spinner {
    width: 44px; height: 44px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading p { color: var(--muted); font-size: 0.95rem; }

  /* ── Error ── */
  .error-box {
    background: rgba(239,68,68,0.1);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: var(--radius);
    padding: 20px 24px;
    color: var(--red);
    margin-top: 24px;
  }

  /* ── Results hero ── */
  .hero {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px 28px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 20px;
    margin-top: 28px;
    flex-wrap: wrap;
  }
  .hero-left h2 { font-size: 2rem; font-weight: 700; }
  .hero-left .company { color: var(--muted); font-size: 1rem; margin-top: 2px; }
  .hero-left .price-row { display: flex; align-items: baseline; gap: 12px; margin-top: 10px; }
  .hero-left .price { font-size: 2.2rem; font-weight: 700; }
  .hero-left .change { font-size: 1rem; font-weight: 500; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }

  .rec-badge {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    background: var(--card);
    border-radius: var(--radius);
    padding: 16px 28px;
    border: 2px solid var(--border);
    min-width: 140px;
    text-align: center;
  }
  .rec-badge .rec-label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
  .rec-badge .rec-value { font-size: 1.8rem; font-weight: 800; letter-spacing: 0.04em; }
  .rec-badge .conf { font-size: 0.8rem; color: var(--muted); }
  .rec-BUY  { border-color: var(--green); }
  .rec-BUY  .rec-value { color: var(--green); }
  .rec-SELL { border-color: var(--red); }
  .rec-SELL .rec-value { color: var(--red); }
  .rec-HOLD { border-color: var(--yellow); }
  .rec-HOLD .rec-value { color: var(--yellow); }

  /* ── Metrics grid ── */
  .section-title {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin: 28px 0 12px;
  }
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 10px;
  }
  .metric-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .metric-card .m-label { font-size: 0.75rem; color: var(--muted); margin-bottom: 4px; }
  .metric-card .m-value { font-size: 1.05rem; font-weight: 600; }

  /* ── Commentary / Analysis cards ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 22px 24px;
    margin-top: 10px;
  }

  /* markdown styles inside .card */
  .card h2 { font-size: 1.2rem; margin-bottom: 12px; color: var(--text); }
  .card h3 { font-size: 1rem; font-weight: 600; color: var(--accent); margin: 16px 0 6px; }
  .card p  { color: #d1d5db; margin-bottom: 8px; }
  .card ul, .card ol { padding-left: 20px; margin-bottom: 8px; }
  .card li { color: #d1d5db; margin-bottom: 4px; }
  .card strong { color: var(--text); }
  .card hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
  .card .disclaimer { font-size: 0.8rem; color: var(--muted); margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }

  .hidden { display: none !important; }
</style>
</head>
<body>

<header>
  <h1>📈 Stock <span>Analysis</span> Agent</h1>
  <p>Real-time market data · AI-powered investment analysis via A2A agents</p>
</header>

<div class="container">

  <div class="search-box">
    <input id="ticker-input" type="text" placeholder="Enter ticker (e.g. AAPL)" maxlength="10"
           onkeydown="if(event.key==='Enter') analyze()">
    <button id="analyze-btn" onclick="analyze()">Analyze</button>
  </div>

  <div class="quick-picks">
    <span>Quick picks:</span>
    <button onclick="go('AAPL')">AAPL</button>
    <button onclick="go('MSFT')">MSFT</button>
    <button onclick="go('NVDA')">NVDA</button>
    <button onclick="go('TSLA')">TSLA</button>
    <button onclick="go('AMZN')">AMZN</button>
    <button onclick="go('GOOGL')">GOOGL</button>
    <button onclick="go('META')">META</button>
  </div>

  <div id="loading" class="loading hidden">
    <div class="spinner"></div>
    <p id="loading-msg">Fetching real-time market data...</p>
  </div>

  <div id="error-box" class="error-box hidden">
    <strong>Error:</strong> <span id="error-msg"></span>
  </div>

  <div id="results" class="hidden">
    <div class="hero">
      <div class="hero-left">
        <h2 id="r-ticker"></h2>
        <div class="company" id="r-company"></div>
        <div class="price-row">
          <span class="price" id="r-price"></span>
          <span class="change" id="r-change"></span>
        </div>
      </div>
      <div class="rec-badge" id="r-rec-badge">
        <span class="rec-label">Recommendation</span>
        <span class="rec-value" id="r-rec"></span>
        <span class="conf" id="r-conf"></span>
      </div>
    </div>

    <div class="section-title">Key Metrics</div>
    <div class="metrics-grid" id="r-metrics"></div>

    <div class="section-title">Market Analysis</div>
    <div class="card" id="r-commentary"></div>

    <div class="section-title">Investment Analysis</div>
    <div class="card" id="r-analysis"></div>
  </div>

</div>

<script>
const METRIC_KEYS = [
  'Volume', 'Avg Volume (30d)', 'Market Cap',
  'P/E Ratio (TTM)', 'P/E Ratio (Forward)', 'EPS (TTM)',
  '52-Week High', '52-Week Low', 'Dividend Yield', 'Beta', 'Short Ratio', 'Sector'
];

function go(ticker) {
  document.getElementById('ticker-input').value = ticker;
  analyze();
}

function setLoading(msg) {
  document.getElementById('loading').classList.remove('hidden');
  document.getElementById('loading-msg').textContent = msg;
  document.getElementById('results').classList.add('hidden');
  document.getElementById('error-box').classList.add('hidden');
}

function showError(msg) {
  document.getElementById('loading').classList.add('hidden');
  document.getElementById('error-box').classList.remove('hidden');
  document.getElementById('error-msg').textContent = msg;
}

function parseResponse(text) {
  const sepIdx = text.indexOf('\\n---\\n');
  const marketSection = sepIdx > -1 ? text.slice(0, sepIdx) : text;
  const analysisSection = sepIdx > -1 ? text.slice(sepIdx + 5).trim() : '';

  // Parse key:value metrics
  const metrics = {};
  let inCommentary = false;
  const commentaryLines = [];

  for (const raw of marketSection.split('\\n')) {
    const line = raw.trim();
    if (line.startsWith('### Analyst Commentary')) { inCommentary = true; continue; }
    if (line.startsWith('#')) { inCommentary = false; continue; }
    if (inCommentary) { commentaryLines.push(raw); continue; }
    const ci = line.indexOf(':');
    if (ci > 0) {
      const key = line.slice(0, ci).trim();
      const val = line.slice(ci + 1).trim();
      if (key && val) metrics[key] = val;
    }
  }

  const recMatch  = analysisSection.match(/\\*\\*Recommendation\\*\\*:\\s*(BUY|HOLD|SELL)/i);
  const confMatch = analysisSection.match(/\\*\\*Confidence\\*\\*:\\s*(\\w+)/i);

  // Strip the first two lines (Recommendation / Confidence) from analysis markdown
  // so we don't double-render them
  const cleanAnalysis = analysisSection
    .replace(/\\*\\*Recommendation\\*\\*:.*\\n?/, '')
    .replace(/\\*\\*Confidence\\*\\*:.*\\n?/, '')
    .trim();

  // Separate disclaimer
  const disclaimerMatch = cleanAnalysis.match(/### Disclaimer\\n([\\s\\S]*)$/);
  const bodyWithoutDisclaimer = disclaimerMatch
    ? cleanAnalysis.slice(0, cleanAnalysis.indexOf('### Disclaimer')).trim()
    : cleanAnalysis;
  const disclaimer = disclaimerMatch ? disclaimerMatch[1].trim() : '';

  return {
    ticker:     metrics['Ticker']  || '',
    company:    metrics['Company'] || '',
    price:      metrics['Price']   || '',
    change:     metrics['Change']  || '',
    metrics,
    commentary: commentaryLines.join('\\n').trim(),
    recommendation: recMatch  ? recMatch[1].toUpperCase() : null,
    confidence:     confMatch ? confMatch[1] : null,
    analysisBody:   bodyWithoutDisclaimer,
    disclaimer,
  };
}

function renderMetricCard(label, value) {
  return `<div class="metric-card"><div class="m-label">${label}</div><div class="m-value">${value}</div></div>`;
}

async function analyze() {
  const raw = document.getElementById('ticker-input').value.trim().toUpperCase();
  if (!raw) return;

  const btn = document.getElementById('analyze-btn');
  btn.disabled = true;
  btn.textContent = 'Analyzing…';

  const msgs = [
    'Fetching real-time market data…',
    'Analyzing market signals with AI…',
    'Generating investment recommendation…',
  ];
  let mi = 0;
  setLoading(msgs[mi]);
  const interval = setInterval(() => {
    mi = (mi + 1) % msgs.length;
    document.getElementById('loading-msg').textContent = msgs[mi];
  }, 3000);

  try {
    const resp = await fetch(`/analyze/${encodeURIComponent(raw)}`, { method: 'POST' });
    if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
    const data = await resp.json();
    if (!data.analysis) throw new Error('Empty response from analysis agents');
    renderResults(data.analysis);
  } catch (e) {
    showError(e.message);
  } finally {
    clearInterval(interval);
    btn.disabled = false;
    btn.textContent = 'Analyze';
    document.getElementById('loading').classList.add('hidden');
  }
}

function renderResults(text) {
  const p = parseResponse(text);

  // Hero
  document.getElementById('r-ticker').textContent  = p.ticker || '—';
  document.getElementById('r-company').textContent = p.company || '';
  document.getElementById('r-price').textContent   = p.price   || '—';

  const changeEl = document.getElementById('r-change');
  changeEl.textContent  = p.change || '';
  changeEl.className    = 'change ' + (p.change.includes('-') ? 'neg' : 'pos');

  // Recommendation badge
  const badge = document.getElementById('r-rec-badge');
  badge.className = 'rec-badge' + (p.recommendation ? ` rec-${p.recommendation}` : '');
  document.getElementById('r-rec').textContent  = p.recommendation || '—';
  document.getElementById('r-conf').textContent = p.confidence ? `Confidence: ${p.confidence}` : '';

  // Metrics grid
  const grid = document.getElementById('r-metrics');
  grid.innerHTML = METRIC_KEYS
    .filter(k => p.metrics[k])
    .map(k => renderMetricCard(k, p.metrics[k]))
    .join('');

  // Commentary (markdown)
  document.getElementById('r-commentary').innerHTML =
    p.commentary ? marked.parse(p.commentary) : '<p style="color:var(--muted)">No commentary available.</p>';

  // Investment analysis (markdown)
  const analysisEl = document.getElementById('r-analysis');
  let html = p.analysisBody ? marked.parse(p.analysisBody) : '';
  if (p.disclaimer) {
    html += `<div class="disclaimer">${marked.parse(p.disclaimer)}</div>`;
  }
  analysisEl.innerHTML = html || '<p style="color:var(--muted)">No analysis available.</p>';

  document.getElementById('results').classList.remove('hidden');
  document.getElementById('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.post("/analyze/{ticker}")
async def analyze(ticker: str):
    ticker = ticker.upper().strip()
    task_id = str(uuid.uuid4())
    a2a_request = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": f"Should I invest in {ticker}?"}],
            },
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(ADVISOR_URL, json=a2a_request)
        resp.raise_for_status()
        data = resp.json()

    result = data.get("result", {})
    text = ""
    for artifact in result.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("type") == "text":
                text += part["text"]

    return JSONResponse({
        "analysis": text,
        "status": result.get("status", {}).get("state", "unknown"),
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
