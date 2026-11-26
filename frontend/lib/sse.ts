import { ApiError, type ChatEvent } from "@/lib/api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** POST a JSON body and parse the text/event-stream response (EventSource can't POST). */
export async function postSSE(
  path: string,
  body: unknown,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value.replace(/\r\n/g, "\n");
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue; // keep-alive comment
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (!data.length) continue;
      try {
        onEvent({ event, data: JSON.parse(data.join("\n")) } as ChatEvent);
      } catch {
        /* ignore a malformed event */
      }
    }
  }
}
