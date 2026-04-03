# 🤖 HR Assistant — FastAPI Backend

MCP-powered HR chatbot API. **Backend on Railway · Frontend on Vercel (Next.js)**

**Stack:** FastAPI · Groq LLM · Google Gemini Embeddings · Pinecone · SQLite · MCP

---

## Architecture

```
Vercel  →  Next.js portfolio (your frontend)
               │  REST + SSE streaming
               ▼
Railway →  FastAPI  (this repo)
               │
               ├── core/hr_agent.py       Groq LLM + ReAct tool loop
               ├── core/mcp_hr_server.py  7 MCP tools via stdio
               ├── hr.db                  SQLite — employee data
               ├── audit.db               SQLite — query audit trail
               └── Pinecone               Policy PDF RAG (cloud)
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check (Railway monitors this) |
| GET | `/docs` | Interactive Swagger UI |
| POST | `/api/chat` | Non-streaming Q&A |
| POST | `/api/chat/stream` | SSE streaming Q&A |
| GET | `/api/employees` | Paginated employee list |
| GET | `/api/analytics` | Department analytics |
| GET | `/api/workforce` | Company-wide KPIs |
| GET | `/api/departments` | Department list |
| GET | `/api/audit` | Query audit log |

---

## Local Development

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Fill in all keys
```

| Variable | Where to get it | Cost |
|---|---|---|
| `GROQ_API_KEY` | https://console.groq.com | Free |
| `GOOGLE_API_KEY` | https://aistudio.google.com | Free |
| `PINECONE_API_KEY` | https://pinecone.io | Free |
| `PINECONE_ENV` | Your Pinecone region (e.g. `us-east-1`) | — |
| `ALLOWED_ORIGINS` | Your Vercel URL | — |

### 3. Add data

```
data/
  employees.csv    ← HR employee dataset (see column spec in docs)
  *.pdf            ← HR policy documents (optional, for RAG)
```

### 4. Ingest

```bash
python scripts/ingest_employees.py    # employees → hr.db + Pinecone
python scripts/ingest_policies.py     # PDFs → Pinecone (skip if no PDFs)
```

### 5. Run locally

```bash
uvicorn app.api_server:app --reload --port 8000
```

- API: http://localhost:8000
- Docs: http://localhost:8000/docs

### 6. Verify

```bash
python scripts/health_check.py
```

---

## Deploy to Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/hr-assistant.git
git push -u origin main
```

> ✅ `.env` is gitignored — never committed
> ✅ `hr.db` is committed (demo data only — Railway disk resets on redeploy)

### 2. Create Railway project

1. Go to [railway.com](https://railway.com) → **Sign up with GitHub** (free, no card)
2. **New Project** → **Deploy from GitHub repo** → select `hr-assistant`
3. Railway auto-detects `railway.toml` — no manual config needed

### 3. Set environment variables

In Railway dashboard → your service → **Variables** tab:

```
GROQ_API_KEY        = gsk_...
GOOGLE_API_KEY      = AIza...
PINECONE_API_KEY    = ...
PINECONE_ENV        = us-east-1
ALLOWED_ORIGINS     = https://yourportfolio.vercel.app
APP_ENV             = production
```

> Railway provides `PORT` automatically — do not set it manually.

### 4. Get your Railway URL

**Settings** tab → **Networking** → copy the public domain:
```
https://hr-assistant-production-xxxx.up.railway.app
```

Verify it works:
```
https://your-app.up.railway.app/health   → { "status": "ok", "agent_ready": true }
https://your-app.up.railway.app/docs     → Swagger UI
```

---

## Connect to Next.js on Vercel

### 1. Add env var in Vercel dashboard

**Project Settings** → **Environment Variables**:
```
NEXT_PUBLIC_HR_API_URL = https://your-app.up.railway.app
```

Redeploy your Vercel project after adding this.

### 2. Copy integration files into your Next.js project

```bash
cp nextjs-integration/lib/hr-api.ts        your-nextjs/lib/
cp nextjs-integration/components/HRChat.tsx  your-nextjs/components/
```

### 3. Use in your pages

```tsx
import HRChat from "@/components/HRChat";

export default function ProjectPage() {
  return <HRChat />;
}
```

Or use the API client directly:

```ts
import { askHR, streamHR, getWorkforce } from "@/lib/hr-api";

// Simple Q&A
const answer = await askHR("How many employees are in Engineering?");

// Streaming (token by token)
for await (const event of streamHR("Who are the top performers?")) {
  if (event.type === "status") setStatus(event.data);
  if (event.type === "token")  setAnswer(a => a + event.data);
  if (event.type === "done")   break;
}

// Data endpoints
const stats = await getWorkforce();
// stats.total, stats.active, stats.avg_pay, ...
```

---

## Auto-deploy workflow

Every `git push` triggers redeploys on both platforms automatically:

```bash
git add .
git commit -m "update"
git push origin main
# Railway redeploys backend  ✅
# Vercel redeploys frontend  ✅
```

---

## Making free credit last longer (Railway tip)

Railway gives $5/month free credit. To stretch it for a portfolio:

1. **Enable sleep on inactivity** → Railway dashboard → Settings → Networking → Sleep
   - Service wakes in ~3-5 seconds when hit (fine for portfolio demos)
2. **Add UptimeRobot** (free) to ping `/health` every 5 min if you want it always warm
   - https://uptimerobot.com → New Monitor → HTTP → your Railway URL + `/health`

---

## Project Structure

```
hr-assistant/
├── core/
│   ├── hr_agent.py              Groq LLM agent + MCP client
│   └── mcp_hr_server.py         7 MCP tools (SQL + RAG)
├── app/
│   └── api_server.py            FastAPI REST + SSE streaming
├── scripts/
│   ├── ingest_employees.py      CSV → SQLite + Pinecone
│   ├── ingest_policies.py       PDFs → Pinecone
│   └── health_check.py          Pre-deploy verification
├── nextjs-integration/
│   ├── lib/hr-api.ts            Typed API client for Next.js
│   └── components/HRChat.tsx    Streaming chat component
├── data/                        Your CSV + PDFs (gitignored for PII)
├── hr.db                        SQLite — commit if demo data
├── railway.toml                 Railway deployment config
├── Procfile                     Fallback start command
├── runtime.txt                  Python 3.11
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## MCP Tools Reference

| Tool | Backend | Description |
|---|---|---|
| `search_employees` | SQLite | Filter by name, dept, position, manager, status |
| `get_employee_details` | SQLite | Full profile for one employee |
| `get_department_analytics` | SQLite | Headcount, pay, performance per dept |
| `get_org_chart` | SQLite | Manager → direct reports tree |
| `get_workforce_summary` | SQLite | Company-wide KPIs |
| `search_hr_policy` | Pinecone | Semantic RAG over policy PDFs |
| `log_audit_event` | SQLite | Immutable audit trail (auto-called) |
