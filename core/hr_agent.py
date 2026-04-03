"""
core/hr_agent.py
================
HR Agent — Groq LLM with a ReAct-style loop over MCP tools.

Supports:
  ask()        → returns complete answer string (sync wrapper available)
  ask_stream() → async generator yielding tokens for streaming
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

log = logging.getLogger("hr_agent")

# ── Groq LLM ─────────────────────────────────────────────────────────────────
LLM_AVAILABLE = False
_llm = None

try:
    from langchain_groq import ChatGroq

    _groq_key = os.environ.get("GROQ_API_KEY")
    if not _groq_key:
        raise EnvironmentError("GROQ_API_KEY is not set.")

    _llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0,
        max_tokens=1024,
        api_key=_groq_key,
    )
    LLM_AVAILABLE = True
    log.info("✅ Groq LLM ready (llama-3.3-70b-versatile)")
except Exception as exc:
    log.warning("⚠️  Groq unavailable: %s", exc)


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an intelligent HR Assistant with access to a set of tools.

## Available Tools
{tool_docs}

## Rules
- If you need a tool, respond with ONLY valid JSON (no markdown, no extra text):
    {{"tool": "tool_name", "args": {{...}}}}
- Integer parameters (e.g. "k", "limit") MUST be numbers, not strings.
- Employee names may be "Last, First" format — pass the full name as one string.
- After receiving tool results, write your final answer in plain English.
- Do NOT call another tool if you already have the information needed.
- Be concise, professional, and privacy-aware.

## Conversation History
{history}
"""

SYNTHESIS_PROMPT = """\
Answer ONLY the specific question asked. Do not volunteer extra information.

Question: {question}

HR Data:
{tool_results}

Rules:
1. Answer ONLY what was asked. If asked for age, give only the age.
2. For yes/no questions: answer "Yes" or "No" first, then one short sentence.
   - "Single"/"Unmarried" means NOT married → "No".
   - "Active" employment means NOT terminated → "No, not terminated".
3. For a single field (age, salary, department): one direct sentence.
4. Do NOT list unrequested employee attributes.
5. Do NOT output JSON.

Answer:"""

MAX_TOOL_CALLS = 4

# ── LLM helpers ───────────────────────────────────────────────────────────────

def _run_llm(prompt: str) -> str:
    if not LLM_AVAILABLE or _llm is None:
        raise RuntimeError("Groq LLM not available. Set GROQ_API_KEY.")
    result = _llm.invoke(prompt)
    return result.content if hasattr(result, "content") else str(result)


async def _run_llm_async(prompt: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, _run_llm, prompt)


async def _stream_llm(prompt: str) -> AsyncIterator[str]:
    """Stream LLM output token by token."""
    if not LLM_AVAILABLE or _llm is None:
        raise RuntimeError("Groq LLM not available.")
    loop = asyncio.get_event_loop()
    full = await loop.run_in_executor(None, _run_llm, prompt)
    # Groq streaming via LangChain; simulate token stream if needed
    for char in full:
        yield char
        await asyncio.sleep(0)


# ── Arg sanitizer ─────────────────────────────────────────────────────────────

def _sanitize_args(tool_name: str, args: dict, tools: dict) -> dict:
    tool = tools.get(tool_name)
    if not tool:
        return args
    props = (tool.inputSchema or {}).get("properties", {})
    sanitized = {}
    for k, v in args.items():
        expected = props.get(k, {}).get("type", "")
        if expected == "integer" and isinstance(v, str):
            try:
                sanitized[k] = int(v)
                continue
            except ValueError:
                pass
        elif expected == "number" and isinstance(v, str):
            try:
                sanitized[k] = float(v)
                continue
            except ValueError:
                pass
        sanitized[k] = v
    return sanitized


