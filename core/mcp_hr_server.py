"""
core/mcp_hr_server.py
=====================
MCP HR Server — exposes all HR data tools via the Model Context Protocol.
Run as a subprocess; the HR Agent communicates via stdio.

Tools:
  search_employees         — Flexible employee search (SQL-backed)
  get_employee_details     — Full profile for one employee
  get_department_analytics — Headcount, avg pay, performance by dept
  get_org_chart            — Manager → direct-reports tree
  get_workforce_summary    — Company-wide KPIs
  search_hr_policy         — RAG over policy PDF knowledge base (Pinecone)
  log_audit_event          — Write to immutable audit trail
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# CRITICAL FIX for MCP servers: 
# Silence ALL standard output before imports so that LangChain/Pinecone don't corrupt JSON-RPC 
# stream with unwanted logs. We restore it in the main function.
_original_stdout = sys.stdout
sys.stdout = open(os.devnull, "w")

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("mcp_hr_server")

# ── DB Paths ──────────────────────────────────────────────────────────────────
# Support running from project root or from core/ subdirectory
_BASE = Path(__file__).parent.parent
DB_PATH    = _BASE / "hr.db"
AUDIT_PATH = _BASE / "audit.db"

# ── Pinecone + Gemini RAG Setup ───────────────────────────────────────────────
RAG_AVAILABLE = False

try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    from pinecone import Pinecone

    _embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=os.getenv("GOOGLE_API_KEY"),
        output_dimensionality=768,
    )
    _pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
    _policy_index = _pc.Index(os.getenv("PINECONE_POLICY_INDEX", "hr-policy-index"))
    RAG_AVAILABLE = True
    log.info("✅ Pinecone RAG initialised.")
except Exception as exc:
    log.warning("⚠️  RAG unavailable: %s", exc)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _hr_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _audit_conn():
    conn = sqlite3.connect(AUDIT_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            tool        TEXT    NOT NULL,
            query       TEXT,
            user_id     TEXT    DEFAULT 'system',
            result_rows INTEGER DEFAULT 0,
            status      TEXT    DEFAULT 'ok'
        )
    """)
    conn.commit()
    return conn


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _fmt(rows: list[dict]) -> str:
    if not rows:
        return "No records found."
    return json.dumps(rows, indent=2, default=str)


# ── Tool implementations ──────────────────────────────────────────────────────

