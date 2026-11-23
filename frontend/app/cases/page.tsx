"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { api, ApiError, type CasesResponse, type Chat } from "@/lib/api";
import { useRequireUser } from "@/lib/use-user";

/**
 * Case search. The URL query string is the single source of truth for filters, so a filtered
 * view can be bookmarked or shared. Parameter names match the backend's GET /cases exactly.
 * `chat` (not sent to the API) is the chat to attach the selected case to.
 *
 * All case text comes from an upstream site and is untrusted: it is only ever rendered as
 * React text nodes (never as HTML).
 */

const FILTER_KEYS = [
  "q",
  "status",
  "sector",
  "proceeding_type",
  "year",
  "applicant",
  "province",
  "closing_within",
  "deadline_from",
  "deadline_to",
  "has_documents",
  "sort",
  "page",
  "page_size",
] as const;

const SORT_LABELS: Record<string, string> = {
  deadline: "Deadline (soonest first)",
  posted_desc: "Posted date (newest first)",
  posted_asc: "Posted date (oldest first)",
  notice_asc: "Notice number (A to Z)",
  notice_desc: "Notice number (Z to A)",
  relevance: "Best match",
};

const inputCls = "rounded border border-neutral-300 bg-white px-2 py-1.5 text-sm";
const labelCls = "mb-1 block text-xs font-medium text-neutral-700";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function CasesPage() {
  // useSearchParams needs a Suspense boundary for static prerendering.
  return (
    <Suspense fallback={null}>
      <CasesView />
    </Suspense>
  );
}

