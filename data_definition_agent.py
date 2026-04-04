"""
Data Definition Agent — generates business definitions for tables and columns.

Usage:
    python data_definition_agent.py input.txt [--output output.txt] [--batch 10]

Input file format (plain text):

    [TABLES]
    TABLE: schema.table_name
    catalog: my_catalog
    type: TABLE
    description: Existing description text
    notes: Any extra notes

    TABLE: finance.invoice_line
    catalog: my_catalog
    type: VIEW

    [COLUMNS]
    COLUMN: schema.table_name.column_name
    data_type: INTEGER
    nullable: NO
    description: Existing description
    sample_values: 1001, 1002

Run `python data_definition_agent.py --create-template` to generate a blank input template.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import anthropic


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ColumnMeta:
    table_schema: str
    table_name: str
    column_name: str
    data_type: str = ""
    is_nullable: str = ""
    existing_description: str = ""
    sample_values: str = ""
    # filled by agent
    business_definition: str = ""
    business_term: str = ""
    data_classification: str = ""


@dataclass
class TableMeta:
    table_catalog: str
    table_schema: str
    table_name: str
    table_type: str = "TABLE"
    existing_description: str = ""
    notes: str = ""
    columns: list[ColumnMeta] = field(default_factory=list)
    # filled by agent
    business_definition: str = ""
    business_name: str = ""
    data_domain: str = ""
    primary_use_case: str = ""


# ---------------------------------------------------------------------------
# Text I/O
# ---------------------------------------------------------------------------
#
# INPUT FORMAT
# ------------
# Sections: [TABLES] and [COLUMNS]
# Each entry starts with TABLE: schema.name  or  COLUMN: schema.table.column
# Followed by KEY: value lines until the next entry or section header.
# Blank lines and lines starting with # are ignored.
#
# OUTPUT FORMAT
# -------------
# Human-readable report with table definitions followed by their columns.
# Each table block is separated by a line of = signs.
# Column blocks are indented and separated by - lines.
# ---------------------------------------------------------------------------

def create_template(path: str) -> None:
    """Write a blank input template that data stewards can fill in."""
    content = """\
# DATA DEFINITION INPUT TEMPLATE
# Fill in the sections below and run:
#   python data_definition_agent.py input.txt
#
# Rules:
#   - TABLE: schema.table_name    (required)
#   - COLUMN: schema.table.column (optional but recommended)
#   - All other fields are optional
#   - Lines starting with # are comments and are ignored

[TABLES]
TABLE: sales.customer_orders
catalog: my_catalog
type: TABLE
description: Contains order records
notes: Sourced from SAP

TABLE: finance.invoice_line
catalog: my_catalog
type: VIEW
description:
notes:

[COLUMNS]
COLUMN: sales.customer_orders.order_id
data_type: INTEGER
nullable: NO
description: Unique order key
sample_values: 1001, 1002

COLUMN: sales.customer_orders.customer_id
data_type: INTEGER
nullable: NO
description:
sample_values:

COLUMN: sales.customer_orders.order_date
data_type: DATE
nullable: NO
description:
sample_values: 2024-01-15

