#!/usr/bin/env python3
"""
Reference Implementation: Pattern involving Metric Definition
=============================================================

Architecture (3 layers):
  AI Layer:        Orchestrator → Domain Agent (Catalog Lookup Skill) → Text2SQL Agent
  Knowledge Layer: Vector Store (embedded metric definitions from Enterprise Data Catalog)
  Data Layer:      Gold Layer with certified Metric Views (SQLite)

Flow:
  Pre-runtime (one-off):
    1. Load Technical & Business Metadata from Enterprise Data Catalog
    2. Embed all metric definitions → Vector Store

  Runtime (per user query):
    1. User query → Orchestrator
    2. Orchestrator → Domain Agent
    3. Domain Agent invokes Catalog Lookup Skill via MCP Gateway
    4. Catalog Lookup → Vector Store (semantic search)
    5. Domain Agent passes enriched metric context → Text2SQL Agent
    6. Text2SQL Agent generates SQL → Metric Views in Gold Layer
    7. Results returned to user

Usage:
  pip install -r requirements-metric-agent.txt
  export ANTHROPIC_API_KEY=sk-ant-...
  python metric_definition_agent.py "What was the net revenue on January 1st?"
  python metric_definition_agent.py   # interactive mode
"""

import json
import os
import sqlite3
import sys
from typing import Optional

import anthropic
import numpy as np
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# ENTERPRISE DATA CATALOG (Technical & Business Metadata)
# In production: pull from Alation, Collibra, DataHub, dbt docs, etc.
# ─────────────────────────────────────────────────────────────────────────────