function CasesView() {
  const user = useRequireUser();
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const chatId = sp.get("chat");

  const [data, setData] = useState<CasesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectError, setSelectError] = useState<string | null>(null);
  const [selecting, setSelecting] = useState<string | null>(null);

  // Text inputs keep local state and are debounced into the URL.
  const [qInput, setQInput] = useState(sp.get("q") ?? "");
  const [applicantInput, setApplicantInput] = useState(sp.get("applicant") ?? "");
  const [suggestions, setSuggestions] = useState<string[]>([]);

  const status = sp.get("status") ?? "open";
  const queryForApi = FILTER_KEYS.map((k) => [k, sp.get(k)] as const)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`)
    .join("&");

  const update = useCallback(
    (changes: Record<string, string | null>, keepPage = false) => {
      const next = new URLSearchParams(sp.toString());
      for (const [k, v] of Object.entries(changes)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      if (!keepPage) next.delete("page");
      const qs = next.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [sp, router, pathname],
  );

  // Debounce the two text boxes into the URL (250 ms).
  const qDebounced = useDebounced(qInput, 250);
  const applicantDebounced = useDebounced(applicantInput, 250);
  useEffect(() => {
    if ((sp.get("q") ?? "") !== qDebounced.trim()) update({ q: qDebounced.trim() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qDebounced]);
  useEffect(() => {
    if ((sp.get("applicant") ?? "") !== applicantDebounced.trim())
      update({ applicant: applicantDebounced.trim() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicantDebounced]);

  // Keep the text boxes in sync when the URL changes from elsewhere (back button, Clear all).
  useEffect(() => {
    setQInput(sp.get("q") ?? "");
    setApplicantInput(sp.get("applicant") ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryForApi]);

  // Load results. The page opens with the default view (open, nearest deadline first).
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    setLoading(true);
    api<CasesResponse>(`/cases${queryForApi ? `?${queryForApi}` : ""}`)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [user, queryForApi]);

  // Applicant type-ahead.
  useEffect(() => {
    const term = applicantInput.trim();
    if (!user || term.length < 2) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      api<{ value: string; count: number }[]>(`/cases/applicants?q=${encodeURIComponent(term)}`)
        .then((r) => !cancelled && setSuggestions(r.map((x) => x.value)))
        .catch(() => !cancelled && setSuggestions([]));
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [user, applicantInput]);

  async function selectCase(caseId: string, notice: string) {
    setSelectError(null);
    setSelecting(caseId);
    try {
      let target = chatId;
      if (!target) {
        const chat = await api<Chat>("/chats", { method: "POST", body: JSON.stringify({}) });
        target = chat.id;
      }
      await api(`/chats/${target}`, { method: "PATCH", body: JSON.stringify({ case_id: caseId }) });
      router.push(`/chat/${target}`);
    } catch (e) {
      setSelectError(
        e instanceof ApiError && e.status === 409
          ? "This chat already has a case. Start a new chat to pick a different one."
          : `Could not select ${notice}: ${(e as Error).message}`,
      );
      setSelecting(null);
    }
  }

  if (!user) return null;

  const f = data?.facets;
  const hasActiveFilters = FILTER_KEYS.some((k) => k !== "page" && k !== "page_size" && sp.get(k));
  const effectiveSort = sp.get("sort") ?? data?.sort ?? "deadline";
  const clearHref = chatId ? `${pathname}?chat=${encodeURIComponent(chatId)}` : pathname;
  const opt = (label: string, count?: number) => (count === undefined ? label : `${label} (${count})`);

  return (
    <main className="mx-auto max-w-6xl p-6">
      <Link href={chatId ? `/chat/${chatId}` : "/"} className="text-sm underline">
        ← {chatId ? "Back to chat" : "All chats"}
      </Link>
      <h1 className="mt-4 mb-1 text-xl font-semibold">CRTC proceedings</h1>
      <p className="mb-4 text-sm text-neutral-600">
        {chatId
          ? "Pick the proceeding this chat is about."
          : "Search proceedings, then select one to start a chat about it."}
      </p>

      <form
        role="search"
        aria-label="Filter proceedings"
        onSubmit={(e) => e.preventDefault()}
        className="mb-4 rounded border bg-white p-4"
      >
        <div className="mb-3">
          <label htmlFor="q" className={labelCls}>
            Search notice number, subject or applicant
          </label>
          <input
            id="q"
            type="search"
            value={qInput}
            maxLength={200}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="e.g. 2026-259 or Rogers"
            className={`${inputCls} w-full`}
          />
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label htmlFor="status" className={labelCls}>
              Status
            </label>
            <select
              id="status"
              value={status}
              onChange={(e) => update({ status: e.target.value === "open" ? null : e.target.value })}
              className={`${inputCls} w-full`}
            >
              <option value="open">{opt("Open", f?.status.open)}</option>
              <option value="closed">{opt("Closed", f?.status.closed)}</option>
              <option value="all">{opt("All", f?.status.all)}</option>
            </select>
          </div>

          <div>
            <label htmlFor="sector" className={labelCls}>
              Sector
            </label>
            <select
              id="sector"
              value={sp.get("sector") ?? ""}
              onChange={(e) => update({ sector: e.target.value })}
              className={`${inputCls} w-full`}
            >
              <option value="">{opt("Any sector")}</option>
              <option value="telecom">{opt("Telecom", f?.sector.telecom)}</option>
              <option value="broadcasting">{opt("Broadcasting", f?.sector.broadcasting)}</option>
              <option value="compliance">{opt("Compliance & Enforcement", f?.sector.compliance)}</option>
            </select>
          </div>

          <div>
            <label htmlFor="proceeding_type" className={labelCls}>
              Proceeding type
            </label>
            <select
              id="proceeding_type"
              value={sp.get("proceeding_type") ?? ""}
              onChange={(e) => update({ proceeding_type: e.target.value })}
              className={`${inputCls} w-full`}
            >
              <option value="">Any type</option>
              {f?.proceeding_types.map((t) => (
                <option key={t.value} value={String(t.value)}>
                  {opt(String(t.value), t.count)}
                </option>
              ))}
              {/* keep a selected value visible even if the current filters give it zero matches */}
              {sp.get("proceeding_type") &&
                !f?.proceeding_types.some((t) => String(t.value) === sp.get("proceeding_type")) && (
                  <option value={sp.get("proceeding_type") as string}>{sp.get("proceeding_type")}</option>
                )}
            </select>
          </div>

          <div>
            <label htmlFor="year" className={labelCls}>
              Year posted
            </label>
            <select
              id="year"
              value={sp.get("year") ?? ""}
              onChange={(e) => update({ year: e.target.value })}
              className={`${inputCls} w-full`}
            >
              <option value="">Any year</option>
              {f?.years.map((y) => (
                <option key={y.value} value={String(y.value)}>
                  {opt(String(y.value), y.count)}
                </option>
              ))}
              {sp.get("year") && !f?.years.some((y) => String(y.value) === sp.get("year")) && (
                <option value={sp.get("year") as string}>{sp.get("year")}</option>
              )}
            </select>
          </div>

          <div>
            <label htmlFor="applicant" className={labelCls}>
              Applicant
            </label>
            <input
              id="applicant"
              type="text"
              list="applicant-options"
              value={applicantInput}
              maxLength={200}
              autoComplete="off"
              onChange={(e) => setApplicantInput(e.target.value)}
              placeholder="Start typing a name"
              className={`${inputCls} w-full`}
            />
            <datalist id="applicant-options">
              {suggestions.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          </div>

          <div>
            <label htmlFor="province" className={labelCls}>
              Province (licence renewals only)
            </label>
            <select
              id="province"
              value={sp.get("province") ?? ""}
              onChange={(e) => update({ province: e.target.value })}
              className={`${inputCls} w-full`}
            >
              <option value="">Any province</option>
              {f?.provinces.map((p) => (
                <option key={p.value} value={String(p.value)}>
                  {opt(String(p.value), p.count)}
                </option>
              ))}
              {sp.get("province") && !f?.provinces.some((p) => String(p.value) === sp.get("province")) && (
                <option value={sp.get("province") as string}>{sp.get("province")}</option>
              )}
            </select>
          </div>

          <div>
            <label htmlFor="deadline_from" className={labelCls}>
              Deadline from
            </label>
            <input
              id="deadline_from"
              type="date"
              value={sp.get("deadline_from") ?? ""}
              onChange={(e) => update({ deadline_from: e.target.value })}
              className={`${inputCls} w-full`}
            />
          </div>
          <div>
            <label htmlFor="deadline_to" className={labelCls}>
              Deadline to
            </label>
            <input
              id="deadline_to"
              type="date"
              value={sp.get("deadline_to") ?? ""}
              onChange={(e) => update({ deadline_to: e.target.value })}
              className={`${inputCls} w-full`}
            />
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span id="closing-label" className="text-xs font-medium text-neutral-700">
            Closing soon:
          </span>
          {(["7", "30"] as const).map((n) => {
            const on = sp.get("closing_within") === n;
            return (
              <button
                key={n}
                type="button"
                aria-pressed={on}
                aria-describedby="closing-label"
                onClick={() => update({ closing_within: on ? null : n })}
                className={`rounded border px-3 py-1 text-sm ${
                  on ? "border-neutral-900 bg-neutral-900 text-white" : "bg-white hover:bg-neutral-50"
                }`}
              >
                {opt(`Next ${n} days`, f?.closing[n])}
              </button>
            );
          })}
          <label className="ml-2 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={sp.get("has_documents") === "true"}
              onChange={(e) => update({ has_documents: e.target.checked ? "true" : null })}
            />
            {opt("Has documents", f?.with_documents)}
          </label>
          <button
            type="button"
            onClick={() => router.replace(clearHref, { scroll: false })}
            disabled={!hasActiveFilters}
            className="ml-auto rounded border px-3 py-1 text-sm underline disabled:opacity-40"
          >
            Clear all filters
          </button>
        </div>
      </form>

      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p role="status" aria-live="polite" className="text-sm text-neutral-700">
          {data
            ? `${data.matched.toLocaleString()} matching of ${data.total_cases_listed.toLocaleString()} proceedings`
            : "Loading…"}
          {data?.stale && " · list may be out of date (refresh failed)"}
        </p>
        <div className="flex items-center gap-3 text-sm">
          <div>
            <label htmlFor="sort" className="mr-1 text-xs font-medium text-neutral-700">
              Sort by
            </label>
            <select
              id="sort"
              value={effectiveSort}
              onChange={(e) => update({ sort: e.target.value })}
              className={inputCls}
            >
              {Object.entries(SORT_LABELS)
                .filter(([k]) => k !== "relevance" || sp.get("q"))
                .map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
            </select>
          </div>
          <div>
            <label htmlFor="page_size" className="mr-1 text-xs font-medium text-neutral-700">
              Per page
            </label>
            <select
              id="page_size"
              value={sp.get("page_size") ?? "25"}
              onChange={(e) => update({ page_size: e.target.value === "25" ? null : e.target.value })}
              className={inputCls}
            >
              {[25, 50, 100].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {error && (
        <p role="alert" className="mb-3 text-sm text-red-700">
          Could not load cases: {error}
        </p>
      )}
      {selectError && (
        <p role="alert" className="mb-3 text-sm text-red-700">
          {selectError}
        </p>
      )}

      {data && data.cases.length === 0 && !loading ? (
        <div className="rounded border bg-white p-8 text-center">
          <p className="font-medium">No proceedings match these filters.</p>
          <p className="mt-1 text-sm text-neutral-600">Try removing a filter or searching for something broader.</p>
          {hasActiveFilters && (
            <button
              type="button"
              onClick={() => router.replace(clearHref, { scroll: false })}
              className="mt-4 rounded bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700"
            >
              Clear all filters
            </button>
          )}
        </div>
      ) : (
        <div className={`overflow-x-auto rounded border bg-white ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
          <table className="w-full min-w-[56rem] text-left text-sm">
            <caption className="sr-only">CRTC proceedings matching the current filters</caption>
            <thead className="border-b bg-neutral-50 text-xs uppercase text-neutral-600">
              <tr>
                <th scope="col" className="p-3">Notice</th>
                <th scope="col" className="p-3">Subject</th>
                <th scope="col" className="p-3">Status</th>
                <th scope="col" className="p-3">Posted</th>
                <th scope="col" className="p-3">Deadline</th>
                <th scope="col" className="p-3">Applicant</th>
                <th scope="col" className="p-3">
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {data?.cases.map((c) => (
                <tr key={c.id} className="align-top">
                  <td className="p-3 font-mono text-xs whitespace-nowrap">
                    {c.source_url?.startsWith("https://") ? (
                      <a href={c.source_url} target="_blank" rel="noopener noreferrer" className="underline">
                        {c.notice_number}
                      </a>
                    ) : (
                      c.notice_number
                    )}
                  </td>
                  <td className="max-w-md p-3">
                    <div className="line-clamp-3">{c.title ?? "(untitled)"}</div>
                    <div className="mt-1 text-xs text-neutral-500">
                      {[c.proceeding_type, c.sector].filter(Boolean).join(" · ")}
                      {c.document_count > 0 && ` · ${c.document_count} document${c.document_count === 1 ? "" : "s"}`}
                    </div>
                  </td>
                  <td className="p-3 capitalize">{c.status}</td>
                  <td className="p-3 whitespace-nowrap">{c.posted_date ?? "—"}</td>
                  <td className="p-3 whitespace-nowrap">{c.intervention_deadline ?? "—"}</td>
                  <td className="p-3">{c.applicant ?? "—"}</td>
                  <td className="p-3">
                    <button
                      type="button"
                      disabled={selecting !== null}
                      onClick={() => selectCase(c.id, c.notice_number)}
                      aria-label={`Select proceeding ${c.notice_number}`}
                      className="rounded bg-neutral-900 px-3 py-1 text-white hover:bg-neutral-700 disabled:opacity-50"
                    >
                      {selecting === c.id ? "Selecting…" : "Select"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && data.pages > 1 && (
        <nav aria-label="Pagination" className="mt-4 flex items-center justify-center gap-4 text-sm">
          <button
            type="button"
            disabled={data.page <= 1}
            onClick={() => update({ page: String(data.page - 1) }, true)}
            className="rounded border bg-white px-3 py-1 disabled:opacity-40"
          >
            Previous
          </button>
          <span aria-current="page">
            Page {data.page} of {data.pages}
          </span>
          <button
            type="button"
            disabled={data.page >= data.pages}
            onClick={() => update({ page: String(data.page + 1) }, true)}
            className="rounded border bg-white px-3 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </nav>
      )}
    </main>
  );
}