COLUMN: sales.customer_orders.total_amount
data_type: DECIMAL(18,2)
nullable: YES
description:
sample_values: 1250.00
"""
    Path(path).write_text(content, encoding="utf-8")
    print(f"Template saved → {path}")


def _parse_entry_block(lines: list[str]) -> dict[str, str]:
    """Parse KEY: value lines into a dict (values may span no continuation lines)."""
    result: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip().lower()] = val.strip()
    return result


def load_input(path: str) -> tuple[list[TableMeta], list[ColumnMeta]]:
    """Load tables and columns from a structured plain-text input file."""
    raw = Path(path).read_text(encoding="utf-8")

    tables: list[TableMeta] = []
    columns: list[ColumnMeta] = []

    section = None          # "tables" | "columns"
    current_key: str = ""   # e.g. "sales.customer_orders"
    current_lines: list[str] = []

    def flush():
        if not current_key or not current_lines:
            return
        props = _parse_entry_block(current_lines)
        if section == "tables":
            parts = current_key.split(".", 1)
            schema = parts[0] if len(parts) > 1 else ""
            name = parts[1] if len(parts) > 1 else parts[0]
            tables.append(TableMeta(
                table_catalog=props.get("catalog", ""),
                table_schema=schema,
                table_name=name,
                table_type=props.get("type", "TABLE") or "TABLE",
                existing_description=props.get("description", ""),
                notes=props.get("notes", ""),
            ))
        elif section == "columns":
            parts = current_key.split(".", 2)
            schema = parts[0] if len(parts) > 2 else ""
            tname = parts[1] if len(parts) > 2 else (parts[0] if len(parts) > 1 else "")
            cname = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else parts[0])
            columns.append(ColumnMeta(
                table_schema=schema,
                table_name=tname,
                column_name=cname,
                data_type=props.get("data_type", ""),
                is_nullable=props.get("nullable", ""),
                existing_description=props.get("description", ""),
                sample_values=props.get("sample_values", ""),
            ))

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper() == "[TABLES]":
            flush(); current_key = ""; current_lines = []; section = "tables"
        elif line.upper() == "[COLUMNS]":
            flush(); current_key = ""; current_lines = []; section = "columns"
        elif section == "tables" and line.upper().startswith("TABLE:"):
            flush()
            current_key = line.split(":", 1)[1].strip()
            current_lines = []
        elif section == "columns" and line.upper().startswith("COLUMN:"):
            flush()
            current_key = line.split(":", 1)[1].strip()
            current_lines = []
        else:
            current_lines.append(line)

    flush()

    # Attach columns to tables
    col_lookup: dict[tuple, list[ColumnMeta]] = {}
    for c in columns:
        col_lookup.setdefault((c.table_schema, c.table_name), []).append(c)

    if not tables and columns:
        seen: dict[tuple, TableMeta] = {}
        for c in columns:
            key = (c.table_schema, c.table_name)
            if key not in seen:
                seen[key] = TableMeta(
                    table_catalog="", table_schema=c.table_schema,
                    table_name=c.table_name,
                )
            seen[key].columns.append(c)
        tables = list(seen.values())
    else:
        for t in tables:
            t.columns = col_lookup.get((t.table_schema, t.table_name), [])

    return tables, columns


def save_output(tables: list[TableMeta], columns: list[ColumnMeta], path: str) -> None:
    """Write enriched definitions to a structured plain-text report."""
    all_cols_flat = columns if columns else [c for t in tables for c in t.columns]
    # Build a lookup so we can find columns that aren't attached to a table object
    col_lookup: dict[tuple, list[ColumnMeta]] = {}
    for c in all_cols_flat:
        col_lookup.setdefault((c.table_schema, c.table_name), []).append(c)

    sep80 = "=" * 80
    sep40 = "-" * 40
    lines: list[str] = []

    lines.append("DATA DEFINITION REPORT")
    lines.append(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Tables    : {len(tables)}")
    lines.append(f"Columns   : {len(all_cols_flat)}")
    lines.append("")

    for t in tables:
        lines.append(sep80)
        lines.append(f"TABLE: {t.table_schema}.{t.table_name}")
        lines.append(sep80)
        lines.append(f"  catalog             : {t.table_catalog}")
        lines.append(f"  type                : {t.table_type}")
        lines.append(f"  business_name       : {t.business_name}")
        lines.append(f"  data_domain         : {t.data_domain}")
        lines.append(f"  primary_use_case    : {t.primary_use_case}")
        lines.append(f"  business_definition : {t.business_definition}")
        lines.append(f"  existing_description: {t.existing_description}")

        table_cols = t.columns or col_lookup.get((t.table_schema, t.table_name), [])
        if table_cols:
            lines.append("")
            lines.append(f"  COLUMNS ({len(table_cols)}):")
            for c in table_cols:
                lines.append(f"  {sep40}")
                lines.append(f"  COLUMN: {c.column_name}")
                lines.append(f"    data_type           : {c.data_type}")
                lines.append(f"    nullable            : {c.is_nullable}")
                lines.append(f"    business_term       : {c.business_term}")
                lines.append(f"    data_classification : {c.data_classification}")
                lines.append(f"    business_definition : {c.business_definition}")
                lines.append(f"    existing_description: {c.existing_description}")
                lines.append(f"    sample_values       : {c.sample_values}")
            lines.append(f"  {sep40}")

        lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nOutput saved → {path}")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

_api_key = os.environ.get("ANTHROPIC_API_KEY")
if not _api_key:
    print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
    print("  Set it with: export ANTHROPIC_API_KEY=your-key-here")
    print("  Or add it to a .env file in the project directory.")
    sys.exit(1)

client = anthropic.Anthropic(api_key=_api_key)
MODEL = "claude-sonnet-4-6"


def _call_claude(system: str, user: str, max_tokens: int = 4096) -> str:
    """Single Claude API call, returns text."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


# ── Agent 1: Table Definition Agent ─────────────────────────────────────────

