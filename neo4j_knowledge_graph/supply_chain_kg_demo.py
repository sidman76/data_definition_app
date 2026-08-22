"""
Small Neo4j supply chain knowledge graph demo.

The ontology matches pepsico_KG.pptx: a Product is stocked in a Warehouse,
which has an Inventory Level and sits in a Location; a Location is the
pick-up point for a Distribution Region; a Region supplies Stores and has
Delivery Routes; a Store sits in a Location and has Contract Terms.

This lets you answer the deck's example question end-to-end:
"Which warehouse is closest to fulfill a Doritos order at Supermarket A?"
-> traverse Product -[:IS_STOCKED_IN]-> Warehouse -[:LOCATED_IN]-> Location
   -[:PICK_UP_AT]-> DistributionRegion -[:SUPPLIES_TO]-> Store

Start Neo4j locally:
    docker compose -f docker-compose.neo4j.yml up -d
    (browser UI at http://localhost:7474, login neo4j/password)

Usage:
    python supply_chain_kg_demo.py reset      # wipe the database
    python supply_chain_kg_demo.py seed       # load the sample supply chain graph
    python supply_chain_kg_demo.py examples   # run a set of annotated example Cypher queries
    python supply_chain_kg_demo.py shell      # interactive Cypher prompt
    python supply_chain_kg_demo.py ask "<question>"   # ask a question in plain English;
                                                        # Claude generates and runs the Cypher
    python supply_chain_kg_demo.py ask        # same, but interactive (repeat questions,
                                                # remembers earlier questions/answers so
                                                # you can ask follow-ups in the same session)

`ask` requires ANTHROPIC_API_KEY. Generated queries are only executed if they
look read-only (no CREATE/MERGE/DELETE/SET/REMOVE/DROP) — anything else is
printed but not run, so a bad generation can't corrupt the seeded data.

Connection settings come from env vars, loaded from a .env file if present
(defaults match docker-compose.neo4j.yml):
    NEO4J_URI=bolt://localhost:7687
    NEO4J_USER=neo4j
    NEO4J_PASSWORD=password

For Neo4j Aura, put these in .env instead:
    NEO4J_URI=neo4j+s://<your-instance-id>.databases.neo4j.io
    NEO4J_USER=neo4j
    NEO4J_PASSWORD=<your Aura password>
"""
import json
import os
import re
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password")

# --- Sample supply chain data -----------------------------------------------
# Ontology (see pepsico_KG.pptx):
#   (:Product)-[:IS_STOCKED_IN]->(:Warehouse)
#   (:Warehouse)-[:HAS_INVENTORY]->(:InventoryLevel)
#   (:Warehouse)-[:LOCATED_IN]->(:Location)
#   (:Location)-[:PICK_UP_AT]->(:DistributionRegion)
#   (:DistributionRegion)-[:SUPPLIES_TO]->(:Store)
#   (:DistributionRegion)-[:HAS]->(:DeliveryRoute)
#   (:Store)-[:LOCATED_IN]->(:Location)
#   (:Store)-[:HAS]->(:ContractTerms)

PRODUCTS = [
    {"name": "Doritos"},
    {"name": "Lay's Classic"},
    {"name": "Tostitos"},
    {"name": "Cheetos"},
    {"name": "Pepsi"},
    {"name": "Mountain Dew"},
    {"name": "Gatorade"},
    {"name": "Mirinda"},
]

REGIONS = [
    {"name": "Southeast"},
    {"name": "Northeast"},
    {"name": "Midwest"},
    {"name": "West"},
]

WAREHOUSES = [
    {"code": "WH123", "city": "Tampa", "state": "FL", "region": "Southeast"},
    {"code": "WH205", "city": "Atlanta", "state": "GA", "region": "Southeast"},
    {"code": "WH310", "city": "Newark", "state": "NJ", "region": "Northeast"},
    {"code": "WH318", "city": "Boston", "state": "MA", "region": "Northeast"},
    {"code": "WH415", "city": "Chicago", "state": "IL", "region": "Midwest"},
    {"code": "WH520", "city": "Los Angeles", "state": "CA", "region": "West"},
]

