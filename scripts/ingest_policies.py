"""
scripts/ingest_policies.py
===========================
Load HR policy PDFs → Pinecone vector store for RAG.

Usage:
    python scripts/ingest_policies.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

DATA_DIR   = _ROOT / "data"
CHUNK_SIZE    = 500
CHUNK_OVERLAP = 60


def main():
    if not DATA_DIR.exists():
        DATA_DIR.mkdir()
        print(f"Created {DATA_DIR}/ — place your HR policy PDFs here, then re-run.")
        sys.exit(0)

    pdfs = list(DATA_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDF files found in {DATA_DIR}/")
        print("Add your HR policy PDFs and re-run.")
        sys.exit(0)

    print(f"Found {len(pdfs)} PDF(s): {[p.name for p in pdfs]}")

    # ── Load & split ──────────────────────────────────────────────────────────
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    docs = []
    for pdf in pdfs:
        loader = PyPDFLoader(str(pdf))
        pages = loader.load()
        for p in pages:
            p.metadata["source"] = pdf.name
        docs.extend(pages)
        print(f"  {pdf.name}: {len(pages)} pages")

    print(f"\nTotal pages: {len(docs)}")

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks = splitter.split_documents(docs)
    print(f"Chunks created: {len(chunks)}")

    # ── Embeddings ────────────────────────────────────────────────────────────
    google_key = os.getenv("GOOGLE_API_KEY")
    if not google_key:
        print("❌ GOOGLE_API_KEY not set in .env")
        sys.exit(1)

    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=google_key,
        output_dimensionality=768,
    )

    # ── Pinecone ──────────────────────────────────────────────────────────────
    pinecone_key = os.getenv("PINECONE_API_KEY")
    pinecone_env = os.getenv("PINECONE_ENV", "us-east-1")
    index_name   = os.getenv("PINECONE_POLICY_INDEX", "hr-policy-index")

    if not pinecone_key:
        print("❌ PINECONE_API_KEY not set in .env")
        sys.exit(1)

    from pinecone import Pinecone, ServerlessSpec
    from langchain_pinecone import PineconeVectorStore

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

    print(f"Embedding & uploading to Pinecone index '{index_name}'…")
    PineconeVectorStore.from_documents(
        documents=chunks,
        embedding=embeddings,
        index_name=index_name,
    )
    print(f"✅ Policy index ready ({len(chunks)} chunks in '{index_name}')")


if __name__ == "__main__":
    main()