ENTERPRISE_CATALOG: list[dict] = [
    {
        "id": "m001",
        "name": "Net Revenue",
        "aliases": ["revenue", "net sales", "NR", "sales"],
        "business_definition": "Total revenue after deducting returns, allowances, and discounts from gross sales.",
        "technical_definition": "SUM(quantity * unit_price * (1 - discount_rate)) for all COMPLETED orders.",
        "table": "fact_orders",
        "sql_expression": "SUM(o.quantity * o.unit_price * (1 - COALESCE(o.discount_rate, 0)))",
        "filters": "o.status = 'COMPLETED'",
        "grain": "Daily",
        "owner": "Finance",
        "tags": ["financial", "revenue", "P&L"],
    },
    {
        "id": "m002",
        "name": "Active Users",
        "aliases": ["MAU", "DAU", "active customers", "users"],
        "business_definition": "Count of unique users who performed at least one action in the period.",
        "technical_definition": "COUNT(DISTINCT user_id) from fact_user_events, excluding bot traffic.",
        "table": "fact_user_events",
        "sql_expression": "COUNT(DISTINCT ue.user_id)",
        "filters": "ue.event_type != 'bot'",
        "grain": "Daily",
        "owner": "Product",
        "tags": ["engagement", "users", "KPI"],
    },
    {
        "id": "m003",
        "name": "Gross Margin",
        "aliases": ["GM", "gross profit margin", "margin"],
        "business_definition": "Percentage of revenue remaining after cost of goods sold.",
        "technical_definition": "(Net Revenue - COGS) / Net Revenue * 100",
        "table": "fact_orders",
        "sql_expression": "(SUM(o.quantity * o.unit_price * (1 - COALESCE(o.discount_rate, 0))) - SUM(o.quantity * o.unit_cost)) * 100.0 / NULLIF(SUM(o.quantity * o.unit_price * (1 - COALESCE(o.discount_rate, 0))), 0)",
        "filters": "o.status = 'COMPLETED'",
        "grain": "Monthly",
        "owner": "Finance",
        "tags": ["financial", "margin", "P&L"],
    },
    {
        "id": "m004",
        "name": "Customer Acquisition Cost",
        "aliases": ["CAC", "cost per acquisition", "acquisition cost"],
        "business_definition": "Total marketing spend divided by number of new customers acquired.",
        "technical_definition": "SUM(marketing_spend) / COUNT(DISTINCT new_customer_id)",
        "table": "fact_marketing_spend JOIN fact_customers",
        "sql_expression": "SUM(ms.spend) / NULLIF(COUNT(DISTINCT fc.customer_id), 0)",
        "filters": "fc.is_new = 1",
        "grain": "Monthly",
        "owner": "Marketing",
        "tags": ["marketing", "acquisition", "KPI"],
    },
    {
        "id": "m005",
        "name": "Churn Rate",
        "aliases": ["customer churn", "attrition rate", "churn"],
        "business_definition": "Percentage of customers who stopped using the product in a given period.",
        "technical_definition": "Customers CHURNED in period / Total customers at period start * 100",
        "table": "fact_subscriptions",
        "sql_expression": "SUM(CASE WHEN s.status = 'CHURNED' THEN 1.0 ELSE 0 END) * 100.0 / NULLIF(COUNT(DISTINCT s.customer_id), 0)",
        "filters": "s.period_start >= :start_date AND s.period_end <= :end_date",
        "grain": "Monthly",
        "owner": "Customer Success",
        "tags": ["retention", "churn", "KPI"],
    },
    {
        "id": "m006",
        "name": "Order Count",
        "aliases": ["orders", "number of orders", "order volume"],
        "business_definition": "Total number of completed orders placed in the period.",
        "technical_definition": "COUNT(DISTINCT order_id) for status = COMPLETED",
        "table": "fact_orders",
        "sql_expression": "COUNT(DISTINCT o.order_id)",
        "filters": "o.status = 'COMPLETED'",
        "grain": "Daily",
        "owner": "Operations",
        "tags": ["orders", "volume", "operations"],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# GOLD LAYER — Certified Metric Views
# In production: Snowflake / BigQuery / Databricks with certified metric views
# ─────────────────────────────────────────────────────────────────────────────

_GOLD_LAYER_DDL = """
CREATE TABLE IF NOT EXISTS fact_orders (
    order_id      TEXT PRIMARY KEY,
    customer_id   TEXT,
    order_date    DATE,
    quantity      INTEGER,
    unit_price    REAL,
    unit_cost     REAL,
    discount_rate REAL,
    status        TEXT,
    region        TEXT
);

CREATE TABLE IF NOT EXISTS fact_user_events (
    event_id   TEXT PRIMARY KEY,
    user_id    TEXT,
    event_date DATE,
    event_type TEXT
);

CREATE TABLE IF NOT EXISTS fact_marketing_spend (
    spend_id   TEXT PRIMARY KEY,
    channel    TEXT,
    spend_date DATE,
    spend      REAL
);

CREATE TABLE IF NOT EXISTS fact_customers (
    customer_id TEXT PRIMARY KEY,
    signup_date DATE,
    is_new      INTEGER
);

CREATE TABLE IF NOT EXISTS fact_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    customer_id     TEXT,
    status          TEXT,
    period_start    DATE,
    period_end      DATE
);

-- Certified Metric Views (Gold Layer)
CREATE VIEW IF NOT EXISTS mv_daily_revenue AS
SELECT
    order_date                                                          AS date,
    region,
    SUM(quantity * unit_price * (1 - COALESCE(discount_rate, 0)))      AS net_revenue,
    SUM(quantity * unit_cost)                                           AS total_cost,
    COUNT(DISTINCT order_id)                                            AS order_count
FROM fact_orders
WHERE status = 'COMPLETED'
GROUP BY order_date, region;

CREATE VIEW IF NOT EXISTS mv_daily_active_users AS
SELECT
    event_date              AS date,
    COUNT(DISTINCT user_id) AS active_users
FROM fact_user_events
WHERE event_type != 'bot'
GROUP BY event_date;
"""

_GOLD_LAYER_SEED = """
INSERT OR IGNORE INTO fact_orders VALUES
('o1','c1','2024-01-01',10,100.0,60.0,0.05,'COMPLETED','North'),
('o2','c2','2024-01-01',5, 200.0,120.0,0.10,'COMPLETED','South'),
('o3','c3','2024-01-02',8, 150.0,90.0, 0.00,'COMPLETED','North'),
('o4','c4','2024-01-02',3, 250.0,150.0,0.15,'COMPLETED','East'),
('o5','c5','2024-01-03',12,80.0, 48.0, 0.00,'COMPLETED','West'),
('o6','c1','2024-01-03',6, 120.0,72.0, 0.05,'COMPLETED','North');

INSERT OR IGNORE INTO fact_user_events VALUES
('e1','u1','2024-01-01','login'),
('e2','u2','2024-01-01','purchase'),
('e3','u3','2024-01-01','login'),
('e4','u1','2024-01-02','purchase'),
('e5','u4','2024-01-02','login'),
('e6','u5','2024-01-03','login'),
('e7','u2','2024-01-03','browse');

INSERT OR IGNORE INTO fact_customers VALUES
('c1','2024-01-01',1),('c2','2024-01-01',1),
('c3','2023-12-15',0),('c4','2024-01-02',1),('c5','2024-01-03',1);

INSERT OR IGNORE INTO fact_marketing_spend VALUES
('ms1','Google Ads','2024-01-01',5000.0),
('ms2','Facebook',  '2024-01-01',3000.0),
('ms3','Google Ads','2024-01-02',4500.0);

INSERT OR IGNORE INTO fact_subscriptions VALUES
('s1','c1','ACTIVE', '2024-01-01','2024-01-31'),
('s2','c2','CHURNED','2024-01-01','2024-01-15'),
('s3','c3','ACTIVE', '2024-01-01','2024-01-31'),
('s4','c4','CHURNED','2024-01-02','2024-01-20'),
('s5','c5','ACTIVE', '2024-01-03','2024-01-31');
"""


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE LAYER — Vector Store
# ─────────────────────────────────────────────────────────────────────────────

class VectorStore:
    """
    Stores embedded metric definitions from the Enterprise Data Catalog.
    Uses TF-IDF for semantic similarity (swap in sentence-transformers for production).
    """

    def __init__(self) -> None:
        self._metrics: list[dict] = []
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._matrix = None

    def build(self, catalog: list[dict]) -> None:
        """Pre-runtime: embed all metric definitions and index them."""
        self._metrics = catalog
        texts = [
            f"{m['name']} {' '.join(m['aliases'])} "
            f"{m['business_definition']} {m['technical_definition']} "
            f"{' '.join(m['tags'])}"
            for m in catalog
        ]
        self._vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        self._matrix = self._vectorizer.fit_transform([t.lower() for t in texts])
        print(f"  [VectorStore] Indexed {len(catalog)} metric definitions.")

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """Return top-k metric definitions most similar to query."""
        query_vec = self._vectorizer.transform([query.lower()])
        scores = cosine_similarity(query_vec, self._matrix).flatten()
        top_indices = scores.argsort()[-top_k:][::-1]
        return [
            {**self._metrics[i], "_score": float(scores[i])}
            for i in top_indices
            if scores[i] > 0
        ]


# ─────────────────────────────────────────────────────────────────────────────
# AI LAYER — MCP Gateway + Skill Registry
# In production: actual MCP server; here it's direct function dispatch
# ─────────────────────────────────────────────────────────────────────────────

class MCPGateway:
    """
    Routes Anthropic tool_use calls to registered skills.
    Exposes: catalog_lookup (Knowledge Layer) and execute_sql (Data Layer).
    """

    def __init__(self, vector_store: VectorStore, db: sqlite3.Connection) -> None:
        self._vs = vector_store
        self._db = db

    # ── Tool definitions (Anthropic SDK format) ───────────────────────────────

    TOOL_CATALOG_LOOKUP = {
        "name": "catalog_lookup",
        "description": (
            "Search the Enterprise Data Catalog for metric definitions matching a query. "
            "Returns business definitions, SQL expressions, table names, and ownership metadata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the metric or KPI to look up.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results (default 3).",
                },
            },
            "required": ["query"],
        },
    }

    TOOL_EXECUTE_SQL = {
        "name": "execute_sql",
        "description": (
            "Execute a read-only SQL query against the Gold Layer (certified metric views + fact tables). "
            "Available views: mv_daily_revenue(date, region, net_revenue, total_cost, order_count), "
            "mv_daily_active_users(date, active_users). "
            "Available tables: fact_orders, fact_user_events, fact_marketing_spend, "
            "fact_customers, fact_subscriptions. Use SQLite syntax."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT query to execute against the Gold Layer.",
                }
            },
            "required": ["sql"],
        },
    }

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, tool_name: str, tool_input: dict):
        if tool_name == "catalog_lookup":
            results = self._vs.search(tool_input["query"], tool_input.get("top_k", 3))
            print(f"    → {len(results)} metric(s) found: {[r['name'] for r in results]}")
            return results
        elif tool_name == "execute_sql":
            sql = tool_input["sql"]
            print(f"    → SQL: {sql[:120].replace(chr(10), ' ')}{'...' if len(sql) > 120 else ''}")
            cursor = self._db.cursor()
            cursor.execute(sql)
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        raise ValueError(f"Unknown tool: {tool_name}")


