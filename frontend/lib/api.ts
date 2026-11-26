const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type User = { id: string; email: string; name: string | null };
export type Chat = {
  id: string;
  title: string;
  case_id: string | null;
  created_at: string;
  updated_at: string;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Fetch against the backend with the HttpOnly session cookie attached. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!res.ok) {
    const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

/** Multipart upload (the browser sets the Content-Type boundary itself). */
export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${API_URL}${path}`, { method: "POST", body, credentials: "include" });
  if (!res.ok) {
    const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText);
  }
  return (await res.json()) as T;
}

export type CaseSummary = {
  id: string;
  notice_number: string;
  title: string | null;
  status: string | null;
  sector: string | null;
  proceeding_type: string | null;
  posted_date: string | null;
  intervention_deadline: string | null;
  applicant: string | null;
  province: string | null;
  document_count: number;
  source_url: string | null;
};
export type FacetOption = { value: string | number; count: number };
export type Facets = {
  status: { open: number; closed: number; all: number };
  sector: { broadcasting: number; telecom: number; compliance: number };
  proceeding_types: FacetOption[];
  years: FacetOption[];
  provinces: FacetOption[];
  closing: { "7": number; "30": number };
  with_documents: number;
};
export type CasesResponse = {
  total_cases_listed: number;
  matched: number;
  returned: number;
  page: number;
  page_size: number;
  pages: number;
  sort: string;
  cases: CaseSummary[];
  facets: Facets | null;
  refreshed: boolean;
  stale: boolean;
  last_listed_at: string | null;
  dropped_records: number | null;
};

// ---- Knowledge-base sync (Phase 3) ----------------------------------------------------

export type DocItem = {
  title: string | null;
  filer: string | null;
  url: string | null;
  kind: string | null;
  reason: string | null;
  reason_code: string;
  note: string | null;
};
export type RecordState = "not_synced" | "empty" | "partial" | "complete";
export type Coverage = {
  record_state: RecordState;
  embedded_docs: DocItem[];
  total_docs_on_record: number;
  embedded: number;
  pending: number;
  failed: DocItem[];
  skipped: DocItem[];
  blocked: DocItem[];
  counts: { pending: number; embedded: number; failed: number; skipped: number; blocked: number };
  reasons: Record<string, Record<string, number>>;
  truncated: Record<string, number>;
  complete: boolean;
  last_synced_at: string | null;
  chunks: number;
};
export type SyncState = "idle" | "queued" | "listing" | "ingesting" | "done" | "error";
export type SyncProgress = {
  phase: SyncState;
  total: number;
  done: number;
  current: string | null;
  error: string | null;
};
export type SyncStatus = {
  state: SyncState;
  progress: SyncProgress | null;
  last_result: {
    status: string;
    newly_embedded: number;
    warnings?: string[];
    listing_complete?: boolean;
    // present after a portal upload
    files?: number;
    embedded?: number;
    matched?: number;
    added?: number;
    error?: string;
    rejected?: { name: string; status: string; reason: string | null }[];
  } | null;
  coverage: Coverage;
};

// ---- Chat (Phase 4) ---------------------------------------------------------------------

export type Citation = {
  id: string;
  doc_title: string | null;
  filer: string | null;
  filed_date: string | null;
  para_start: number;
  para_end: number;
  page: number | null;
  url: string | null;
  snippet: string;
};
export type AnswerParagraph = {
  text: string;
  kind: "heading" | "body" | "note";
  citations: Citation[];
};
export type StructuredAnswer = {
  paragraphs: AnswerParagraph[];
  citation_valid: boolean;
  warnings: string[];
  n_citations: number;
};
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  structured: StructuredAnswer | null;
  run_id: string | null;
  created_at: string;
};
export type ChatEvent =
  | { event: "token"; data: { text: string } }
  | { event: "status"; data: { label: string } }
  | { event: "tool_start"; data: { name: string; label: string } }
  | { event: "tool_end"; data: { name: string } }
  | { event: "progress"; data: { phase: string; total: number; done: number; current: string | null } }
  | { event: "final"; data: { message_id: string; run_id: string | null; answer: StructuredAnswer } }
  | { event: "error"; data: { message: string } }
  | { event: "skip"; data: { reason: string } };
