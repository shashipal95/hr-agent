"""
app/api_server.py
=================
FastAPI backend for HR Assistant.
Deployed on Railway (backend) + Vercel/Next.js (frontend).

Endpoints:
  POST /api/chat            — non-streaming chat
  POST /api/chat/stream     — SSE streaming chat
  GET  /api/employees       — paginated employee list
  GET  /api/analytics       — workforce analytics
  GET  /api/workforce       — company-wide KPIs
  GET  /api/audit           — audit log
  GET  /api/departments     — department list
  GET  /health              — health check (Railway pings this)
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT      = Path(__file__).parent.parent
DB_PATH    = _ROOT / "hr.db"
AUDIT_PATH = _ROOT / "audit.db"

# ── CORS ──────────────────────────────────────────────────────────────────────
# Set ALLOWED_ORIGINS in Railway environment variables as comma-separated URLs:
#   https://yourportfolio.vercel.app,https://www.yourcustomdomain.com
# Defaults to localhost:3000 for local development.
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# ── Agent singleton ───────────────────────────────────────────────────────────
_mcp_client = None
_agent      = None


_agent_error = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise MCP agent on startup, clean up on shutdown."""
    global _mcp_client, _agent, _agent_error
    try:
        from core.hr_agent import MCPClient, HRAgent
        
        _mcp_client = MCPClient()
        await _mcp_client.connect()
        _agent = HRAgent(_mcp_client, user_id="system")
        print("✅ HR Agent ready.")
    except Exception as exc:
        import traceback
        _agent_error = traceback.format_exc()
        print(f"⚠️  HR Agent unavailable: {exc}")
    yield
    if _mcp_client:
        try:
            await _mcp_client.disconnect()
        except Exception:
            pass


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="HR Assistant API",
    version="2.0.0",
    description="MCP-powered HR chatbot — Groq + Pinecone",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.(vercel\.app|up\.railway\.app)",  # Vercel + Railway previews
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Type"],
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _hr_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _audit_db() -> sqlite3.Connection:
    conn = sqlite3.connect(AUDIT_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    user_id: str = "anonymous"


# ── System endpoints ──────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
def root():
    return {"message": "HR Assistant API is running. See /docs for endpoints."}


@app.get("/health", tags=["System"])
def health():
    """Railway health check — must return 2xx for service to be marked healthy."""
    return {
        "status":      "ok",
        "hr_db":       DB_PATH.exists(),
        "audit_db":    AUDIT_PATH.exists(),
        "agent_ready": _agent is not None,
        "agent_error": _agent_error,
    }


# ── Chat ──────────────────────────────────────────────────────────────────────

@app.post("/api/chat", tags=["Chat"])
async def chat(req: ChatRequest):
    """Non-streaming chat — returns the complete answer in one response."""
    if _agent is None:
        raise HTTPException(503, "HR Agent unavailable. Check server logs.")
    try:
        _agent.user_id = req.user_id
        answer = await _agent.ask(req.message, req.history)
        return {"answer": answer}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/chat/stream", tags=["Chat"])
async def chat_stream(req: ChatRequest):
    """
    SSE streaming chat.

    Each event: {"type": "token"|"status"|"done"|"error", "data": "..."}
      status — progress update shown while tools run
      token  — one LLM token (append to your chat bubble)
      done   — stream finished, close EventSource
      error  — something went wrong
    """
    if _agent is None:
        async def _err():
            yield _sse({"type": "error", "data": "HR Agent unavailable."})
            yield _sse({"type": "done",  "data": ""})
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def generate():
        try:
            _agent.user_id = req.user_id
            yield _sse({"type": "status", "data": "Querying HR database…"})
            async for token in _agent.ask_stream(req.message, req.history):
                if token == "\x00TOOLS_DONE\x00":
                    yield _sse({"type": "status", "data": "Generating answer…"})
                    continue
                yield _sse({"type": "token", "data": token})
            yield _sse({"type": "done", "data": ""})
        except Exception as exc:
            yield _sse({"type": "error", "data": str(exc)})
            yield _sse({"type": "done",  "data": ""})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# ── Employees ─────────────────────────────────────────────────────────────────

@app.get("/api/employees", tags=["Data"])
def get_employees(
    name:       str = Query(None),
    department: str = Query(None),
    status:     str = Query(None),
    limit:      int = Query(50, ge=1, le=200),
    offset:     int = Query(0,  ge=0),
):
    if not DB_PATH.exists():
        return {"employees": [], "total_filtered": 0, "total_all": 0}

    clauses, params = [], []
    if name:
        clauses.append('"Employee Name" LIKE ?'); params.append(f"%{name}%")
    if department and department != "All":
        clauses.append("Department = ?"); params.append(department)
    if status and status != "All":
        clauses.append('"Employment Status" = ?'); params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _hr_db() as conn:
        total_filtered = conn.execute(
            f"SELECT COUNT(*) FROM employees {where}", params
        ).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"""SELECT "Employee Name", Department, Position,
                       "Employment Status", "Manager Name",
                       "Pay Rate", "Performance Score", Sex, Age
                FROM employees {where}
                ORDER BY "Employee Name"
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()]
        total_all = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]

    return {"employees": rows, "total_filtered": total_filtered, "total_all": total_all}


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/api/analytics", tags=["Data"])
def get_analytics():
    if not DB_PATH.exists():
        return {"departments": [], "performance": [], "status": [], "gender": []}

    with _hr_db() as conn:
        dept = [dict(r) for r in conn.execute("""
            SELECT
                Department,
                COUNT(*) AS headcount,
                ROUND(AVG(CAST("Pay Rate" AS REAL)), 2) AS avg_pay,
                SUM(CASE WHEN "Employment Status"='Active'            THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN "Employment Status"!='Active'           THEN 1 ELSE 0 END) AS terminated,
                SUM(CASE WHEN "Performance Score"='Exceeds'           THEN 1 ELSE 0 END) AS exceeds,
                SUM(CASE WHEN "Performance Score"='Fully Meets'       THEN 1 ELSE 0 END) AS fully_meets,
                SUM(CASE WHEN "Performance Score"='Needs Improvement' THEN 1 ELSE 0 END) AS needs_improvement,
                SUM(CASE WHEN "Performance Score"='PIP'               THEN 1 ELSE 0 END) AS pip
            FROM employees GROUP BY Department ORDER BY headcount DESC
        """).fetchall()]
        perf = [dict(r) for r in conn.execute(
            'SELECT "Performance Score" AS score, COUNT(*) AS count '
            'FROM employees GROUP BY "Performance Score"'
        ).fetchall()]
        status = [dict(r) for r in conn.execute(
            'SELECT "Employment Status" AS status, COUNT(*) AS count '
            'FROM employees GROUP BY "Employment Status"'
        ).fetchall()]
        gender = [dict(r) for r in conn.execute(
            'SELECT Sex AS gender, COUNT(*) AS count FROM employees GROUP BY Sex'
        ).fetchall()]

    return {"departments": dept, "performance": perf, "status": status, "gender": gender}


# ── Workforce ─────────────────────────────────────────────────────────────────

@app.get("/api/workforce", tags=["Data"])
def get_workforce():
    if not DB_PATH.exists():
        return {}
    with _hr_db() as conn:
        row = dict(conn.execute("""
            SELECT
                COUNT(*)  AS total,
                SUM(CASE WHEN "Employment Status"='Active'        THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN "Employment Status"!='Active'       THEN 1 ELSE 0 END) AS terminated,
                ROUND(AVG(CAST("Pay Rate" AS REAL)), 2) AS avg_pay,
                ROUND(MIN(CAST("Pay Rate" AS REAL)), 2) AS min_pay,
                ROUND(MAX(CAST("Pay Rate" AS REAL)), 2) AS max_pay,
                COUNT(DISTINCT Department)              AS departments,
                COUNT(DISTINCT Position)                AS positions,
                SUM(CASE WHEN "Performance Score"='Exceeds' THEN 1 ELSE 0 END) AS top_performers,
                SUM(CASE WHEN Sex='F' THEN 1 ELSE 0 END) AS female,
                SUM(CASE WHEN Sex='M' THEN 1 ELSE 0 END) AS male
            FROM employees
        """).fetchone())
    return row


# ── Departments ───────────────────────────────────────────────────────────────

@app.get("/api/departments", tags=["Data"])
def get_departments():
    if not DB_PATH.exists():
        return {"departments": []}
    with _hr_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT Department FROM employees "
            "WHERE Department IS NOT NULL ORDER BY Department"
        ).fetchall()
    return {"departments": [r[0] for r in rows]}


# ── Audit ─────────────────────────────────────────────────────────────────────

@app.get("/api/audit", tags=["Data"])
def get_audit(
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0,  ge=0),
):
    if not AUDIT_PATH.exists():
        return {"entries": [], "total": 0}
    try:
        with _audit_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            rows  = [dict(r) for r in conn.execute(
                "SELECT id, ts, tool, query, user_id, result_rows, status "
                "FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
                [limit, offset],
            ).fetchall()]
        return {"entries": rows, "total": total}
    except Exception:
        return {"entries": [], "total": 0}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.api_server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("APP_ENV") == "development",
    )
