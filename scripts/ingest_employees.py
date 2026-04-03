"""
scripts/ingest_employees.py
===========================
Load employees.csv into SQLite (hr.db) and optionally Pinecone for semantic search.

Usage:
    python scripts/ingest_employees.py
    python scripts/ingest_employees.py --skip-pinecone   # SQLite only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# ── Resolve project root ──────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

CSV_PATH = _ROOT / "data" / "employees.csv"
DB_PATH  = _ROOT / "hr.db"


def load_to_sqlite():
    if not CSV_PATH.exists():
        print(f"❌ {CSV_PATH} not found.")
        print("   Place your employee CSV at data/employees.csv and re-run.")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH)
    df.columns = [c.strip() for c in df.columns]
    print(f"✅ Loaded {len(df)} rows from {CSV_PATH.name}")

    with sqlite3.connect(DB_PATH) as conn:
        df.to_sql("employees", conn, if_exists="replace", index=False)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_name ON employees ("Employee Name")')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_dept ON employees (Department)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_mgr  ON employees ("Manager Name")')
        conn.commit()

    print(f"✅ hr.db ready — {len(df)} employees")
    return df


def upload_to_pinecone(df: pd.DataFrame):
    import os
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    from pinecone import Pinecone, ServerlessSpec

    pinecone_key = os.getenv("PINECONE_API_KEY")
    google_key   = os.getenv("GOOGLE_API_KEY")
    pinecone_env = os.getenv("PINECONE_ENV", "us-east-1")
    index_name   = os.getenv("PINECONE_EMPLOYEE_INDEX", "hr-index")

    if not pinecone_key or not google_key:
        print("⚠️  Skipping Pinecone upload — PINECONE_API_KEY or GOOGLE_API_KEY not set.")
        return

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=google_key,
        output_dimensionality=768,
    )

    pc = Pinecone(api_key=pinecone_key)
    existing = [i["name"] for i in pc.list_indexes()]
    if index_name not in existing:
        print(f"Creating Pinecone index '{index_name}'…")
        pc.create_index(
            name=index_name,
            dimension=768,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region=pinecone_env),
        )

    index = pc.Index(index_name)
    print(f"Embedding {len(df)} employees and uploading to Pinecone…")

    vectors = []
    for i, row in df.iterrows():
        text = "\n".join(f"{k}: {v}" for k, v in row.items() if pd.notna(v))
        embedding = embeddings.embed_query(text)
        vectors.append({
            "id": f"emp-{i}",
            "values": embedding,
            "metadata": {
                "text": text,
                "name": str(row.get("Employee Name", "")),
                "department": str(row.get("Department", "")),
            },
        })

    batch_size = 100
    for i in range(0, len(vectors), batch_size):
        index.upsert(vectors=vectors[i:i + batch_size], namespace="employees")
        print(f"  Uploaded {min(i + batch_size, len(vectors))}/{len(vectors)}")

    print(f"✅ Pinecone upload complete ({len(vectors)} records in namespace='employees')")


def main():
    parser = argparse.ArgumentParser(description="Ingest employees into hr.db and Pinecone")
    parser.add_argument("--skip-pinecone", action="store_true", help="Skip Pinecone upload")
    args = parser.parse_args()

    df = load_to_sqlite()

    if not args.skip_pinecone:
        try:
            upload_to_pinecone(df)
        except Exception as exc:
            print(f"⚠️  Pinecone upload failed: {exc}")
            print("   Employee data is still available in hr.db for SQL queries.")
    else:
        print("ℹ️  Pinecone upload skipped (--skip-pinecone).")


if __name__ == "__main__":
    main()
