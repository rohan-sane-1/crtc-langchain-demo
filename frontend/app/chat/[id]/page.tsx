"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { ChatPanel } from "@/components/ChatPanel";
import { KnowledgeBase } from "@/components/KnowledgeBase";
import { api, ApiError, type CaseSummary, type Chat } from "@/lib/api";
import { useRequireUser } from "@/lib/use-user";

export default function ChatPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const user = useRequireUser();
  const [chat, setChat] = useState<Chat | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [theCase, setTheCase] = useState<CaseSummary | null>(null);

  useEffect(() => {
    if (!user) return;
    api<Chat>(`/chats/${id}`)
      .then(setChat)
      .catch((e) => e instanceof ApiError && e.status === 404 && setNotFound(true));
  }, [user, id]);

  useEffect(() => {
    if (chat?.case_id) api<CaseSummary>(`/cases/${chat.case_id}`).then(setTheCase).catch(() => {});
  }, [chat?.case_id]);

  if (!user) return null;

  return (
    <main className="mx-auto max-w-3xl p-6">
      <Link href="/" className="text-sm underline">
        ← All chats
      </Link>
      {notFound ? (
        <p className="mt-6">Chat not found.</p>
      ) : chat ? (
        <>
          <h1 className="mt-4 text-xl font-semibold">{chat.title}</h1>
          <p className="mt-1 text-xs text-neutral-500">Thread id: {chat.id}</p>
          {chat.case_id ? (
            <p className="mt-4 rounded border bg-white p-4 text-sm">
              <span className="font-mono text-xs">{theCase?.notice_number}</span>{" "}
              {/* upstream text: plain text node only */}
              <span>{theCase?.title}</span>
            </p>
          ) : (
            <p className="mt-4 text-sm">
              <Link href={`/cases?chat=${chat.id}`} className="underline">
                Choose a CRTC proceeding for this chat
              </Link>
            </p>
          )}
          {chat.case_id && theCase ? (
            <KnowledgeBase caseId={chat.case_id} sourceUrl={theCase.source_url} />
          ) : null}
          {chat.case_id && theCase ? <ChatPanel chatId={chat.id} /> : null}
        </>
      ) : null}
    </main>
  );
}