# ─────────────────────────────────────────────────────────────────────────────
# AI LAYER — Text2SQL Agent
# ─────────────────────────────────────────────────────────────────────────────

_TEXT2SQL_SYSTEM = """You are a Text2SQL expert for a business analytics platform.

You receive a user question and relevant metric definitions retrieved from the Enterprise Data Catalog.
Your job:
1. Use the provided metric definitions (SQL expressions, table names, filters) to write correct SQL.
2. Call execute_sql to run the query.
3. Summarize the results clearly for the user.

SQL rules (SQLite):
- Prefer mv_daily_revenue / mv_daily_active_users views when they cover the computation.
- When writing raw SQL, use the exact sql_expression from the metric definition.
- Always alias computed columns with readable names.
- Do NOT use window functions or CTEs unless necessary.
"""


class Text2SQLAgent:
    """Generates and executes SQL using metric context from the Domain Agent."""

    def __init__(self, client: anthropic.Anthropic, gateway: MCPGateway) -> None:
        self._client = client
        self._gateway = gateway

    def run(self, question: str, metric_context: list[dict]) -> str:
        context_str = json.dumps(
            [{k: v for k, v in m.items() if k != "_score"} for m in metric_context],
            indent=2,
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"User question: {question}\n\n"
                    f"Metric definitions from Enterprise Data Catalog:\n{context_str}\n\n"
                    "Generate and execute the SQL to answer this question."
                ),
            }
        ]

        print("  [Text2SQLAgent] Generating SQL...")
        while True:
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=_TEXT2SQL_SYSTEM,
                tools=[self._gateway.TOOL_EXECUTE_SQL],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return next(
                    (b.text for b in response.content if hasattr(b, "text")),
                    "No result.",
                )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"  [Text2SQLAgent] → {block.name}")
                        try:
                            result = self._gateway.dispatch(block.name, block.input)
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(result),
                                }
                            )
                        except Exception as exc:
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"Error: {exc}",
                                    "is_error": True,
                                }
                            )
                messages.append({"role": "user", "content": tool_results})


