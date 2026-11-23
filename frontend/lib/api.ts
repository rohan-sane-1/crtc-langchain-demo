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