SYSTEM_TABLE = """You are a senior data steward and business analyst. Your job is to write
clear, concise business definitions for database tables and views that non-technical
business users can understand.

For each table you will produce:
- business_name: a friendly, human-readable name (Title Case, 3-6 words)
- data_domain: the high-level business domain (e.g. Sales, Finance, Supply Chain, Customer, HR, Operations)
- primary_use_case: one sentence describing what business question this table answers
- business_definition: 2-4 sentences describing what the table contains, why it exists, and who uses it

Rules:
- Use plain English — avoid technical jargon
- Infer meaning from schema names, table names, and any existing descriptions
- Return ONLY a JSON array, no markdown fences, no extra text
"""


def generate_table_definitions(tables: list[TableMeta], batch_size: int = 10) -> None:
    """Fills business_name, data_domain, primary_use_case, business_definition on each table."""
    batches = [tables[i:i+batch_size] for i in range(0, len(tables), batch_size)]
    total = len(tables)
    done = 0

    for batch in batches:
        payload = []
        for t in batch:
            payload.append({
                "id": f"{t.table_schema}.{t.table_name}",
                "table_catalog": t.table_catalog,
                "table_schema": t.table_schema,
                "table_name": t.table_name,
                "table_type": t.table_type,
                "existing_description": t.existing_description,
                "notes": t.notes,
                "sample_column_names": [c.column_name for c in t.columns[:15]],
            })

        prompt = (
            "Generate business definitions for the following tables.\n"
            "Return a JSON array where each element has keys: "
            "id, business_name, data_domain, primary_use_case, business_definition.\n\n"
            f"{json.dumps(payload, indent=2)}"
        )

        raw = _call_claude(SYSTEM_TABLE, prompt, max_tokens=8192)
        try:
            results = json.loads(raw)
        except json.JSONDecodeError:
            # try to salvage partial JSON
            start = raw.find("[")
            end = raw.rfind("]") + 1
            results = json.loads(raw[start:end]) if start >= 0 else []

        result_map = {r["id"]: r for r in results}
        for t in batch:
            key = f"{t.table_schema}.{t.table_name}"
            if key in result_map:
                r = result_map[key]
                t.business_name = r.get("business_name", "")
                t.data_domain = r.get("data_domain", "")
                t.primary_use_case = r.get("primary_use_case", "")
                t.business_definition = r.get("business_definition", "")
            done += 1

        print(f"  Tables: {done}/{total} defined", end="\r")
        time.sleep(0.3)  # gentle rate-limit buffer

    print(f"  Tables: {total}/{total} defined ✓")


# ── Agent 2: Column Definition Agent ────────────────────────────────────────

SYSTEM_COLUMN = """You are a senior data steward. Your job is to write clear, concise
business definitions for database columns that non-technical users can understand.

For each column produce:
- business_term: a friendly display name (Title Case, 1-4 words)
- data_classification: one of Public | Internal | Confidential | PII | Sensitive
- business_definition: 1-2 sentences explaining what the column represents in business terms

Rules:
- Use plain English — no SQL jargon
- Infer meaning from the column name, data type, nullable flag, and sample values
- Consider the parent table context when explaining what the column means
- Return ONLY a JSON array, no markdown fences
"""


def generate_column_definitions(tables: list[TableMeta], batch_size: int = 30) -> None:
    """Fills business_term, data_classification, business_definition on each column."""
    # Flatten all columns, grouped by table for context
    all_tasks = []
    for t in tables:
        for c in t.columns:
            all_tasks.append((t, c))

    batches = [all_tasks[i:i+batch_size] for i in range(0, len(all_tasks), batch_size)]
    total = len(all_tasks)
    done = 0

    for batch in batches:
        payload = []
        for t, c in batch:
            payload.append({
                "id": f"{c.table_schema}.{c.table_name}.{c.column_name}",
                "table_context": t.business_definition or t.existing_description or t.table_name,
                "table_schema": c.table_schema,
                "table_name": c.table_name,
                "column_name": c.column_name,
                "data_type": c.data_type,
                "is_nullable": c.is_nullable,
                "existing_description": c.existing_description,
                "sample_values": c.sample_values,
            })

        prompt = (
            "Generate business definitions for the following columns.\n"
            "Return a JSON array where each element has keys: "
            "id, business_term, data_classification, business_definition.\n\n"
            f"{json.dumps(payload, indent=2)}"
        )

        raw = _call_claude(SYSTEM_COLUMN, prompt, max_tokens=8192)
        try:
            results = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("[")
            end = raw.rfind("]") + 1
            results = json.loads(raw[start:end]) if start >= 0 else []

        result_map = {r["id"]: r for r in results}
        for t, c in batch:
            key = f"{c.table_schema}.{c.table_name}.{c.column_name}"
            if key in result_map:
                r = result_map[key]
                c.business_term = r.get("business_term", "")
                c.data_classification = r.get("data_classification", "")
                c.business_definition = r.get("business_definition", "")
            done += 1

        print(f"  Columns: {done}/{total} defined", end="\r")
        time.sleep(0.3)

    print(f"  Columns: {total}/{total} defined ✓")