# ─────────────────────────────────────────────────────────────────────────────
# AI LAYER — Domain Agent
# ─────────────────────────────────────────────────────────────────────────────

_DOMAIN_AGENT_SYSTEM = """You are a Business Domain Expert Agent for an analytics platform.

Your responsibilities:
1. Understand what business metric(s) the user is asking about.
2. Use catalog_lookup to retrieve the correct metric definitions from the Enterprise Data Catalog.
3. If the question involves multiple metrics, look up each one separately.
4. Always use catalog_lookup — never guess metric definitions.
"""


class DomainAgent:
    """
    Understands business intent, uses Catalog Lookup Skill to retrieve metric definitions,
    then delegates to Text2SQL Agent for data retrieval from the Gold Layer.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        gateway: MCPGateway,
        text2sql: Text2SQLAgent,
    ) -> None:
        self._client = client
        self._gateway = gateway
        self._text2sql = text2sql

    def run(self, question: str) -> str:
        print("  [DomainAgent] Looking up metric definitions via Catalog Lookup Skill...")
        messages = [{"role": "user", "content": question}]
        metric_context: list[dict] = []
        seen_ids: set[str] = set()

        while True:
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=_DOMAIN_AGENT_SYSTEM,
                tools=[self._gateway.TOOL_CATALOG_LOOKUP],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use" and block.name == "catalog_lookup":
                        print(f"  [DomainAgent] Catalog lookup: '{block.input['query']}'")
                        results = self._gateway.dispatch(block.name, block.input)
                        for r in results:
                            if r["id"] not in seen_ids:
                                seen_ids.add(r["id"])
                                metric_context.append(r)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(results),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        if not metric_context:
            metric_context = self._gateway.dispatch(
                "catalog_lookup", {"query": question, "top_k": 3}
            )

        print(f"  [DomainAgent] Metric context ready: {[m['name'] for m in metric_context]}")
        return self._text2sql.run(question, metric_context)


# ─────────────────────────────────────────────────────────────────────────────
# AI LAYER — Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

_ORCHESTRATOR_SYSTEM = """You are an analytics query router.

