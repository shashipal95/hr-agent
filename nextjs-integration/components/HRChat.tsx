"use client";

/**
 * nextjs-integration/components/HRChat.tsx
 * =========================================
 * Example streaming chat component for your Next.js portfolio.
 * Copy to your components/ folder and import where needed.
 *
 * Install shadcn/ui or replace with your own UI components.
 */

import { useState, useRef, useEffect } from "react";
import { streamHR, type ChatMessage } from "@/lib/hr-api";

export default function HRChat() {
  const [messages, setMessages]   = useState<ChatMessage[]>([]);
  const [input, setInput]         = useState("");
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus]       = useState("");
  const [liveAnswer, setLiveAnswer] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, liveAnswer]);

  async function send() {
    const question = input.trim();
    if (!question || streaming) return;

    setInput("");
    setStreaming(true);
    setLiveAnswer("");
    setStatus("Querying HR database…");

    let answer = "";

    try {
      for await (const event of streamHR(question, messages)) {
        if (event.type === "status") {
          setStatus(event.data);
        } else if (event.type === "token") {
          answer += event.data;
          setLiveAnswer(answer);
        } else if (event.type === "error") {
          answer = `⚠️ ${event.data}`;
          setLiveAnswer(answer);
        } else if (event.type === "done") {
          break;
        }
      }
    } finally {
      setMessages((prev) => [...prev, { user: question, bot: answer }]);
      setLiveAnswer("");
      setStatus("");
      setStreaming(false);
    }
  }

  function handleKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="flex flex-col h-[600px] max-w-2xl mx-auto border rounded-xl overflow-hidden shadow-lg">
      {/* Header */}
      <div className="px-4 py-3 bg-gray-900 text-white font-semibold flex items-center gap-2">
        <span>🤖</span>
        <span>HR Assistant</span>
        {streaming && (
          <span className="ml-auto text-xs text-gray-400 animate-pulse">{status}</span>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-gray-50">
        {messages.length === 0 && !streaming && (
          <p className="text-center text-gray-400 text-sm mt-8">
            Ask anything about employees, HR policies, or workforce analytics.
          </p>
        )}

        {messages.map((msg, i) => (
          <div key={i} className="space-y-2">
            {/* User bubble */}
            <div className="flex justify-end">
              <div className="bg-blue-600 text-white rounded-2xl rounded-tr-sm px-4 py-2 max-w-[80%] text-sm">
                {msg.user}
              </div>
            </div>
            {/* Bot bubble */}
            <div className="flex justify-start">
              <div className="bg-white border rounded-2xl rounded-tl-sm px-4 py-2 max-w-[80%] text-sm whitespace-pre-wrap shadow-sm">
                {msg.bot}
              </div>
            </div>
          </div>
        ))}

        {/* Live streaming answer */}
        {streaming && (
          <div className="space-y-2">
            <div className="flex justify-start">
              <div className="bg-white border rounded-2xl rounded-tl-sm px-4 py-2 max-w-[80%] text-sm whitespace-pre-wrap shadow-sm">
                {liveAnswer || (
                  <span className="text-gray-400 animate-pulse">{status}</span>
                )}
                {liveAnswer && (
                  <span className="inline-block w-1.5 h-4 bg-blue-500 ml-0.5 animate-pulse align-middle" />
                )}
              </div>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="p-3 border-t bg-white flex gap-2">
        <input
          className="flex-1 border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="Ask about an employee, policy, or department…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKey}
          disabled={streaming}
        />
        <button
          className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          onClick={send}
          disabled={streaming || !input.trim()}
        >
          {streaming ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
