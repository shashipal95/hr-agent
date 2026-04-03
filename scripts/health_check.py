"""
scripts/health_check.py
=======================
Verify the project is correctly configured before deploying.

Usage:
    python scripts/health_check.py
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")


def section(title: str):
    print(f"\n{'─' * 50}\n  {title}\n{'─' * 50}")


def ok(label: str, msg: str = ""):
    print(f"  ✅  {label}" + (f" {msg}" if msg else ""))


def fail(label: str, fix: str = ""):
    print(f"  ❌  {label}" + (f"\n       → {fix}" if fix else ""))


def info(label: str):
    print(f"  ℹ️   {label}")


# ── Packages ──────────────────────────────────────────────────────────────────
section("Python Packages")

PACKAGES = {
    "mcp":                      "pip install mcp",
    "fastapi":                  "pip install fastapi uvicorn",
    "langchain_groq":           "pip install langchain-groq",
    "langchain_google_genai":   "pip install langchain-google-genai",
    "pinecone":                 "pip install 'pinecone<8.0.0'",
    "langchain_pinecone":       "pip install langchain-pinecone",
    "langchain_community":      "pip install langchain-community",
    "langchain_text_splitters": "pip install langchain-text-splitters",
    "pandas":                   "pip install pandas",
    "pypdf":                    "pip install pypdf",
}

all_ok = True

for pkg, install in PACKAGES.items():
    try:
        importlib.import_module(pkg)
        ok(pkg)
    except ImportError:
        fail(pkg, install)
        all_ok = False

if not all_ok:
    print("\n  Run: pip install -r requirements.txt")


# ── API Keys ──────────────────────────────────────────────────────────────────
section("API Keys (.env)")

for key, hint in {
    "GROQ_API_KEY":    "https://console.groq.com",
    "GOOGLE_API_KEY":  "https://aistudio.google.com",
    "PINECONE_API_KEY":"https://pinecone.io",
    "PINECONE_ENV":    "e.g. us-east-1",
    "ALLOWED_ORIGINS": "e.g. https://yourportfolio.vercel.app",
}.items():
    val = os.getenv(key, "")
    if val:
        ok(f"{key} = {val[:10]}…")
    else:
        fail(f"{key} missing", hint)


# ── Core files ────────────────────────────────────────────────────────────────
section("Core Files")

for f in [
    "core/mcp_hr_server.py",
    "core/hr_agent.py",
    "app/api_server.py",
    "scripts/ingest_employees.py",
    "scripts/ingest_policies.py",
    "Procfile",
    "railway.toml",
    "runtime.txt"
]:
    path = _ROOT / f

    if path.exists():
        ok(f)
    else:
        fail(f, "File missing")


# ── Data & DBs ────────────────────────────────────────────────────────────────
section("Data & Databases")

data_dir = _ROOT / "data"

if data_dir.exists():
    ok("data/ folder")
else:
    fail("data/ folder", "mkdir data")

# employees.csv
employees_file = data_dir / "employees.csv"
if employees_file.exists():
    ok("data/employees.csv")
else:
    fail("data/employees.csv", "Place your HR CSV here")

# PDFs
pdfs = list(data_dir.glob("*.pdf")) if data_dir.exists() else []
if pdfs:
    ok(f"Policy PDFs ({len(pdfs)} found)")
else:
    info("No PDFs — policy RAG unavailable (employee queries still work)")

# hr.db
db = _ROOT / "hr.db"
if db.exists():
    try:
        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        ok(f"hr.db — {count} employees")
    except Exception as exc:
        fail(f"hr.db error: {exc}", "Re-run: python scripts/ingest_employees.py")
else:
    fail("hr.db not found", "Run: python scripts/ingest_employees.py")

# audit.db
audit = _ROOT / "audit.db"
if audit.exists():
    ok("audit.db exists")
else:
    info("audit.db — auto-created on first query")


# ── Deployment ────────────────────────────────────────────────────────────────
section("Deployment Readiness (Railway)")

procfile = _ROOT / "Procfile"
if procfile.exists():
    ok(f"Procfile: {procfile.read_text().strip()}")
else:
    fail("Procfile missing")

gitignore = _ROOT / ".gitignore"
if gitignore.exists():
    content = gitignore.read_text()
    if ".env" in content:
        ok(".gitignore — .env excluded ✓")
    else:
        fail(".gitignore — .env NOT excluded!", "Add .env to .gitignore")
else:
    fail(".gitignore missing")


# ── Summary ───────────────────────────────────────────────────────────────────
section("Next Steps")

print("""
  Local dev:
    uvicorn app.api_server:app --reload --port 8000
    Open: http://localhost:8000/docs

  Deploy to Railway (free, no card):
    1. git push to GitHub
    2. railway.com → New App → connect GitHub repo
    3. Set env vars: GROQ_API_KEY, GOOGLE_API_KEY, PINECONE_API_KEY,
                     PINECONE_ENV, ALLOWED_ORIGINS
    4. Auto-deploys on every git push ✓

  Connect Next.js (Vercel):
    .env.local → NEXT_PUBLIC_HR_API_URL=https://your-app.railway.app
    Use: nextjs-integration/lib/hr-api.ts
""")