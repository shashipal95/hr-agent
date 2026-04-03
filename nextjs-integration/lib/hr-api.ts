/**
 * lib/hr-api.ts
 * =============
 * Drop this file into your Next.js project.
 * Provides typed functions to call the HR Assistant API.
 *
 * Setup:
 *   1. Add to .env.local:
 *        NEXT_PUBLIC_HR_API_URL=https://your-app.koyeb.app
 *   2. Import and use anywhere in your Next.js app.
 */

const API_BASE = process.env.NEXT_PUBLIC_HR_API_URL || "http://localhost:8000";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ChatMessage {
  user: string;
  bot:  string;
}

export interface ChatResponse {
  answer: string;
}

export interface Employee {
  "Employee Name":    string;
  Department:         string;
  Position:           string;
  "Employment Status": string;
  "Manager Name":     string;
  "Pay Rate":         number;
  "Performance Score": string;
  Sex:                string;
  Age:                number;
}

export interface EmployeesResponse {
  employees:      Employee[];
  total_filtered: number;
  total_all:      number;
}

export interface WorkforceStats {
  total:         number;
  active:        number;
  terminated:    number;
  avg_pay:       number;
  min_pay:       number;
  max_pay:       number;
  departments:   number;
  positions:     number;
  top_performers: number;
  female:        number;
  male:          number;
}

// SSE event from /api/chat/stream
export type StreamEvent =
  | { type: "status"; data: string }
  | { type: "token";  data: string }
  | { type: "done";   data: string }
  | { type: "error";  data: string };


// ── Non-streaming chat ────────────────────────────────────────────────────────

export async function askHR(
  message:  string,
  history:  ChatMessage[] = [],
  userId:   string = "anonymous",
): Promise<string> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ message, history, user_id: userId }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Chat request failed");
  }

  const data: ChatResponse = await res.json();
  return data.answer;
}


// ── Streaming chat ────────────────────────────────────────────────────────────
// Usage:
//   for await (const event of streamHR(message, history)) {
//     if (event.type === "token") setAnswer(prev => prev + event.data)
//     if (event.type === "status") setStatus(event.data)
//     if (event.type === "done") break
//   }

export async function* streamHR(
  message: string,
  history: ChatMessage[] = [],
  userId:  string = "anonymous",
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ message, history, user_id: userId }),
  });

  if (!res.ok || !res.body) {
    yield { type: "error", data: `Request failed: ${res.statusText}` };
    return;
  }

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let   buffer  = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";   // keep incomplete line in buffer

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const event: StreamEvent = JSON.parse(line.slice(6));
        yield event;
        if (event.type === "done") return;
      } catch {
        // malformed line — skip
      }
    }
  }
}


// ── Data endpoints ────────────────────────────────────────────────────────────

export async function getEmployees(params?: {
  name?:       string;
  department?: string;
  status?:     string;
  limit?:      number;
  offset?:     number;
}): Promise<EmployeesResponse> {
  const qs = new URLSearchParams();
  if (params?.name)       qs.set("name",       params.name);
  if (params?.department) qs.set("department", params.department);
  if (params?.status)     qs.set("status",     params.status);
  if (params?.limit)      qs.set("limit",      String(params.limit));
  if (params?.offset)     qs.set("offset",     String(params.offset));

  const res = await fetch(`${API_BASE}/api/employees?${qs}`);
  return res.json();
}

export async function getWorkforce(): Promise<WorkforceStats> {
  const res = await fetch(`${API_BASE}/api/workforce`);
  return res.json();
}

export async function getAnalytics() {
  const res = await fetch(`${API_BASE}/api/analytics`);
  return res.json();
}

export async function getDepartments(): Promise<{ departments: string[] }> {
  const res = await fetch(`${API_BASE}/api/departments`);
  return res.json();
}

export async function checkHealth() {
  const res = await fetch(`${API_BASE}/health`);
  return res.json();
}