STORES = [
    {"name": "Supermarket A", "city": "Miami", "state": "FL", "region": "Southeast"},
    {"name": "Supermarket B", "city": "Orlando", "state": "FL", "region": "Southeast"},
    {"name": "Supermarket C", "city": "Boston", "state": "MA", "region": "Northeast"},
    {"name": "Supermarket D", "city": "Chicago", "state": "IL", "region": "Midwest"},
    {"name": "Supermarket E", "city": "Los Angeles", "state": "CA", "region": "West"},
    {"name": "Supermarket F", "city": "Newark", "state": "NJ", "region": "Northeast"},
]

# Locations are derived from the warehouses/stores above (deduplicated by city+state)
# rather than hand-listed, so they can never drift out of sync with those tables.
LOCATIONS = list({
    (row["city"], row["state"]): {"city": row["city"], "state": row["state"]}
    for row in WAREHOUSES + STORES
}.values())

STOCKED_IN = [  # (product, warehouse_code)
    ("Doritos", "WH123"),
    ("Doritos", "WH205"),
    ("Doritos", "WH415"),
    ("Lay's Classic", "WH123"),
    ("Lay's Classic", "WH310"),
    ("Lay's Classic", "WH520"),
    ("Tostitos", "WH205"),
    ("Tostitos", "WH415"),
    ("Cheetos", "WH123"),
    ("Cheetos", "WH318"),
    ("Pepsi", "WH310"),
    ("Pepsi", "WH415"),
    ("Pepsi", "WH520"),
    ("Mountain Dew", "WH205"),
    ("Mountain Dew", "WH520"),
    ("Gatorade", "WH123"),
    ("Gatorade", "WH318"),
    ("Gatorade", "WH415"),
    ("Mirinda", "WH205"),
]

INVENTORY_LEVELS = [  # (warehouse_code, product, cases, level)
    ("WH123", "Doritos", 62, ">50 cases"),
    ("WH205", "Doritos", 40, "26-50 cases"),
    ("WH415", "Doritos", 15, "<=25 cases"),
    ("WH123", "Lay's Classic", 80, ">50 cases"),
    ("WH310", "Lay's Classic", 45, "26-50 cases"),
    ("WH520", "Lay's Classic", 20, "<=25 cases"),
    ("WH205", "Tostitos", 55, ">50 cases"),
    ("WH415", "Tostitos", 30, "26-50 cases"),
    ("WH123", "Cheetos", 70, ">50 cases"),
    ("WH318", "Cheetos", 18, "<=25 cases"),
    ("WH310", "Pepsi", 90, ">50 cases"),
    ("WH415", "Pepsi", 60, ">50 cases"),
    ("WH520", "Pepsi", 35, "26-50 cases"),
    ("WH205", "Mountain Dew", 48, "26-50 cases"),
    ("WH520", "Mountain Dew", 22, "<=25 cases"),
    ("WH123", "Gatorade", 100, ">50 cases"),
    ("WH318", "Gatorade", 40, "26-50 cases"),
    ("WH415", "Gatorade", 12, "<=25 cases"),
    ("WH205", "Mirinda", 28, "26-50 cases"),
]

CONTRACT_TERMS = [  # (store, product, price_per_case, year)
    ("Supermarket A", "Doritos", "$28.50", 2026),
    ("Supermarket A", "Gatorade", "$22.00", 2026),
    ("Supermarket B", "Doritos", "$27.75", 2026),
    ("Supermarket C", "Pepsi", "$18.00", 2026),
    ("Supermarket C", "Cheetos", "$26.50", 2026),
    ("Supermarket D", "Tostitos", "$24.00", 2026),
    ("Supermarket D", "Pepsi", "$17.50", 2026),
    ("Supermarket E", "Mountain Dew", "$19.25", 2026),
    ("Supermarket E", "Lay's Classic", "$25.00", 2026),
    ("Supermarket F", "Lay's Classic", "$26.25", 2026),
]

