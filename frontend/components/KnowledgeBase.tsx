"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, uploadFile, type Coverage, type DocItem, type SyncStatus } from "@/lib/api";
import { safeHref } from "@/lib/safe-url";

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

function DocList({
  title,
  tone,
  items,
  total,
  open = false,
}: {
  title: string;
  tone: string;
  items: DocItem[];
  total: number;
  open?: boolean;
}) {
  if (total === 0) return null;
  return (
    <details className={`mt-3 rounded border p-3 ${tone}`} open={open}>
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
                {d.reason || !d.note ? (
                  <>
                    {REASONS[d.reason_code] ?? d.reason ?? "No reason recorded."}
                    {d.reason ? <span className="text-neutral-400"> ({d.reason})</span> : null}
                  </>
                ) : (
                  d.note
                )}
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
  if (c.counts.blocked) parts.push(`${c.counts.blocked} need your upload`);
  if (c.counts.skipped) parts.push(`${c.counts.skipped} skipped`);
  if (c.counts.failed) parts.push(`${c.counts.failed} failed`);
  if (c.counts.pending) parts.push(`${c.counts.pending} pending`);
  return <>{parts.join(" · ")}</>;
}

export function KnowledgeBase({ caseId, sourceUrl }: { caseId: string; sourceUrl: string | null }) {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);

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

  async function upload(file: File | undefined) {
    if (!file) return;
    setError(null);
    setUploading(true);
    try {
      await uploadFile(`/cases/${caseId}/upload`, file);
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 429
          ? "You have started several syncs or uploads recently. Please wait a few minutes."
          : e instanceof ApiError
            ? e.message
            : "Could not upload the file",
      );
    } finally {
      setUploading(false);
    }
  }

  const c = status?.coverage;
  const never = c?.record_state === "not_synced";
  const empty = c?.record_state === "empty";
  const unreadable = c ? c.counts.blocked + c.counts.failed : 0;
  const busy = running || uploading;
  const proceedingUrl = safeHref(sourceUrl);
  const p = status?.progress;

  return (
    <section className="mt-4 rounded border bg-white p-4 text-sm" aria-label="Knowledge base">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium">Documents for this case</h2>
          <p className="mt-1 text-neutral-700" aria-live="polite">
            {!c ? "Loading…" : never ? "Not synced yet." : empty ? "No documents on the public record." : <Summary c={c} />}
          </p>
          {c?.last_synced_at ? (
            <p className="mt-1 text-xs text-neutral-500">
              Last checked {new Date(c.last_synced_at).toLocaleString()}
            </p>
          ) : null}
        </div>
        <button
          onClick={start}
          disabled={starting || busy}
          className="shrink-0 rounded bg-black px-3 py-1.5 text-white disabled:opacity-50"
        >
          {busy ? "Working…" : "Sync documents"}
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

      {c && empty ? (
        <p className="mt-3 rounded border border-neutral-300 bg-neutral-50 p-2 text-neutral-800">
          This proceeding has no documents on the public record yet. That is normal, not an error. Use “Sync
          documents” to check again later.
        </p>
      ) : null}

      {c && !never && !empty ? (
        c.complete ? (
          <p className="mt-3 rounded border border-green-300 bg-green-50 p-2 text-green-900">
            Every document on the public record is searchable.
          </p>
        ) : (
          <p className="mt-3 rounded border border-amber-300 bg-amber-50 p-2 text-amber-900">
            <strong>This knowledge base is incomplete.</strong> Answers can only draw on the {c.embedded} searchable
            documents; the {c.total_docs_on_record - c.embedded} listed below are not included until they are read.
          </p>
        )
      ) : null}

      {c ? (
        <>
          <DocList title="Searchable" tone="border-green-200" items={c.embedded_docs} total={c.counts.embedded} />
          <DocList title="Blocked by the site" tone="border-amber-200" items={c.blocked} total={c.counts.blocked} open />
          <DocList title="Failed" tone="border-red-200" items={c.failed} total={c.counts.failed} open />
          <DocList title="Skipped" tone="border-neutral-200" items={c.skipped} total={c.counts.skipped} />
          {unreadable > 0 || c.counts.skipped > 0 ? (
            <div className="mt-4 rounded border border-blue-200 bg-blue-50 p-3">
              <h3 className="font-medium text-blue-900">Add the documents we could not fetch</h3>
              <p className="mt-1 text-xs text-blue-900">
                The CRTC site does not let automated tools download these files, but you can. Open the proceeding,
                download its documents (the ZIP of the proceeding, or individual files), then upload them here. We
                match them to the list above and read them like any other document.
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-3">
                {proceedingUrl ? (
                  <a
                    href={proceedingUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded border border-blue-700 px-3 py-1.5 text-blue-900"
                  >
                    1. Open the proceeding on the CRTC site
                  </a>
                ) : null}
                <label
                  className={`rounded bg-blue-700 px-3 py-1.5 text-white ${busy ? "opacity-50" : "cursor-pointer"}`}
                >
                  {uploading ? "Uploading…" : "2. Upload ZIP or files"}
                  <input
                    type="file"
                    className="sr-only"
                    accept=".zip,.pdf,.docx,.html,.htm"
                    disabled={busy}
                    onChange={(e) => {
                      upload(e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                </label>
              </div>
            </div>
          ) : null}
          {status?.last_result?.status === "uploaded" ? (
            <p className="mt-3 text-xs text-neutral-700" aria-live="polite">
              Upload read: {status.last_result.embedded} of {status.last_result.files} files added (
              {status.last_result.matched} matched a listed document, {status.last_result.added} added as new).
              {status.last_result.rejected?.length
                ? ` ${status.last_result.rejected.length} could not be read: ${status.last_result.rejected
                    .slice(0, 5)
                    .map((r) => `${r.name} (${r.reason ?? r.status})`)
                    .join("; ")}.`
                : ""}
            </p>
          ) : null}
          {status?.last_result?.status === "upload_failed" ? (
            <p className="mt-3 rounded border border-red-300 bg-red-50 p-2 text-red-800">
              The upload could not be read: {status.last_result.error ?? "unknown error"}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