def _parse_tool_call(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
            if isinstance(obj, dict) and "tool" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _extract_name(question: str) -> str | None:
    for pat in [
        r"(?:of|for)\s+([A-Z][a-zA-Z]+,\s*[A-Z][a-zA-Z]+)",
        r"(?:of|for)\s+([A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+)",
        r"\b([A-Z][a-zA-Z]+,\s*[A-Z][a-zA-Z]+)\b",
    ]:
        m = re.search(pat, question)
        if m:
            return m.group(1).strip()
    return None


# ── MCP Client ────────────────────────────────────────────────────────────────

_SERVER_SCRIPT = Path(__file__).parent / "mcp_hr_server.py"


class MCPClient:
    def __init__(self, server_script: str | Path = _SERVER_SCRIPT):
        self.server_script = str(server_script)
        self._session: ClientSession | None = None
        self._tools: dict[str, Any] = {}

    async def connect(self):
        params = StdioServerParameters(command=sys.executable, args=["-u", "-W", "ignore", self.server_script])
        self._cm = stdio_client(params)
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        resp = await self._session.list_tools()
        self._tools = {t.name: t for t in resp.tools}
        log.info("MCP connected. Tools: %s", list(self._tools))

    async def call(self, tool_name: str, args: dict) -> str:
        if self._session is None:
            raise RuntimeError("MCPClient not connected.")
        args = _sanitize_args(tool_name, args, self._tools)
        resp = await self._session.call_tool(tool_name, args)
        return "\n".join(b.text for b in resp.content if hasattr(b, "text"))

    def tool_docs(self) -> str:
        lines = []
        for name, tool in self._tools.items():
            props = (tool.inputSchema or {}).get("properties", {})
            param_str = ", ".join(
                f"{k}: {v.get('type','any')} — {v.get('description','')}"
                for k, v in props.items()
            )
            lines.append(f"### {name}\n{tool.description}\nParameters: {param_str or 'none'}\n")
        return "\n".join(lines)

    async def disconnect(self):
        if self._session:
            await self._session.__aexit__(None, None, None)
        if hasattr(self, "_cm"):
            await self._cm.__aexit__(None, None, None)


# ── HR Agent ──────────────────────────────────────────────────────────────────

class HRAgent:
    def __init__(self, client: MCPClient, user_id: str = "anonymous"):
        self.client = client
        self.user_id = user_id

    # ── Internal: tool loop ───────────────────────────────────────────────────
    async def _run_tool_loop(
        self, question: str, chat_history: list[dict]
    ) -> tuple[list[str], str | None]:
        """
        Returns (collected_tool_results, early_answer_or_None).
        early_answer is set when no synthesis is needed.
        """
        if not LLM_AVAILABLE:
            result = await self._keyword_fallback(question)
            return [], result

        # High-confidence direct routing (avoids unnecessary LLM hops)
        direct = await self._direct_route(question)
        if direct is not None:
            return direct

        history_text = "\n".join(
            f"User: {h['user']}\nAssistant: {h['bot']}"
            for h in chat_history[-6:]
        )
        system = SYSTEM_PROMPT.format(
            tool_docs=self.client.tool_docs(),
            history=history_text or "(none)",
        )
        conversation = f"{system}\n\nUser: {question}\nAssistant:"
        collected: list[str] = []

        for _ in range(MAX_TOOL_CALLS):
            raw = await _run_llm_async(conversation)
            log.debug("LLM raw: %s", raw[:200])

            tool_call = _parse_tool_call(raw)
            if tool_call is None:
                return [], raw.strip() if raw.strip() else None

            tool_name = tool_call.get("tool", "")
            args = tool_call.get("args", {})
            if not tool_name:
                return [], raw.strip() if raw.strip() else None

            # Auto-audit employee lookups
            if tool_name in ("get_employee_details", "search_employees"):
                try:
                    await self.client.call("log_audit_event", {
                        "tool": tool_name,
                        "query": question,
                        "user_id": self.user_id,
                        "status": "ok",
                    })
                except Exception:
                    pass

            try:
                result = await self.client.call(tool_name, args)
            except Exception as exc:
                result = f"Tool error: {exc}"

            collected.append(f"[{tool_name}]\n{result}")
            conversation += f"\n{raw}\n[Tool Result: {tool_name}]\n{result}\nAssistant:"

        return collected, None

    # ── Public API ────────────────────────────────────────────────────────────
    async def ask(self, question: str, chat_history: list[dict]) -> str:
        collected, early = await self._run_tool_loop(question, chat_history)

        if early is not None:
            return early

        if not collected:
            return "I was unable to find relevant information. Please try rephrasing."

        prompt = SYNTHESIS_PROMPT.format(
            question=question,
            tool_results="\n\n".join(collected),
        )
        answer = await _run_llm_async(prompt)

        # Strip any stray JSON the LLM may have emitted during synthesis
        if _parse_tool_call(answer) is not None:
            lines = [l for l in answer.splitlines() if not l.strip().startswith("{")]
            answer = " ".join(lines).strip()
            if not answer:
                answer = "Here is the retrieved data:\n\n" + "\n\n".join(collected)
        return answer

    async def ask_stream(
        self, question: str, chat_history: list[dict]
    ) -> AsyncIterator[str]:
        """
        Async generator:
          1. Runs tool calls silently.
          2. Yields a sentinel \\x00TOOLS_DONE\\x00 when tools complete.
          3. Streams the synthesis answer token-by-token.
        """
        collected, early = await self._run_tool_loop(question, chat_history)

        if early is not None:
            for ch in early:
                yield ch
                await asyncio.sleep(0)
            return

        if not collected:
            yield "I was unable to find relevant information. Please try rephrasing."
            return

        yield "\x00TOOLS_DONE\x00"

        prompt = SYNTHESIS_PROMPT.format(
            question=question,
            tool_results="\n\n".join(collected),
        )
        async for token in _stream_llm(prompt):
            yield token

    # ── Direct routing helpers ────────────────────────────────────────────────
    async def _direct_route(
        self, question: str
    ) -> tuple[list[str], str | None] | None:
        """Return results directly for high-confidence patterns, or None to fall through."""
        q = question.lower()

        detail_kw = [
            "pay rate", "salary", "wage", "details", "profile",
            "position", "department of", "manager of", "age of",
            "is married", "is he", "is she", "marital",
        ]
        if any(kw in q for kw in detail_kw):
            name = _extract_name(question)
            if name:
                await self._safe_audit("get_employee_details", question)
                result = await self.client.call("get_employee_details", {"name": name})
                return ([f"[get_employee_details]\n{result}"], None)

        if re.search(
            r"how many.+(?:in|from|within)\s+(?:the\s+)?([a-zA-Z\s]+?)"
            r"\s*(?:department|dept|team)?[?.]?$",
            q,
        ):
            result = await self.client.call("get_department_analytics", {})
            return ([f"[get_department_analytics]\n{result}"], None)

        return None

    async def _keyword_fallback(self, question: str) -> str:
        """LLM-free fallback using keyword heuristics."""
        q = question.lower()
        name = _extract_name(question)
        if any(kw in q for kw in ["pay rate", "salary", "details", "profile"]) and name:
            return await self.client.call("get_employee_details", {"name": name})
        if re.search(r"how many.+(?:in|from).+(?:department|dept|team)", q):
            return await self.client.call("get_department_analytics", {})
        if any(w in q for w in ["policy", "leave", "pto", "benefit", "conduct", "remote"]):
            return await self.client.call("search_hr_policy", {"query": question})
        if any(w in q for w in ["summary", "overview", "kpi", "workforce", "company"]):
            return await self.client.call("get_workforce_summary", {})
        if "department" in q and any(w in q for w in ["analytic", "stat", "breakdown"]):
            return await self.client.call("get_department_analytics", {})
        if "org" in q or "direct report" in q:
            return await self.client.call("get_org_chart", {})
        return await self.client.call("search_employees", {"name": question})

    async def _safe_audit(self, tool: str, query: str):
        try:
            await self.client.call("log_audit_event", {
                "tool": tool, "query": query,
                "user_id": self.user_id, "status": "ok",
            })
        except Exception:
            pass


# ── Sync wrappers (for Streamlit) ─────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_mcp_client: MCPClient | None = None
_agent: HRAgent | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def get_agent(user_id: str = "anonymous") -> HRAgent:
    global _mcp_client, _agent
    if _agent is None:
        _mcp_client = MCPClient()
        _get_loop().run_until_complete(_mcp_client.connect())
        _agent = HRAgent(_mcp_client, user_id=user_id)
    _agent.user_id = user_id
    return _agent


def ask_sync(question: str, chat_history: list[dict], user_id: str = "anonymous") -> str:
    agent = get_agent(user_id)
    return _get_loop().run_until_complete(agent.ask(question, chat_history))