DELIVERY_ROUTES = [  # (region, next_delivery, destination)
    ("Southeast", "Next delivery to Miami: next day", "Miami, FL"),
    ("Southeast", "Next delivery to Orlando: 2 days", "Orlando, FL"),
    ("Northeast", "Next delivery to Boston: next day", "Boston, MA"),
    ("Northeast", "Next delivery to Newark: same day", "Newark, NJ"),
    ("Midwest", "Next delivery to Chicago: next day", "Chicago, IL"),
    ("West", "Next delivery to Los Angeles: 2 days", "Los Angeles, CA"),
]


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def reset(driver):
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    print("Database wiped.")


def seed(driver):
    with driver.session() as session:
        session.run(
            "UNWIND $rows AS row MERGE (p:Product {name: row.name})",
            rows=PRODUCTS,
        )
        session.run(
            "UNWIND $rows AS row MERGE (r:DistributionRegion {name: row.name})",
            rows=REGIONS,
        )
        session.run(
            "UNWIND $rows AS row MERGE (l:Location {city: row.city, state: row.state})",
            rows=LOCATIONS,
        )
        session.run(
            "UNWIND $rows AS row "
            "MERGE (w:Warehouse {code: row.code}) "
            "WITH w, row "
            "MATCH (l:Location {city: row.city, state: row.state}) "
            "MERGE (w)-[:LOCATED_IN]->(l) "
            "WITH row "
            "MATCH (l:Location {city: row.city, state: row.state}), "
            "(reg:DistributionRegion {name: row.region}) "
            "MERGE (l)-[:PICK_UP_AT]->(reg)",
            rows=WAREHOUSES,
        )
        session.run(
            "UNWIND $rows AS row "
            "MERGE (s:Store {name: row.name}) "
            "WITH s, row "
            "MATCH (l:Location {city: row.city, state: row.state}) "
            "MERGE (s)-[:LOCATED_IN]->(l) "
            "WITH row "
            "MATCH (reg:DistributionRegion {name: row.region}), (s2:Store {name: row.name}) "
            "MERGE (reg)-[:SUPPLIES_TO]->(s2)",
            rows=STORES,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (p:Product {name: row[0]}), (w:Warehouse {code: row[1]}) "
            "MERGE (p)-[:IS_STOCKED_IN]->(w)",
            rows=STOCKED_IN,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (w:Warehouse {code: row[0]}) "
            "MERGE (i:InventoryLevel {warehouse: row[0], product: row[1]}) "
            "SET i.cases = row[2], i.level = row[3] "
            "MERGE (w)-[:HAS_INVENTORY]->(i)",
            rows=INVENTORY_LEVELS,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (s:Store {name: row[0]}) "
            "MERGE (c:ContractTerms {store: row[0], product: row[1]}) "
            "SET c.price_per_case = row[2], c.year = row[3] "
            "MERGE (s)-[:HAS]->(c)",
            rows=CONTRACT_TERMS,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (reg:DistributionRegion {name: row[0]}) "
            "MERGE (d:DeliveryRoute {region: row[0], destination: row[2]}) "
            "SET d.next_delivery = row[1] "
            "MERGE (reg)-[:HAS]->(d)",
            rows=DELIVERY_ROUTES,
        )
    print(
        f"Seeded {len(PRODUCTS)} products, {len(WAREHOUSES)} warehouses, "
        f"{len(STORES)} stores, {len(REGIONS)} regions, {len(LOCATIONS)} locations, "
        f"{len(INVENTORY_LEVELS)} inventory levels, {len(CONTRACT_TERMS)} contract terms, "
        f"{len(DELIVERY_ROUTES)} delivery routes."
    )


