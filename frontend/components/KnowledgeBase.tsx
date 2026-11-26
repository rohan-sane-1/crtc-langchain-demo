"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Coverage, type DocItem, type SyncStatus } from "@/lib/api";

const RUNNING = new Set(["queued", "listing", "ingesting"]);

// What each machine-readable reason means for the person reading it.
const REASONS: Record<string, string> = {
  robots_disallowed:
    "The site's robots.txt asks automated tools not to download this. It is listed here but its content is not searchable.",
  cloudflare_challenge:
    "The site's bot protection refused an automated request. It is not retried; the content is not searchable.",
  http_403: "The server refused the request (HTTP 403).",
  http_401: "The server requires a login (HTTP 401).",
  rate_limited_429: "The server asked us to slow down (HTTP 429); it will be tried again later.",
  needs_ocr: "A scanned document with no text layer. OCR is not supported yet.",
  unsupported_type: "A file type that is not read (only PDF, DOCX and HTML are).",
  nested_archive: "An archive inside an archive; never opened for safety.",
  duplicate_format: "Same filing as a PDF that is already included.",
  type_mismatch: "The file's contents did not match its claimed type, so it was not read.",
  unsafe_archive: "The ZIP file looked unsafe (path traversal, link, or decompression bomb) and was refused.",
  fetch_failed: "A temporary download problem; it will be retried on the next sync.",
  not_found: "The file no longer exists on the site.",
  encrypted: "Password-protected.",
  malformed: "The file is corrupt or could not be read.",
  timeout: "Reading the file took too long.",
  crashed: "The file reader crashed on this file.",
  internal_error: "An unexpected error; the other documents were not affected.",
};

/** Only render a link for plain https CRTC URLs; everything else is shown as text. */
function safeHref(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && (u.hostname === "crtc.gc.ca" || u.hostname.endsWith(".crtc.gc.ca"))
      ? u.toString()
      : null;
  } catch {
    return null;
  }
}

function DocList({ title, tone, items, total }: { title: string; tone: string; items: DocItem[]; total: number }) {
  if (total === 0) return null;
  return (
    <details className={`mt-3 rounded border p-3 ${tone}`}>
      <summary className="cursor-pointer text-sm font-medium">
        {title} ({total})
      </summary>
      <ul className="mt-2 space-y-2 text-sm">
        {items.map((d, i) => {
          const href = safeHref(d.url);
          return (
            <li key={`${d.url}-${i}`} className="border-t pt-2 first:border-t-0 first:pt-0">
              {/* upstream text: rendered as plain text nodes only */}
              <div>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" className="underline">
                    {d.title ?? "Untitled"}
                  </a>
                ) : (
                  <span>{d.title ?? "Untitled"}</span>
                )}
                {d.filer ? <span className="text-neutral-500"> · {d.filer}</span> : null}
              </div>
              <div className="text-xs text-neutral-600">
                {REASONS[d.reason_code] ?? d.reason ?? "No reason recorded."}
                {d.reason ? <span className="text-neutral-400"> ({d.reason})</span> : null}
              </div>
            </li>
          );
        })}
        {total > items.length ? (
          <li className="text-xs text-neutral-500">…and {total - items.length} more not shown.</li>
        ) : null}
      </ul>
    </details>
  );
}

function Summary({ c }: { c: Coverage }) {
  const parts = [`${c.total_docs_on_record} documents on the public record`, `${c.embedded} searchable`];
  if (c.counts.blocked) parts.push(`${c.counts.blocked} blocked`);
  if (c.counts.skipped) parts.push(`${c.counts.skipped} skipped`);
  if (c.counts.failed) parts.push(`${c.counts.failed} failed`);
  if (c.counts.pending) parts.push(`${c.counts.pending} pending`);
  return <>{parts.join(" · ")}</>;
}

export function KnowledgeBase({ caseId, notice }: { caseId: string; notice: string }) {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api<SyncStatus>(`/cases/${caseId}/sync-status`));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load sync status");
    }
  }, [caseId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const running = status ? RUNNING.has(status.state) : false;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [running, refresh]);

  async function start() {
    setError(null);
    setStarting(true);
    try {
      await api(`/cases/${caseId}/sync`, { method: "POST" });
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 429
          ? "You have started several syncs recently. Please wait a few minutes."
          : e instanceof ApiError
            ? e.message
            : "Could not start the sync",
      );
    } finally {
      setStarting(false);
    }
  }

  const c = status?.coverage;
  const never = !!c && c.total_docs_on_record === 0 && !c.last_synced_at;
  const p = status?.progress;

  return (
    <section className="mt-4 rounded border bg-white p-4 text-sm" aria-label="Knowledge base">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium">Documents for this case</h2>
          <p className="mt-1 text-neutral-700" aria-live="polite">
            {!c ? "Loading…" : never ? "Not synced yet." : <Summary c={c} />}
          </p>
          {c?.last_synced_at ? (
            <p className="mt-1 text-xs text-neutral-500">
              Last checked {new Date(c.last_synced_at).toLocaleString()}
            </p>
          ) : null}
        </div>
        <button
          onClick={start}
          disabled={starting || running}
          className="shrink-0 rounded bg-black px-3 py-1.5 text-white disabled:opacity-50"
        >
          {running ? "Syncing…" : "Sync documents"}
        </button>
      </div>

      {running && p ? (
        <p className="mt-3 text-xs text-neutral-600" aria-live="polite">
          {p.phase === "ingesting"
            ? `Reading documents: ${p.done} of ${p.total}${p.current ? ` · ${p.current}` : ""}`
            : p.phase === "listing"
              ? "Checking the public record…"
              : "Waiting to start…"}
        </p>
      ) : null}

      {status?.state === "error" ? (
        <p className="mt-3 rounded border border-red-300 bg-red-50 p-2 text-red-800">
          The last sync failed ({status.progress?.error ?? "unknown error"}). Documents already read are unaffected.
        </p>
      ) : null}
      {status?.last_result?.status === "listing_failed" ? (
        <p className="mt-3 rounded border border-red-300 bg-red-50 p-2 text-red-800">
          The CRTC public record could not be listed, so this may be out of date.
        </p>
      ) : null}
      {status?.last_result?.warnings?.map((w, i) => (
        <p key={i} className="mt-2 text-xs text-amber-800">
          {w}
        </p>
      ))}
      {error ? <p className="mt-3 text-red-700">{error}</p> : null}

      {c && !never ? (
        c.complete ? (
          <p className="mt-3 rounded border border-green-300 bg-green-50 p-2 text-green-900">
            Every document on the public record is searchable.
          </p>
        ) : (
          <p className="mt-3 rounded border border-amber-300 bg-amber-50 p-2 text-amber-900">
            <strong>This knowledge base is incomplete.</strong> Answers can only draw on the {c.embedded} searchable
            documents; the {c.total_docs_on_record - c.embedded} listed below are not included.
          </p>
        )
      ) : null}

      {c ? (
        <>
          <DocList title="Failed" tone="border-red-200" items={c.failed} total={c.counts.failed} />
          <DocList title="Blocked" tone="border-amber-200" items={c.blocked} total={c.counts.blocked} />
          <DocList title="Skipped" tone="border-neutral-200" items={c.skipped} total={c.counts.skipped} />
          {c.counts.blocked > 0 ? (
            <p className="mt-3 text-xs text-neutral-500">
              To include a blocked file, download it in your browser and run{" "}
              <code className="rounded bg-neutral-100 px-1">
                uv run python -m app.cli ingest-file {notice} &lt;file&gt; --for-url &lt;document url&gt;
              </code>
              .
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
