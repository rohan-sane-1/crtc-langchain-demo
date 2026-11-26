# crtc/

## Data source (discovered Phase 2)

`GET https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data` is the JSON the
public-proceedings table loads. Shape: `{"data": [ {...}, ... ]}`, ~7,000 records, ~18 MB,
`Cache-Control: public, max-age=3600`, not sorted. Field notes are in the Phase 2 summary;
the fetch/parse code is `client.py` (hardened HTTP) and `cases.py` (validation, upsert, cache).
Subject/Doc/EventNo contain HTML and are treated as untrusted.
`tests/fixtures/crtc_list_sample.json` is a trimmed real sample.

## Phase 3: documents on a case's record

See `docs/crtc-documents.md` for the discovery write-up (sources, hosts, URL patterns, what is
reachable and why). In short:

* `records.py` lists a case's documents: the filings JSON (`/portail-portal/eng/listes-lists/21/data?f=<file>&g=num`),
  the stored related-document links (`TransferToWeb/*.zip` are reachable, `crtc.gc.ca` pages are
  Cloudflare-challenged), and the case's own `source_url`.
* `client.py` gained robots.txt enforcement (`robots.py`), `fetch_document` (streaming size cap),
  `head`, and `FetchBlocked` for 401/403/429/Cloudflare, which are recorded and never retried.
* Parsing, chunking, embedding and the sync pipeline live in `app/ingest/`.