EXAMPLES = [
    (
        "Pattern match: which warehouses stock Doritos?",
        "MATCH (p:Product {name: 'Doritos'})-[:IS_STOCKED_IN]->(w:Warehouse) "
        "RETURN w.code AS warehouse",
    ),
    (
        "Filter with WHERE: inventory levels above 50 cases",
        "MATCH (w:Warehouse)-[:HAS_INVENTORY]->(i:InventoryLevel) WHERE i.cases > 50 "
        "RETURN w.code AS warehouse, i.product AS product, i.cases AS cases "
        "ORDER BY i.cases DESC",
    ),
    (
        "3-hop traversal: the deck's flagship question — which warehouse can "
        "fulfill a Doritos order at Supermarket A?",
        "MATCH (p:Product {name: 'Doritos'})-[:IS_STOCKED_IN]->(w:Warehouse)"
        "-[:LOCATED_IN]->(l:Location)-[:PICK_UP_AT]->(reg:DistributionRegion)"
        "-[:SUPPLIES_TO]->(s:Store {name: 'Supermarket A'}) "
        "RETURN w.code AS warehouse, l.city AS warehouse_city, reg.name AS region",
    ),
    (
        "Aggregation: how many products does each warehouse stock?",
        "MATCH (p:Product)-[:IS_STOCKED_IN]->(w:Warehouse) "
        "RETURN w.code AS warehouse, count(p) AS product_count "
        "ORDER BY product_count DESC",
    ),
    (
        "Combined pattern: contract terms for every store, grouped by the "
        "region that supplies it",
        "MATCH (reg:DistributionRegion)-[:SUPPLIES_TO]->(s:Store)-[:HAS]->(c:ContractTerms) "
        "RETURN reg.name AS region, s.name AS store, c.product AS product, "
        "c.price_per_case AS price, c.year AS year "
        "ORDER BY region, store",
    ),
    (
        "Full chain: warehouse + delivery timing to fulfill a Doritos order "
        "at Supermarket A",
        "MATCH (p:Product {name: 'Doritos'})-[:IS_STOCKED_IN]->(w:Warehouse)"
        "-[:LOCATED_IN]->(l:Location)-[:PICK_UP_AT]->(reg:DistributionRegion)"
        "-[:SUPPLIES_TO]->(s:Store {name: 'Supermarket A'})-[:LOCATED_IN]->(sl:Location), "
        "(reg)-[:HAS]->(dr:DeliveryRoute {destination: sl.city + ', ' + sl.state}) "
        "RETURN w.code AS warehouse, l.city AS warehouse_city, reg.name AS region, "
        "dr.next_delivery AS delivery",
    ),
]


def run_examples(driver):
    with driver.session() as session:
        for description, query in EXAMPLES:
            print(f"\n--- {description} ---")
            print(f"> {query}\n")
            result = session.run(query)
            rows = [record.data() for record in result]
            if not rows:
                print("(no results)")
            for row in rows:
                print(row)


GRAPH_SCHEMA = """\
Nodes:
  (:Product {name: string})
  (:Warehouse {code: string})
  (:Location {city: string, state: string})
  (:DistributionRegion {name: string})
  (:Store {name: string})
  (:InventoryLevel {warehouse: string, product: string, cases: int, level: string})
  (:ContractTerms {store: string, product: string, price_per_case: string, year: int})
  (:DeliveryRoute {region: string, destination: string, next_delivery: string})

Relationships:
  (:Product)-[:IS_STOCKED_IN]->(:Warehouse)
  (:Warehouse)-[:HAS_INVENTORY]->(:InventoryLevel)
  (:Warehouse)-[:LOCATED_IN]->(:Location)
  (:Location)-[:PICK_UP_AT]->(:DistributionRegion)
  (:DistributionRegion)-[:SUPPLIES_TO]->(:Store)
  (:DistributionRegion)-[:HAS]->(:DeliveryRoute)
  (:Store)-[:LOCATED_IN]->(:Location)
  (:Store)-[:HAS]->(:ContractTerms)
"""

WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|CALL\s+\w+\.\w+)\b", re.IGNORECASE
)

HISTORY_TURNS_KEPT = 10