# ── Agent 3: Quality Review Agent ───────────────────────────────────────────

SYSTEM_REVIEW = """You are a data governance quality reviewer. Review AI-generated business
definitions and flag any that need improvement.

For each item assess:
- clarity_score: 1-5 (5 = perfectly clear to a business user)
- issues: brief list of problems (or empty list if none)
- suggestion: improved definition if score < 4, else empty string

Return ONLY a JSON array with keys: id, clarity_score, issues, suggestion.
"""


def quality_review(tables: list[TableMeta], threshold: int = 3) -> None:
    """Reviews table definitions and applies suggestions for low-scoring ones."""
    payload = [
        {
            "id": f"{t.table_schema}.{t.table_name}",
            "business_name": t.business_name,
            "business_definition": t.business_definition,
            "primary_use_case": t.primary_use_case,
        }
        for t in tables if t.business_definition
    ]
    if not payload:
        return

    prompt = (
        "Review these business definitions for clarity and accuracy.\n"
        f"{json.dumps(payload, indent=2)}"
    )

    raw = _call_claude(SYSTEM_REVIEW, prompt, max_tokens=8192)
    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        results = json.loads(raw[start:end]) if start >= 0 else []

    improved = 0
    result_map = {r["id"]: r for r in results}
    for t in tables:
        key = f"{t.table_schema}.{t.table_name}"
        if key in result_map:
            r = result_map[key]
            if r.get("clarity_score", 5) <= threshold and r.get("suggestion"):
                t.business_definition = r["suggestion"]
                improved += 1

    print(f"  Quality review: {improved} definitions improved ✓")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Data Definition Agent — AI-powered business definitions for tables & columns"
    )
    parser.add_argument("input", nargs="?", help="Input .txt file path")
    parser.add_argument("--output", "-o", help="Output .txt file path (default: <input>_definitions.txt)")
    parser.add_argument("--batch", type=int, default=10, help="Batch size for API calls (default: 10)")
    parser.add_argument("--no-columns", action="store_true", help="Skip column-level definitions")
    parser.add_argument("--no-review", action="store_true", help="Skip quality review pass")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N tables (0 = all)")
    parser.add_argument("--create-template", action="store_true", help="Create a blank input template and exit")
    args = parser.parse_args()

    if args.create_template:
        create_template("data_definition_template.txt")
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    if not Path(args.input).exists():
        print(f"ERROR: File not found: {args.input}")
        sys.exit(1)

    output_path = args.output or Path(args.input).stem + "_definitions.txt"

    print(f"\n{'='*60}")
    print(" Data Definition Agent")
    print(f"{'='*60}")
    print(f" Input : {args.input}")
    print(f" Output: {output_path}")
    print(f"{'='*60}\n")

    # ── Step 1: Load ──────────────────────────────────────────────────────
    print("[1/4] Loading input...")
    tables, columns = load_input(args.input)
    flat_columns = columns if columns else [c for t in tables for c in t.columns]
    print(f"  Loaded {len(tables)} tables, {len(flat_columns)} columns")

    if args.limit > 0:
        tables = tables[:args.limit]
        flat_columns = [c for t in tables for c in t.columns]
        print(f"  Limiting to {len(tables)} tables (--limit {args.limit})")

    if not tables:
        print("ERROR: No tables found in input file. Run --create-template to generate an example input.txt.")
        sys.exit(1)

    # ── Step 2: Table definitions ─────────────────────────────────────────
    print("\n[2/4] Generating table business definitions...")
    generate_table_definitions(tables, batch_size=args.batch)

    # ── Step 3: Column definitions ────────────────────────────────────────
    if not args.no_columns and flat_columns:
        print(f"\n[3/4] Generating column business definitions ({len(flat_columns)} columns)...")
        generate_column_definitions(tables, batch_size=30)
    else:
        print("\n[3/4] Skipping column definitions")

    # ── Step 4: Quality review ────────────────────────────────────────────
    if not args.no_review:
        print("\n[4/4] Running quality review...")
        quality_review(tables)
    else:
        print("\n[4/4] Skipping quality review")

    # ── Save output ───────────────────────────────────────────────────────
    all_cols = columns if columns else [c for t in tables for c in t.columns]
    save_output(tables, all_cols, output_path)

    print(f"\nDone! {len(tables)} table definitions and {len(all_cols)} column definitions generated.")


if __name__ == "__main__":
    main()
