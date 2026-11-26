"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { CitationPanel } from "@/components/CitationPanel";
import {
  api,
  ApiError,
  type AnswerParagraph,
  type ChatEvent,
  type ChatMessage,
  type Citation,
  type StructuredAnswer,
} from "@/lib/api";
import { postSSE } from "@/lib/sse";

const PRESETS = [
  "Main themes in this case",
  "Industry positions (who argues what)",
  "Anything that relates to Rogers?",
  "What did the CRTC decide?",
];

type Progress = { done: number; total: number; current: string | null };

function Paragraph({ p, onCite }: { p: AnswerParagraph; onCite: (c: Citation) => void }) {
  const byId = new Map(p.citations.map((c) => [c.id, c]));
  const parts = p.text.split(/(\[c\d+\])/g);
  const content = parts.map((part, i) => {
    const m = /^\[(c\d+)\]$/.exec(part);
    const cite = m ? byId.get(m[1]) : undefined;
    if (!cite) return <Fragment key={i}>{part}</Fragment>;
    return (
      <button
        key={i}
        onClick={() => onCite(cite)}
        title={cite.doc_title ?? undefined}
        className="mx-0.5 rounded bg-blue-100 px-1.5 py-0.5 align-baseline text-xs font-medium text-blue-900 hover:bg-blue-200"
      >
        {cite.id.replace("c", "")}
      </button>
    );
  });
  if (p.kind === "heading") return <h3 className="mt-4 text-sm font-semibold">{content}</h3>;
  if (p.kind === "note") return <p className="mt-3 text-xs italic text-neutral-500">{content}</p>;
  return <p className="mt-2 text-sm leading-relaxed">{content}</p>;
}

function Answer({
  m,
  onCite,
  onFeedback,
  rated,
}: {
  m: ChatMessage;
  onCite: (c: Citation) => void;
  onFeedback: (score: 0 | 1) => void;
  rated: 0 | 1 | undefined;
}) {
  const s: StructuredAnswer | null = m.structured;
  return (
    <div className="rounded border bg-white p-4">
      {s && !s.citation_valid ? (
        <p role="alert" className="mb-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
          Some statements below could not be tied to a source passage. Verify before relying on them.
        </p>
      ) : null}
      {s ? s.paragraphs.map((p, i) => <Paragraph key={i} p={p} onCite={onCite} />) : <p className="text-sm">{m.content}</p>}
      {m.run_id ? (
        <div className="mt-3 flex items-center gap-2 text-xs text-neutral-500">
          <span>Was this helpful?</span>
          {([1, 0] as const).map((score) => (
            <button
              key={score}
              onClick={() => onFeedback(score)}
              disabled={rated !== undefined}
              aria-label={score ? "Helpful" : "Not helpful"}
              className={`rounded border px-2 py-0.5 ${rated === score ? "bg-neutral-900 text-white" : ""}`}
            >
              {score ? "👍" : "👎"}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ChatPanel({ chatId, onTurnDone }: { chatId: string; onTurnDone?: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [rated, setRated] = useState<Record<string, 0 | 1>>({});
  const opened = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  const runTurn = useCallback(
    async (body: { content?: string; open?: boolean }) => {
      setStreaming(true);
      setError(null);
      setDraft("");
      setStatus(body.open ? "Checking knowledge base…" : "Thinking…");
      setProgress(null);
      if (body.content) {
        setMessages((ms) => [
          ...ms,
          { id: `local-${Date.now()}`, role: "user", content: body.content!, structured: null, run_id: null, created_at: "" },
        ]);
      }
      const onEvent = (e: ChatEvent) => {
        switch (e.event) {
          case "token":
            setDraft((d) => d + e.data.text);
            break;
          case "tool_start":
            setDraft("");
            setStatus(e.data.label);
            break;
          case "tool_end":
            setProgress(null);
            break;
          case "status":
            setStatus(e.data.label);
            break;
          case "progress":
            if (e.data.total > 0) setProgress({ done: e.data.done, total: e.data.total, current: e.data.current });
            break;
          case "final":
            setMessages((ms) => [
              ...ms,
              {
                id: e.data.message_id,
                role: "assistant",
                content: "",
                structured: e.data.answer,
                run_id: e.data.run_id,
                created_at: "",
              },
            ]);
            setDraft("");
            break;
          case "error":
            setError(e.data.message);
            break;
        }
      };
      try {
        await postSSE(`/chats/${chatId}/messages`, body, onEvent);
      } catch (e) {
        if (!(body.open && e instanceof ApiError && e.status === 409)) {
          setError(e instanceof ApiError ? e.message : "Connection lost. Reload to see the answer, if it finished.");
        }
      } finally {
        setStreaming(false);
        setStatus(null);
        setProgress(null);
        setDraft("");
        onTurnDone?.();
      }
    },
    [chatId, onTurnDone],
  );

  useEffect(() => {
    api<ChatMessage[]>(`/chats/${chatId}/messages`)
      .then((ms) => {
        setMessages(ms);
        setLoaded(true);
        if (ms.length === 0 && !opened.current) {
          opened.current = true;
          void runTurn({ open: true });
        }
      })
      .catch(() => setLoaded(true));
  }, [chatId, runTurn]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, draft, status]);

  const send = (text: string) => {
    const t = text.trim();
    if (!t || streaming) return;
    setInput("");
    void runTurn({ content: t });
  };

  const feedback = (m: ChatMessage, score: 0 | 1) => {
    setRated((r) => ({ ...r, [m.id]: score }));
    api(`/chats/${chatId}/messages/${m.id}/feedback`, { method: "POST", body: JSON.stringify({ score }) }).catch(() => {});
  };

  return (
    <section className="mt-6" aria-label="Conversation">
      <div className="space-y-3">
        {!loaded ? <p className="text-sm text-neutral-500">Loading…</p> : null}
        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="ml-auto max-w-[85%] whitespace-pre-wrap rounded bg-neutral-900 p-3 text-sm text-white">
              {m.content}
            </div>
          ) : (
            <Answer key={m.id} m={m} onCite={setCitation} onFeedback={(s) => feedback(m, s)} rated={rated[m.id]} />
          ),
        )}
        {streaming ? (
          <div className="rounded border border-dashed bg-white p-3 text-sm text-neutral-600" aria-live="polite">
            <span className="inline-block rounded-full bg-neutral-100 px-2 py-0.5 text-xs">{status}</span>
            {progress ? (
              <span className="ml-2 text-xs">
                {progress.done}/{progress.total}
                {progress.current ? ` · ${progress.current}` : ""}
              </span>
            ) : null}
            {draft ? <p className="mt-2 whitespace-pre-wrap text-neutral-500">{draft}</p> : null}
          </div>
        ) : null}
        {error ? (
          <p role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
            {error}
          </p>
        ) : null}
        <div ref={bottom} />
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {PRESETS.map((p) => (
          <button
            key={p}
            onClick={() => send(p)}
            disabled={streaming}
            className="rounded-full border bg-white px-3 py-1 text-xs hover:bg-neutral-100 disabled:opacity-50"
          >
            {p}
          </button>
        ))}
      </div>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          maxLength={2000}
          disabled={streaming}
          placeholder="Ask about this proceeding…"
          aria-label="Your question"
          className="min-w-0 flex-1 rounded border bg-white px-3 py-2 text-sm disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={streaming || !input.trim()}
          className="rounded bg-neutral-900 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          Ask
        </button>
      </form>

      {citation ? <CitationPanel citation={citation} onClose={() => setCitation(null)} /> : null}
    </section>
  );
}
