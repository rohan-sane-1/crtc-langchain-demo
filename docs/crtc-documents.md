# Where a CRTC case's documents live, and which of them we can reach

Findings from Phase 3 discovery (probed 2026-11-23). Every request used our one honest
User-Agent (`crtc-copilot/0.1 (personal research tool; polite client; 1 req/s)`) at about
1 request per second. Nothing here disguises the client, retries a refusal, or solves a challenge.

## Summary

| Source | Host | Reachable? | What it gives us |
|---|---|---|---|
| Filings list (JSON) | `applications.crtc.gc.ca` | **Yes** (200) | Who filed what and when: filer, date, document type, file names and sizes |
| The filings' files | `applications.crtc.gc.ca/DocWebBroker/` | **No: robots.txt disallows it** | Interventions, replies, answers... (the bulk of a busy case) |
| "Related documents" ZIPs | `applications.crtc.gc.ca/TransferToWeb/` | **Yes** (200) | The application itself, procedural requests, RFI responses, costs. These hold real PDFs/DOCX |
| Notice and letter pages | `crtc.gc.ca`, `www.crtc.gc.ca` | **No: Cloudflare managed challenge** (HTTP 403, `cf-mitigated: challenge`) | Notices of consultation, staff letters, decisions |

Share of documents by status, for the **45 open cases** (135 documents on record: 40 filing files,
75 files inside 16 ZIPs, 20 notice/letter pages; the ZIP containers themselves aren't counted):

| Outcome | Documents | Share |
|---|---|---|
| Reachable and readable (PDFs, plus DOCX including `.doc`-named DOCX) | about 67 (43 PDF + about 24 DOCX) | about 50% |
| Reachable but a type we don't read (legacy OLE `.doc`, `.xls`) | about 8 | about 6% |
| Blocked: `robots.txt` disallows `/DocWebBroker/` | 40 | 30% |
| Blocked: Cloudflare challenge | 20 | 15% |

The DOCX estimate comes from one sampled ZIP (14 of its 17 `.doc` files were DOCX); the rest are exact counts.
Only 3 of the 45 open cases have filings yet: most are new BITS/tariff applications still inside
their comment period.

Across **all 7,064 stored cases**, counting the stored related-document links: 13,861 links, of
which **4,467 (32%) are reachable ZIPs** on the `applications` host and **9,394 (68%) are
notice/letter pages behind Cloudflare**. In addition, 2,061 cases have an interventions list page
(whose files are all behind the robots.txt rule). 5,291 cases have at least one source of documents.

Live end-to-end check (closed case `202604032`): 6 documents on record, **5 embedded (83%)**,
1 blocked (a staff letter on `crtc.gc.ca`: Cloudflare).

## What is stored in `cases.document_links`

The Phase 2 case list stores the "Related Documents" column as links. For all 7,064 cases (13,861 links
after classification, including each case's own `source_url`):

| Kind | Host | Count | Reachable |
|---|---|---|---|
| `archive` (`TransferToWeb/*.zip`) | applications.crtc.gc.ca | 4,466 | yes |
| `notice` (`/eng/archive/YYYY/NNNN-NNN.htm`) | crtc.gc.ca / www.crtc.gc.ca | 3,297 / 2,497 | no (Cloudflare) |
| `letter` (Staff / Secretary General letters) | crtc.gc.ca / www.crtc.gc.ca | 2,321 / 1,269 | no (Cloudflare) |
| `other` (`/public/otf/...pdf`, `.docx`) | crtc.gc.ca / www.crtc.gc.ca | 2 / 8 | no (Cloudflare) |
| Interventions list pages (`/listes-lists/interventions/21?f=...&dt=...`) | applications.crtc.gc.ca | 3,862 | not documents: a view over the filings JSON (below) |

Only 11 of the 45 open cases had stored links. For the rest the **filings list works without any
stored link** (below): the stored interventions link is only a convenience.

## 1. Filings list (JSON)

The "interventions" page (`.../portail-portal/eng/listes-lists/interventions/21?f=<file>&dt=<type>`)
fills its table with JavaScript. The underlying request, found in the page source, is:

```
GET https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/21/data?f=<file>&g=num
```

* `f` is the **notice number for consultations** (`2026-259`) and the **dashed file number for
  applications**: the 9-digit case number `202605387` becomes `2026-0538-7`
  (`YYYY-NNNN-N`). Passing the 9-digit form returns `{"data":[]}`.
* An unknown `f` also returns `{"data":[]}` with HTTP 200, so "no filings" and "wrong id" look the same.
* `dt=` (`i`, `r`, `ar`, `c`, `f`, ...) is applied in the browser, not the server. The endpoint returns
  every document type for the file.
* Response: `{"data":[{apl, name, cmpny, date, itm, num, typ, sbjEn, sbjFr, dcEn, dcFr, evnt, cnum}, ...]}`.
  `typ` is `INTR`, `REPL`, `ANSW`, `FINA`, `CMMT`, ... `dcEn` is `"<DMID>!<file name>|<size>|<DMID>!<file name>|<size>|"`.
  Each filing typically has a PDF and an HTML rendition of the same document.
* Cache-Control `public, max-age=3600`. Roughly 7 s per request (the first run of 45 cases took about 5 minutes).
* Each file is opened through `https://applications.crtc.gc.ca/DocWebBroker/OpenDocument.aspx?DMID=<id>`.

## 2. Related-documents ZIPs

`https://applications.crtc.gc.ca/TransferToWeb/<year>/<file>_<Procedural requests|Responses to requests for information|Costs|...>.zip`
(`200`, `application/x-zip-compressed`, HEAD works). For applications, the case's own `source_url` is the
application ZIP (`.../TransferToWeb/2026/8340-T114-202605858.zip`).

Sampled contents (open cases, 16 ZIPs, 75 members): 43 PDF, 29 `.doc`, 3 `.xls`. Member names are
`DM#<id> - <description>.<ext>` and use legacy encodings (accented characters can arrive garbled; we
only use names as display text). **ZIPs grow as filings arrive**, so a URL alone isn't a change
detector: we sign each ZIP with `Content-Length | Last-Modified | ETag` from a HEAD request, and
each member with `CRC32:size`.

## 3. Notices, letters and `/public/` files

`https://crtc.gc.ca/eng/archive/2026/2026-259.htm` and the `www.` equivalent answer our honest
client with **HTTP 403 and `cf-mitigated: challenge`** (a Cloudflare managed-challenge page,
"Security Check"). That is a bot-protection decision, not an error. We record these as
`blocked: cloudflare_challenge`, make one request per host per sync, and then stop asking that host
for the rest of the run (and for 24 hours afterwards).

## robots.txt (fetched for all three hosts)

* **applications.crtc.gc.ca**: `Disallow: /DocWebBroker/`, `/ListeInterventionList/`, `/preweb/`,
  `/recherche-search/`, `/listes-lists/interventions/`, `/listes-lists/instances-publiques/`,
  `/listes-lists/public-proceedings/`, `/telecom-applications-report/`, ...
  * `/DocWebBroker/` matches the real download URLs, so **every filing's file is off limits** and we
    never request one. They are listed (title, filer, date) and shown as blocked.
  * **Judgement call, please confirm.** The listing rules (`/listes-lists/interventions/`,
    `/listes-lists/public-proceedings/`) do not carry the `/portail-portal/eng` prefix that the
    real URLs have. Under RFC 9309 a rule matches from the start of the path, so
    `/portail-portal/eng/listes-lists/21/data` (and Phase 2's `/18/data`) are *not* matched and
    the client treats them as allowed. The *intent* of those rules may be to keep crawlers off
    the interventions list. I fetched the interventions HTML page once while investigating, and the
    sync code only ever uses the `/21/data` JSON. If you would rather not use it, turn it off in
    `records.list_record_documents` (the stored related-document links work without it).
* **crtc.gc.ca**: Cloudflare content signals `search=yes, ai-train=no` (no `ai-input` signal);
  named AI-training crawlers (GPTBot, ClaudeBot, ...) are disallowed; `Disallow: /public/`, `/hearing/`,
  `/*.zip$`, ... Blocked by Cloudflare anyway. We are not a training crawler and don't train.
* **www.crtc.gc.ca**: content signals only (`ai-train=no`), no path restrictions for us.

Our matcher implements `*`, `$`, longest-match and Allow-beats-Disallow (`app/crtc/robots.py`; the stdlib
parser can't do wildcards) and is tested against these three real files.

## Rules the code follows

* HTTPS only, host allowlist on every redirect hop, SSRF guard, streaming size cap, timeouts, about 1 request/second.
* robots.txt is checked before any request (cached per host); a disallowed path is never requested.
* 401/403/451, 429 and Cloudflare challenges are recorded as `blocked` with a reason and **never retried**;
  one refusal stops further requests to that host for the rest of the run, and blocked documents are not
  retried for 24 h (`KB_BLOCKED_RETRY_S`).
* No browser User-Agent, header spoofing, proxies, headless browser or challenge solving.

## Fallback options for the blocked documents

1. **Manual download, then ingest (built).** Open the document in your browser, save it, then:
   `uv run python -m app.cli ingest-file <notice> <file> --for-url "<document url from the Blocked list>"`.
   It goes through the same magic-byte check, sandboxed parser, chunking and embedding. For a notice or
   letter page, use "Save page as" (HTML) and ingest that.
2. **Ask the CRTC.** A request to their web team for permission or a feed, or an exception for this
   client on the Cloudflare side, is the legitimate way to get automated access. The robots.txt rule on
   `/DocWebBroker/` and the managed challenge are both explicit signals that it is currently off limits.
3. **Not an option here:** driving a real browser (Playwright etc.) to pass the challenge. That disguises the
   client and is against the ground rules of this project.

If you want me to learn more about what the site serves, one useful capture: open an interventions page in
your browser (for example `.../interventions/21?f=2026-259&dt=i`), click a document, and in DevTools
> Network copy the redirect chain after `DocWebBroker/OpenDocument.aspx?DMID=...`. It would only tell us
where the file finally comes from; we still could not follow the first hop.