def search_employees(
    name: str | None = None,
    department: str | None = None,
    position: str | None = None,
    manager: str | None = None,
    status: str | None = None,
    performance: str | None = None,
    limit: int = 20,
) -> str:
    """Filter employees by any combination of fields."""
    clauses, params = [], []
    if name:
        clauses.append('"Employee Name" LIKE ?'); params.append(f"%{name}%")
    if department:
        clauses.append("Department LIKE ?"); params.append(f"%{department}%")
    if position:
        clauses.append("Position LIKE ?"); params.append(f"%{position}%")
    if manager:
        clauses.append('"Manager Name" LIKE ?'); params.append(f"%{manager}%")
    if status:
        clauses.append('"Employment Status" = ?'); params.append(status)
    if performance:
        clauses.append('"Performance Score" = ?'); params.append(performance)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT "Employee Name", Department, Position, "Employment Status",
               "Manager Name", "Pay Rate", "Performance Score", Sex, Age, "Marital Status"
        FROM employees {where}
        ORDER BY "Employee Name"
        LIMIT ?
    """
    with _hr_conn() as conn:
        rows = _rows(conn.execute(sql, params + [limit]).fetchall())
    return _fmt(rows)


def get_employee_details(name: str) -> str:
    """Return the full HR profile for an employee by name (partial match)."""
    sql = 'SELECT * FROM employees WHERE "Employee Name" LIKE ?'
    with _hr_conn() as conn:
        rows = _rows(conn.execute(sql, [f"%{name}%"]).fetchall())
    return _fmt(rows)


def get_department_analytics(department: str | None = None) -> str:
    """Headcount, average pay, and performance breakdown per department."""
    where = "WHERE Department = ?" if department else ""
    params = [department] if department else []
    sql = f"""
        SELECT
            Department,
            COUNT(*) AS headcount,
            ROUND(AVG(CAST("Pay Rate" AS REAL)), 2) AS avg_pay,
            SUM(CASE WHEN "Employment Status" = 'Active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN "Employment Status" != 'Active' THEN 1 ELSE 0 END) AS terminated,
            SUM(CASE WHEN "Performance Score" = 'Exceeds'          THEN 1 ELSE 0 END) AS exceeds,
            SUM(CASE WHEN "Performance Score" = 'Fully Meets'      THEN 1 ELSE 0 END) AS fully_meets,
            SUM(CASE WHEN "Performance Score" = 'Needs Improvement' THEN 1 ELSE 0 END) AS needs_improvement,
            SUM(CASE WHEN "Performance Score" = 'PIP'              THEN 1 ELSE 0 END) AS pip
        FROM employees {where}
        GROUP BY Department
        ORDER BY headcount DESC
    """
    with _hr_conn() as conn:
        rows = _rows(conn.execute(sql, params).fetchall())
    return _fmt(rows)


def get_org_chart(manager_name: str | None = None) -> str:
    """Return manager → direct-reports hierarchy."""
    if manager_name:
        sql = """
            SELECT "Manager Name", "Employee Name", Position, Department
            FROM employees
            WHERE "Manager Name" LIKE ?
            ORDER BY "Manager Name", "Employee Name"
        """
        params = [f"%{manager_name}%"]
    else:
        sql = """
            SELECT "Manager Name", "Employee Name", Position, Department
            FROM employees
            WHERE "Manager Name" IS NOT NULL
            ORDER BY "Manager Name", "Employee Name"
            LIMIT 100
        """
        params = []

    with _hr_conn() as conn:
        rows = _rows(conn.execute(sql, params).fetchall())

    # Group by manager
    tree: dict[str, list] = {}
    for r in rows:
        mgr = r["Manager Name"] or "Unknown"
        tree.setdefault(mgr, []).append(
            {"name": r["Employee Name"], "position": r["Position"], "department": r["Department"]}
        )
    return json.dumps(tree, indent=2)


def get_workforce_summary() -> str:
    """Company-wide KPIs: headcount, active rate, avg pay, gender split, top performers."""
    sql = """
        SELECT
            COUNT(*)  AS total_employees,
            SUM(CASE WHEN "Employment Status" = 'Active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN "Employment Status" != 'Active' THEN 1 ELSE 0 END) AS terminated,
            ROUND(AVG(CAST("Pay Rate" AS REAL)), 2) AS avg_pay_rate,
            ROUND(MIN(CAST("Pay Rate" AS REAL)), 2) AS min_pay_rate,
            ROUND(MAX(CAST("Pay Rate" AS REAL)), 2) AS max_pay_rate,
            COUNT(DISTINCT Department) AS departments,
            COUNT(DISTINCT Position)   AS unique_positions,
            SUM(CASE WHEN "Performance Score" = 'Exceeds' THEN 1 ELSE 0 END) AS top_performers,
            SUM(CASE WHEN Sex = 'F' THEN 1 ELSE 0 END) AS female,
            SUM(CASE WHEN Sex = 'M' THEN 1 ELSE 0 END) AS male
        FROM employees
    """
    with _hr_conn() as conn:
        row = dict(conn.execute(sql).fetchone())
    return json.dumps(row, indent=2)


def search_hr_policy(query: str, k: int = 4) -> str:
    """Semantic search over HR policy documents stored in Pinecone."""
    if not RAG_AVAILABLE:
        return (
            "⚠️ Policy search is unavailable. "
            "Ensure GOOGLE_API_KEY and PINECONE_API_KEY are set and "
            "policies have been ingested via `python scripts/ingest_policies.py`."
        )
    try:
        vector = _embeddings.embed_query(query)
        results = _policy_index.query(vector=vector, top_k=k, include_metadata=True)
        matches = results.get("matches", [])
        if not matches:
            return "No relevant policy sections found."
        sections = []
        for i, m in enumerate(matches, 1):
            meta = m.get("metadata", {})
            sections.append(f"[{i}] Source: {meta.get('source', 'Policy')}\n{meta.get('text', '')}")
        return "\n\n---\n\n".join(sections)
    except Exception as exc:
        log.error("Policy search error: %s", exc)
        return f"Policy search error: {exc}"


def log_audit_event(
    tool: str,
    query: str,
    user_id: str = "system",
    result_rows: int = 0,
    status: str = "ok",
) -> str:
    """Write an immutable audit entry to audit.db."""
    try:
        with _audit_conn() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (ts, tool, query, user_id, result_rows, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [datetime.now(timezone.utc).isoformat(), tool, query, user_id, result_rows, status],
            )
            conn.commit()
        return f"Audit logged (id={cur.lastrowid})"
    except Exception as exc:
        log.error("Audit log error: %s", exc)
        return f"Audit log error: {exc}"


# ── MCP Server definition ─────────────────────────────────────────────────────

server = Server("hr-assistant")

TOOLS: list[Tool] = [
    Tool(
        name="search_employees",
        description=(
            "Search employees by name, department, position, manager, status, "
            "or performance score. All filters are optional."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name":        {"type": "string",  "description": "Partial employee name"},
                "department":  {"type": "string",  "description": "Department name"},
                "position":    {"type": "string",  "description": "Job title / position"},
                "manager":     {"type": "string",  "description": "Manager name"},
                "status":      {"type": "string",  "description": "Employment status, e.g. Active"},
                "performance": {"type": "string",  "description": "Performance score, e.g. Exceeds"},
                "limit":       {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    ),
    Tool(
        name="get_employee_details",
        description="Return the full HR profile for a specific employee.",
        inputSchema={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Full or partial employee name"},
            },
        },
    ),
    Tool(
        name="get_department_analytics",
        description="Headcount, average pay, and performance breakdown per department.",
        inputSchema={
            "type": "object",
            "properties": {
                "department": {"type": "string", "description": "Filter to one department (optional)"},
            },
        },
    ),
    Tool(
        name="get_org_chart",
        description="Return manager → direct-reports hierarchy.",
        inputSchema={
            "type": "object",
            "properties": {
                "manager_name": {"type": "string", "description": "Manager name to filter (optional)"},
            },
        },
    ),
    Tool(
        name="get_workforce_summary",
        description="Company-wide KPIs: headcount, active rate, avg pay, gender split, top performers.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="search_hr_policy",
        description="Semantic search over HR policy documents (leave, benefits, conduct, etc.).",
        inputSchema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string",  "description": "Natural-language policy question"},
                "k":     {"type": "integer", "description": "Number of policy chunks to return (default 4)"},
            },
        },
    ),
    Tool(
        name="log_audit_event",
        description="Write an audit entry to the immutable audit trail.",
        inputSchema={
            "type": "object",
            "required": ["tool", "query"],
            "properties": {
                "tool":        {"type": "string",  "description": "Tool name that was called"},
                "query":       {"type": "string",  "description": "Query / question asked"},
                "user_id":     {"type": "string",  "description": "User identifier"},
                "result_rows": {"type": "integer", "description": "Number of rows returned"},
                "status":      {"type": "string",  "description": "ok | error"},
            },
        },
    ),
]

TOOL_FN_MAP = {
    "search_employees":         search_employees,
    "get_employee_details":     get_employee_details,
    "get_department_analytics": get_department_analytics,
    "get_org_chart":            get_org_chart,
    "get_workforce_summary":    get_workforce_summary,
    "search_hr_policy":         search_hr_policy,
    "log_audit_event":          log_audit_event,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    fn = TOOL_FN_MAP.get(name)
    if fn is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = fn(**arguments)
    except Exception as exc:
        log.error("Tool %s failed: %s", name, exc)
        result = f"Tool error: {exc}"
    return [TextContent(type="text", text=str(result))]


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main():
    # Restore stdout exactly when starting the true JSON-RPC stream handler!
    sys.stdout = _original_stdout
    async with stdio_server() as (read, write):
        try:
            await server.run(read, write)
        except Exception:
            with open("/tmp/mcp_fatal_error.txt", "w") as err_f:
                err_f.write(traceback.format_exc())
            raise

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(_main())
    except Exception:
        sys.stdout = _original_stdout
        with open("/tmp/mcp_fatal_error.txt", "w") as err_f:
            err_f.write(traceback.format_exc())
        raise
