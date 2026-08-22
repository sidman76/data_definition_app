"""
Small Neo4j knowledge graph demo.

Start Neo4j locally:
    docker compose -f docker-compose.neo4j.yml up -d
    (browser UI at http://localhost:7474, login neo4j/password)

Usage:
    python neo4j_kg_demo.py reset      # wipe the database
    python neo4j_kg_demo.py seed       # load the sample movie knowledge graph
    python neo4j_kg_demo.py examples   # run a set of annotated example Cypher queries
    python neo4j_kg_demo.py shell      # interactive Cypher prompt
    python neo4j_kg_demo.py ask "<question>"   # ask a question in plain English;
                                                # Claude generates and runs the Cypher
    python neo4j_kg_demo.py ask        # same, but interactive (repeat questions,
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

# A small movie knowledge graph: (:Person)-[:ACTED_IN|DIRECTED]->(:Movie)-[:IN_GENRE]->(:Genre)
# Movies carry a `language` property — an English/Hollywood set plus a
# Hindi/Bollywood set from the 1980s — so the graph can be queried/filtered by language.
PEOPLE = [
    {"name": "Keanu Reeves", "born": 1964},
    {"name": "Carrie-Anne Moss", "born": 1967},
    {"name": "Laurence Fishburne", "born": 1961},
    {"name": "Lana Wachowski", "born": 1965},
    {"name": "Tom Hanks", "born": 1956},
    {"name": "Robin Wright", "born": 1966},
    {"name": "Robert Zemeckis", "born": 1951},
    {"name": "Christopher Nolan", "born": 1970},
    {"name": "Leonardo DiCaprio", "born": 1974},
    {"name": "Elliot Page", "born": 1987},
    # Bollywood (1980s Hindi cinema)
    {"name": "Amitabh Bachchan", "born": 1942},
    {"name": "Rishi Kapoor", "born": 1952},
    {"name": "Rekha", "born": 1954},
    {"name": "Jaya Bachchan", "born": 1948},
    {"name": "Dilip Kumar", "born": 1922},
    {"name": "Tina Munim", "born": 1957},
    {"name": "Anil Kapoor", "born": 1956},
    {"name": "Sridevi", "born": 1963},
    {"name": "Madhuri Dixit", "born": 1967},
    {"name": "Aamir Khan", "born": 1965},
    {"name": "Juhi Chawla", "born": 1967},
    {"name": "Salman Khan", "born": 1965},
    {"name": "Bhagyashree", "born": 1969},
    {"name": "Subhash Ghai", "born": 1945},
    {"name": "Yash Chopra", "born": 1932},
    {"name": "Ramesh Sippy", "born": 1947},
    {"name": "Shekhar Kapur", "born": 1945},
    {"name": "N. Chandra", "born": 1949},
    {"name": "Mansoor Khan", "born": 1949},
    {"name": "Sooraj Barjatya", "born": 1964},
    # Bollywood (1990s Hindi cinema)
    {"name": "Shah Rukh Khan", "born": 1965},
    {"name": "Kajol", "born": 1974},
    {"name": "Shilpa Shetty", "born": 1975},
    {"name": "Abbas-Mustan", "born": 1958},
    {"name": "Aditya Chopra", "born": 1971},
    {"name": "Ram Gopal Varma", "born": 1962},
    {"name": "Urmila Matondkar", "born": 1974},
    {"name": "Karisma Kapoor", "born": 1974},
    {"name": "Karan Johar", "born": 1972},
    {"name": "Rani Mukerji", "born": 1978},
    {"name": "Aishwarya Rai", "born": 1973},
    {"name": "Akshaye Khanna", "born": 1975},
    {"name": "Rakesh Roshan", "born": 1949},
    {"name": "Hrithik Roshan", "born": 1974},
    {"name": "Ameesha Patel", "born": 1975},
]

MOVIES = [
    {"title": "The Matrix", "released": 1999, "genres": ["Sci-Fi", "Action"], "language": "English"},
    {"title": "The Matrix Reloaded", "released": 2003, "genres": ["Sci-Fi", "Action"], "language": "English"},
    {"title": "Forrest Gump", "released": 1994, "genres": ["Drama", "Romance"], "language": "English"},
    {"title": "Inception", "released": 2010, "genres": ["Sci-Fi", "Action"], "language": "English"},
    {"title": "The Dark Knight", "released": 2008, "genres": ["Action", "Crime"], "language": "English"},
    # Bollywood (1980s Hindi cinema)
    {"title": "Karz", "released": 1980, "genres": ["Drama", "Musical"], "language": "Hindi"},
    {"title": "Silsila", "released": 1981, "genres": ["Drama", "Romance"], "language": "Hindi"},
    {"title": "Shakti", "released": 1982, "genres": ["Drama", "Action"], "language": "Hindi"},
    {"title": "Mr. India", "released": 1987, "genres": ["Sci-Fi", "Action", "Comedy"], "language": "Hindi"},
    {"title": "Tezaab", "released": 1988, "genres": ["Action", "Drama"], "language": "Hindi"},
    {"title": "Qayamat Se Qayamat Tak", "released": 1988, "genres": ["Romance", "Drama"], "language": "Hindi"},
    {"title": "Maine Pyar Kiya", "released": 1989, "genres": ["Romance", "Drama"], "language": "Hindi"},
    {"title": "Chandni", "released": 1989, "genres": ["Romance", "Drama"], "language": "Hindi"},
    # Bollywood (1990s Hindi cinema)
    {"title": "Lamhe", "released": 1991, "genres": ["Drama", "Romance"], "language": "Hindi"},
    {"title": "Baazigar", "released": 1993, "genres": ["Thriller", "Romance"], "language": "Hindi"},
    {"title": "Hum Aapke Hain Koun", "released": 1994, "genres": ["Romance", "Drama", "Musical"], "language": "Hindi"},
    {"title": "Dilwale Dulhania Le Jayenge", "released": 1995, "genres": ["Romance", "Drama"], "language": "Hindi"},
    {"title": "Rangeela", "released": 1995, "genres": ["Romance", "Musical"], "language": "Hindi"},
    {"title": "Dil To Pagal Hai", "released": 1997, "genres": ["Romance", "Musical", "Drama"], "language": "Hindi"},
    {"title": "Kuch Kuch Hota Hai", "released": 1998, "genres": ["Romance", "Drama"], "language": "Hindi"},
    {"title": "Taal", "released": 1999, "genres": ["Romance", "Musical"], "language": "Hindi"},
    {"title": "Kaho Naa... Pyaar Hai", "released": 2000, "genres": ["Romance", "Action"], "language": "Hindi"},
]

ACTED_IN = [
    ("Keanu Reeves", "The Matrix", "Neo"),
    ("Carrie-Anne Moss", "The Matrix", "Trinity"),
    ("Laurence Fishburne", "The Matrix", "Morpheus"),
    ("Keanu Reeves", "The Matrix Reloaded", "Neo"),
    ("Carrie-Anne Moss", "The Matrix Reloaded", "Trinity"),
    ("Tom Hanks", "Forrest Gump", "Forrest"),
    ("Robin Wright", "Forrest Gump", "Jenny"),
    ("Leonardo DiCaprio", "Inception", "Cobb"),
    ("Elliot Page", "Inception", "Ariadne"),
    ("Leonardo DiCaprio", "The Dark Knight", None),
    # Bollywood (1980s Hindi cinema)
    ("Rishi Kapoor", "Karz", "Monty Oberoi"),
    ("Tina Munim", "Karz", "Kamini"),
    ("Amitabh Bachchan", "Silsila", "Amit"),
    ("Rekha", "Silsila", "Chandni"),
    ("Jaya Bachchan", "Silsila", "Shobha"),
    ("Amitabh Bachchan", "Shakti", "Vijay"),
    ("Dilip Kumar", "Shakti", "Ashwini Kumar"),
    ("Anil Kapoor", "Mr. India", "Arun Verma"),
    ("Sridevi", "Mr. India", "Seema"),
    ("Anil Kapoor", "Tezaab", "Mahesh Deshmukh"),
    ("Madhuri Dixit", "Tezaab", "Mohini"),
    ("Aamir Khan", "Qayamat Se Qayamat Tak", "Raj"),
    ("Juhi Chawla", "Qayamat Se Qayamat Tak", "Rashmi"),
    ("Salman Khan", "Maine Pyar Kiya", "Prem"),
    ("Bhagyashree", "Maine Pyar Kiya", "Suman"),
    ("Sridevi", "Chandni", "Chandni"),
    ("Rishi Kapoor", "Chandni", "Rohit"),
    # Bollywood (1990s Hindi cinema)
    ("Sridevi", "Lamhe", "Pallavi & Pooja"),
    ("Anil Kapoor", "Lamhe", "Viren Pratap Singh"),
    ("Shah Rukh Khan", "Baazigar", "Ajay Sharma"),
    ("Kajol", "Baazigar", "Priya"),
    ("Shilpa Shetty", "Baazigar", "Seema"),
    ("Salman Khan", "Hum Aapke Hain Koun", "Prem"),
    ("Madhuri Dixit", "Hum Aapke Hain Koun", "Nisha"),
    ("Shah Rukh Khan", "Dilwale Dulhania Le Jayenge", "Raj"),
    ("Kajol", "Dilwale Dulhania Le Jayenge", "Simran"),
    ("Aamir Khan", "Rangeela", "Munna"),
    ("Urmila Matondkar", "Rangeela", "Mili"),
    ("Shah Rukh Khan", "Dil To Pagal Hai", "Rahul"),
    ("Madhuri Dixit", "Dil To Pagal Hai", "Pooja"),
    ("Karisma Kapoor", "Dil To Pagal Hai", "Nisha"),
    ("Shah Rukh Khan", "Kuch Kuch Hota Hai", "Rahul"),
    ("Kajol", "Kuch Kuch Hota Hai", "Anjali"),
    ("Rani Mukerji", "Kuch Kuch Hota Hai", "Tina"),
    ("Aishwarya Rai", "Taal", "Mansi"),
    ("Akshaye Khanna", "Taal", "Manav"),
    ("Hrithik Roshan", "Kaho Naa... Pyaar Hai", "Rohit & Raj"),
    ("Ameesha Patel", "Kaho Naa... Pyaar Hai", "Sonia"),
]

DIRECTED = [
    ("Lana Wachowski", "The Matrix"),
    ("Lana Wachowski", "The Matrix Reloaded"),
    ("Robert Zemeckis", "Forrest Gump"),
    ("Christopher Nolan", "Inception"),
    ("Christopher Nolan", "The Dark Knight"),
    # Bollywood (1980s Hindi cinema)
    ("Subhash Ghai", "Karz"),
    ("Yash Chopra", "Silsila"),
    ("Ramesh Sippy", "Shakti"),
    ("Shekhar Kapur", "Mr. India"),
    ("N. Chandra", "Tezaab"),
    ("Mansoor Khan", "Qayamat Se Qayamat Tak"),
    ("Sooraj Barjatya", "Maine Pyar Kiya"),
    ("Yash Chopra", "Chandni"),
    # Bollywood (1990s Hindi cinema)
    ("Yash Chopra", "Lamhe"),
    ("Abbas-Mustan", "Baazigar"),
    ("Sooraj Barjatya", "Hum Aapke Hain Koun"),
    ("Aditya Chopra", "Dilwale Dulhania Le Jayenge"),
    ("Ram Gopal Varma", "Rangeela"),
    ("Yash Chopra", "Dil To Pagal Hai"),
    ("Karan Johar", "Kuch Kuch Hota Hai"),
    ("Subhash Ghai", "Taal"),
    ("Rakesh Roshan", "Kaho Naa... Pyaar Hai"),
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
            "UNWIND $rows AS row "
            "MERGE (p:Person {name: row.name}) "
            "SET p.born = row.born",
            rows=PEOPLE,
        )
        session.run(
            "UNWIND $rows AS row "
            "MERGE (m:Movie {title: row.title}) "
            "SET m.released = row.released, m.language = row.language",
            rows=MOVIES,
        )
        session.run(
            "UNWIND $rows AS row "
            "UNWIND row.genres AS genre "
            "MERGE (g:Genre {name: genre}) "
            "MERGE (m:Movie {title: row.title}) "
            "MERGE (m)-[:IN_GENRE]->(g)",
            rows=MOVIES,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (p:Person {name: row[0]}), (m:Movie {title: row[1]}) "
            "MERGE (p)-[r:ACTED_IN]->(m) "
            "SET r.role = row[2]",
            rows=ACTED_IN,
        )
        session.run(
            "UNWIND $rows AS row "
            "MATCH (p:Person {name: row[0]}), (m:Movie {title: row[1]}) "
            "MERGE (p)-[:DIRECTED]->(m)",
            rows=DIRECTED,
        )
    print(f"Seeded {len(PEOPLE)} people, {len(MOVIES)} movies, "
          f"{len(ACTED_IN)} ACTED_IN edges, {len(DIRECTED)} DIRECTED edges.")


EXAMPLES = [
    (
        "Pattern match: who acted in The Matrix?",
        "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie {title: 'The Matrix'}) "
        "RETURN p.name AS actor, r.role AS role",
    ),
    (
        "Filter with WHERE: movies released after 2000",
        "MATCH (m:Movie) WHERE m.released > 2000 "
        "RETURN m.title AS title, m.released AS released ORDER BY m.released",
    ),
    (
        "2-hop traversal: co-actors (people who shared a movie with Keanu Reeves)",
        "MATCH (:Person {name: 'Keanu Reeves'})-[:ACTED_IN]->(m:Movie)<-[:ACTED_IN]-(co:Person) "
        "RETURN DISTINCT co.name AS co_actor, m.title AS movie",
    ),
    (
        "Aggregation: number of movies per genre",
        "MATCH (m:Movie)-[:IN_GENRE]->(g:Genre) "
        "RETURN g.name AS genre, count(m) AS movie_count ORDER BY movie_count DESC",
    ),
    (
        "Variable-length path: shortest connection between two people through shared movies",
        "MATCH p = shortestPath("
        "(a:Person {name: 'Elliot Page'})-[:ACTED_IN*]-(b:Person {name: 'Tom Hanks'})"
        ") RETURN [n IN nodes(p) | coalesce(n.name, n.title)] AS path",
    ),
    (
        "Who both acted in and directed a movie?",
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie), (p)-[:DIRECTED]->(m) "
        "RETURN p.name AS person, m.title AS movie",
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
  (:Person {name: string, born: int})
  (:Movie {title: string, released: int, language: string})  // e.g. "English", "Hindi"
  (:Genre {name: string})

Relationships:
  (:Person)-[:ACTED_IN {role: string}]->(:Movie)
  (:Person)-[:DIRECTED]->(:Movie)
  (:Movie)-[:IN_GENRE]->(:Genre)
"""

WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|CALL\s+\w+\.\w+)\b", re.IGNORECASE
)


HISTORY_TURNS_KEPT = 10


def generate_cypher(client, question, history):
    system = (
        "You translate natural-language questions into a single Cypher query "
        "for a Neo4j movie knowledge graph. Use only this schema:\n\n"
        f"{GRAPH_SCHEMA}\n"
        "Rules:\n"
        "- Output ONLY the Cypher query, no explanation, no markdown fences.\n"
        "- The query must be read-only (MATCH/WHERE/RETURN/WITH/ORDER BY/etc).\n"
        "- Never use CREATE, MERGE, DELETE, SET, REMOVE, or DROP.\n"
        "- When RETURN uses DISTINCT or an aggregation (count, collect, etc.), "
        "ORDER BY may only reference variables/aliases in that RETURN clause.\n"
        "- The user may ask follow-up questions that refer back to the "
        "conversation (e.g. 'what about after 2005?', 'who directed that one?', "
        "'and her co-stars?'). Use the conversation history below to resolve "
        "those references into a complete, self-contained query.\n"
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
        "information (e.g. 'delete X', 'update Y's role'), do NOT claim that "
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
        "Ask questions in plain English about the movie graph. "
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
