"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Chat } from "@/lib/api";
import { useRequireUser } from "@/lib/use-user";

export default function HomePage() {
  const router = useRouter();
  const user = useRequireUser();
  const [chats, setChats] = useState<Chat[] | null>(null);

  useEffect(() => {
    if (user) api<Chat[]>("/chats").then(setChats);
  }, [user]);

  async function newChat() {
    const chat = await api<Chat>("/chats", { method: "POST", body: JSON.stringify({}) });
    router.push(`/chat/${chat.id}`);
  }

  async function logout() {
    await api("/auth/logout", { method: "POST" });
    router.replace("/login");
  }

  if (!user) return null;

  return (
    <main className="mx-auto max-w-2xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Your chats</h1>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-neutral-500">{user.email}</span>
          <button onClick={logout} className="underline">
            Log out
          </button>
        </div>
      </header>

      <button
        onClick={newChat}
        className="mb-6 rounded bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700"
      >
        New chat
      </button>
      <Link href="/cases" className="mb-6 ml-4 text-sm underline">
        Browse CRTC proceedings
      </Link>

      {chats === null ? (
        <p className="text-sm text-neutral-500">Loading…</p>
      ) : chats.length === 0 ? (
        <p className="text-sm text-neutral-500">No chats yet.</p>
      ) : (
        <ul className="divide-y rounded border bg-white">
          {chats.map((c) => (
            <li key={c.id}>
              <Link href={`/chat/${c.id}`} className="flex justify-between p-3 hover:bg-neutral-50">
                <span>{c.title}</span>
                <span className="text-xs text-neutral-500">
                  {new Date(c.updated_at).toLocaleString()}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