def generate_cypher(client, question, history):
    system = (
        "You translate natural-language questions into a single Cypher query "
        "for a Neo4j supply chain knowledge graph. Use only this schema:\n\n"
        f"{GRAPH_SCHEMA}\n"
        "Rules:\n"
        "- Output ONLY the Cypher query, no explanation, no markdown fences.\n"
        "- The query must be read-only (MATCH/WHERE/RETURN/WITH/ORDER BY/etc).\n"
        "- Never use CREATE, MERGE, DELETE, SET, REMOVE, or DROP.\n"
        "- When RETURN uses DISTINCT or an aggregation (count, collect, etc.), "
        "ORDER BY may only reference variables/aliases in that RETURN clause.\n"
        "- To find which warehouse(s) can fulfill an order for a store, chain: "
        "Product-[:IS_STOCKED_IN]->Warehouse-[:LOCATED_IN]->Location"
        "-[:PICK_UP_AT]->DistributionRegion-[:SUPPLIES_TO]->Store.\n"
        "- The user may ask follow-up questions that refer back to the "
        "conversation (e.g. 'what about Gatorade instead?', 'and its contract "
        "terms?'). Use the conversation history below to resolve those "
        "references into a complete, self-contained query.\n"
    )

    user_content = question
    if history:
        transcript = "\n".join(
            f"Q: {turn['question']}\nA: {turn['answer']}"
            for turn in history[-HISTORY_TURNS_KEPT:]
        )
        user_content = (
            f"Conversation so far:\n{transcript}\n\n"
            f"New follow-up question: {question}"
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    query = response.content[0].text.strip()
    query = re.sub(r"^```(?:cypher)?\s*|\s*```$", "", query.strip(), flags=re.IGNORECASE)
    return query.strip()


def summarize_answer(client, question, rows):
    if not rows:
        return "I couldn't find anything in the graph that answers that."

    system = (
        "You are given a user's question and the raw rows a database query "
        "returned to answer it. Write a concise, natural, plain-English answer "
        "using only the data provided — no Cypher, no JSON, no field names, "
        "just the answer a person would want to read.\n\n"
        "This system is strictly read-only: the query that produced these rows "
        "never creates, modifies, or deletes anything, no matter how the "
        "question was phrased. If the question asked for an action rather than "
        "information (e.g. 'delete X', 'update Y's price'), do NOT claim that "
        "action happened — say plainly that this assistant can only read data, "
        "then describe what the current data shows instead."
    )
    user_content = (
        f"Question: {question}\n\nData (JSON rows):\n{json.dumps(rows, default=str)}"
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text.strip()


def ask_once(driver, client, question, history):
    """Answer one question. `history` is a list of {"question", "answer"} dicts
    from earlier in the session, used to resolve follow-up references. Returns
    the plain-English answer on success, or None if nothing was answered (so
    the caller knows not to add this turn to history)."""
    query = generate_cypher(client, question, history)
    print(f"> {query}\n")

    if WRITE_KEYWORDS.search(query):
        print("Refusing to run: generated query looks like a write, not a read.")
        return None

    with driver.session() as session:
        try:
            result = session.run(query)
            rows = [record.data() for record in result]
        except Exception as e:
            print(f"Query failed: {e}")
            return None

    answer = summarize_answer(client, question, rows)
    print(answer)
    return answer


def ask(driver, question):
    import anthropic

    client = anthropic.Anthropic()

    if question:
        ask_once(driver, client, question, history=[])
        return

    print(
        "Ask questions in plain English about the supply chain graph. "
        "Follow-up questions are supported. Type 'exit' to quit."
    )
    history = []
    while True:
        try:
            question = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in ("exit", "quit"):
            break
        if not question:
            continue
        try:
            answer = ask_once(driver, client, question, history)
            if answer is not None:
                history.append({"question": question, "answer": answer})
        except Exception as e:
            print(f"Error: {e}")


def shell(driver):
    print("Interactive Cypher shell. Type Cypher queries, or 'exit' to quit.")
    with driver.session() as session:
        while True:
            try:
                query = input("cypher> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if query.lower() in ("exit", "quit"):
                break
            if not query:
                continue
            try:
                result = session.run(query)
                rows = [record.data() for record in result]
                if not rows:
                    print("(no results)")
                for row in rows:
                    print(row)
            except Exception as e:
                print(f"Error: {e}")


def main():
    valid_commands = ("reset", "seed", "examples", "shell", "ask")
    if len(sys.argv) < 2 or sys.argv[1] not in valid_commands:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] != "ask" and len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Could not connect to Neo4j at {NEO4J_URI}: {e}")
        print("Start it with: docker compose -f docker-compose.neo4j.yml up -d")
        sys.exit(1)

    try:
        if command == "reset":
            reset(driver)
        elif command == "seed":
            seed(driver)
        elif command == "examples":
            run_examples(driver)
        elif command == "shell":
            shell(driver)
        elif command == "ask":
            question = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
            ask(driver, question)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