Classify the user's query:
- ANALYTICS: any question about business metrics, KPIs, revenue, users, orders, margin, churn, or data
- GENERAL: greetings, help requests, or questions unrelated to business data

For ANALYTICS queries respond with exactly: ROUTE:domain_agent
For GENERAL queries respond with a short, helpful answer."""


class Orchestrator:
    """Entry point. Routes analytics queries to Domain Agent; answers general queries directly."""

    def __init__(self, client: anthropic.Anthropic, domain_agent: DomainAgent) -> None:
        self._client = client
        self._domain_agent = domain_agent

    def run(self, query: str) -> str:
        print(f"\n[Orchestrator] Query: '{query}'")

        response = self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=_ORCHESTRATOR_SYSTEM,
            messages=[{"role": "user", "content": query}],
        )
        decision = response.content[0].text.strip()

        if "ROUTE:domain_agent" in decision:
            print("[Orchestrator] → Domain Agent")
            return self._domain_agent.run(query)

        return decision


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE SETUP
# ─────────────────────────────────────────────────────────────────────────────

def _init_gold_layer() -> sqlite3.Connection:
    """Initialize the in-memory Gold Layer with metric views and seed data."""
    db = sqlite3.connect(":memory:")
    db.executescript(_GOLD_LAYER_DDL)
    db.executescript(_GOLD_LAYER_SEED)
    db.commit()
    print("  [GoldLayer] Metric views created and seeded.")
    return db


def build_pipeline() -> Orchestrator:
    """
    Pre-runtime initialization:
      1. Embed Enterprise Data Catalog → Vector Store  (Knowledge Layer)
      2. Initialize Gold Layer with Metric Views       (Data Layer)
      3. Wire up MCP Gateway + AI agents               (AI Layer)
    """
    print("\n=== Pre-runtime: Building Knowledge Layer ===")
    vector_store = VectorStore()
    vector_store.build(ENTERPRISE_CATALOG)

    print("\n=== Pre-runtime: Initializing Gold Layer ===")
    db = _init_gold_layer()

    mcp_gateway = MCPGateway(vector_store, db)
    client = anthropic.Anthropic()

    text2sql = Text2SQLAgent(client, mcp_gateway)
    domain_agent = DomainAgent(client, mcp_gateway, text2sql)
    orchestrator = Orchestrator(client, domain_agent)

    print("\n=== Pipeline ready ===\n")
    return orchestrator


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

_EXAMPLE_QUERIES = [
    "What was the net revenue on January 1st 2024?",
    "How many active users did we have on January 2nd?",
    "What is the gross margin across all orders?",
    "What is the customer acquisition cost for January 2024?",
    "What is the churn rate for January 2024?",
    "Show me daily revenue by region for January 2024",
]


def main() -> None:
    orchestrator = build_pipeline()

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        result = orchestrator.run(query)
        print(f"\n{'─'*60}")
        print(result)
        return

    print("Metric Definition Agent — interactive mode")
    print("Example queries:")
    for q in _EXAMPLE_QUERIES:
        print(f"  • {q}")
    print("\nType 'quit' to exit.\n")

    while True:
        try:
            query = input("Query: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            break
        result = orchestrator.run(query)
        print(f"\nAnswer:\n{result}\n{'─'*60}\n")


if __name__ == "__main__":
    main()
