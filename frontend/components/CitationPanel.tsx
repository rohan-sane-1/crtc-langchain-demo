"use client";

import type { Citation } from "@/lib/api";
import { safeHref } from "@/lib/safe-url";

export function CitationPanel({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const href = safeHref(citation.url);
  const paras =
    citation.para_start === citation.para_end
      ? `¶ ${citation.para_start}`
      : `¶ ${citation.para_start}–${citation.para_end}`;
  return (
    <aside
      role="complementary"
      aria-label="Source"
      className="fixed inset-y-0 right-0 z-10 w-full max-w-md overflow-y-auto border-l bg-white p-5 shadow-xl"
    >
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-sm font-semibold">Source [{citation.id}]</h2>
        <button onClick={onClose} className="rounded border px-2 py-0.5 text-xs" aria-label="Close source panel">
          Close
        </button>
      </div>
      {/* All fields below are upstream filing text: plain text nodes only. */}
      <p className="mt-3 text-sm font-medium">{citation.doc_title ?? "Untitled document"}</p>
      <dl className="mt-2 space-y-1 text-xs text-neutral-600">
        <div>
          <dt className="inline font-medium">Filer: </dt>
          <dd className="inline">{citation.filer ?? "not recorded"}</dd>
        </div>
        <div>
          <dt className="inline font-medium">Filed: </dt>
          <dd className="inline">{citation.filed_date ?? "not recorded"}</dd>
        </div>
        <div>
          <dt className="inline font-medium">Location: </dt>
          <dd className="inline">
            {paras}
            {citation.page ? ` · page ${citation.page}` : ""}
          </dd>
        </div>
      </dl>
      <blockquote className="mt-4 whitespace-pre-wrap border-l-4 border-neutral-300 pl-3 text-sm text-neutral-800">
        {citation.snippet}
      </blockquote>
      <div className="mt-4">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block rounded bg-neutral-900 px-3 py-1.5 text-sm text-white"
          >
            Open document ↗
          </a>
        ) : (
          <p className="text-xs text-neutral-500">No public link available for this document.</p>
        )}
      </div>
    </aside>
  );
}
