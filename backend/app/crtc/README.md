# crtc/

## Data source (discovered Phase 2)

`GET https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data` is the JSON the
public-proceedings table loads. Shape: `{"data": [ {...}, ... ]}`, ~7,000 records, ~18 MB,
`Cache-Control: public, max-age=3600`, not sorted. Field notes are in the Phase 2 summary;
the fetch/parse code is `client.py` (hardened HTTP) and `cases.py` (validation, upsert, cache).
Subject/Doc/EventNo contain HTML and are treated as untrusted.
`tests/fixtures/crtc_list_sample.json` is a trimmed real sample.
