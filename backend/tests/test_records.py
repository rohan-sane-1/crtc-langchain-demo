"""Listing the documents on a case's public record."""

from types import SimpleNamespace

import httpx
import pytest

from app.crtc.records import (
    RecordDoc,
    RecordListingError,
    classify_link,
    fingerprint,
    list_record_documents,
    listing_key,
    parse_filings,
    related_documents,
)
from tests.fakes import APPS, LISTING, FakeSite, filing_row, filings_payload, make_zip


def case(notice="2026-259", source_url=None, links=()):
    return SimpleNamespace(
        notice_number=notice, source_url=source_url, document_links=[{"url": u, "text": t} for u, t in links]
    )


# ----------------------------------------------------------------------------- listing key


@pytest.mark.parametrize(
    ("notice", "links", "expected"),
    [
        ("202605387", [], "2026-0538-7"),  # application: 9 digits -> dashed file number
        ("2026-259", [], "2026-259"),  # notice of consultation: as is
        ("202605387", [(f"{APPS}/portail-portal/eng/listes-lists/interventions/21?f=2026-0538-7&dt=i", "I")], "2026-0538-7"),
        ("weird", [(f"{APPS}/portail-portal/eng/listes-lists/interventions/21?f=ABC&dt=r", "R")], "ABC"),
        ("2026-47", [("https://crtc.gc.ca/a.htm", "x")], "2026-47"),
    ],
)
def test_listing_key(notice, links, expected):
    assert listing_key(notice, [{"url": u, "text": t} for u, t in links]) == expected


# ----------------------------------------------------------------------------- filings


def test_filings_become_documents_with_filer_date_and_title():
    payload = filings_payload(
        [
            filing_row("Doe, Jane", "INTR", [(111, "Intervention.PDF", "494 KB")], "2026-11-16", cmpny="Acme Ltd."),
            filing_row("Roe, Rick", "REPL", [(222, "Reply.docx", "9 KB")], "2026-11-17"),
        ]
    )
    docs, dropped = parse_filings(payload)
    assert dropped == 0 and len(docs) == 2
    a, b = docs
    assert a.url == f"{APPS}/DocWebBroker/OpenDocument.aspx?DMID=111"
    assert a.title == "Intervention: Intervention.PDF"
    assert a.filer == "Doe, Jane (Acme Ltd.)" and str(a.filed_date) == "2026-11-16" and a.kind == "filing"
    assert b.title == "Reply: Reply.docx" and b.doc_type == "REPL"


def test_pdf_and_html_renditions_of_one_filing_are_linked():
    docs, _ = parse_filings(
        filings_payload([filing_row(docs=[(1, "Intervention.PDF", "1 KB"), (2, "Intervention.HTML", "1 KB"),
                                          (3, "Annex.HTML", "1 KB")])])
    )
    by = {d.title.split(": ")[1]: d for d in docs}
    assert by["Intervention.PDF"].variant_of is None
    assert by["Intervention.HTML"].variant_of == by["Intervention.PDF"].url  # the twin points at the PDF
    assert by["Annex.HTML"].variant_of is None  # an HTML with no PDF sibling is a real document


def test_bad_filing_rows_are_dropped_and_counted_not_fatal():
    payload = {
        "data": [
            filing_row(),
            "a string",
            None,
            {"name": {"x": 1}, "dcEn": "1!a.pdf|1 KB|"},  # wrong type
            filing_row(docs=[("12abc", "BadId.pdf", "1 KB")]),  # non-numeric DMID
            filing_row(docs=[(7, "x" * 600 + ".pdf", "1 KB")]),  # title too long after clean -> trimmed, kept
            filing_row(date="not-a-date"),  # bad date: document kept with no date
        ]
    }
    docs, dropped = parse_filings(payload)
    assert dropped == 4
    assert len(docs) == 3
    assert docs[-1].filed_date is None


def test_unusable_filings_payload_raises():
    for bad in (None, [], {"rows": []}, {"data": "x"}):
        with pytest.raises(RecordListingError):
            parse_filings(bad)
    assert parse_filings({"data": []}) == ([], 0)


def test_html_and_control_characters_in_filer_names_are_neutralised():
    docs, _ = parse_filings(filings_payload([filing_row(name="Evil\u200b<script>x</script>\x07 Corp")]))
    assert "\u200b" not in docs[0].filer and "\x07" not in docs[0].filer


# ----------------------------------------------------------------------------- related documents


@pytest.mark.parametrize(
    ("url", "text", "kind"),
    [
        (f"{APPS}/TransferToWeb/2026/2026-259_Procedural%20requests.zip", "Procedural requests", "archive"),
        (f"{APPS}/portail-portal/eng/listes-lists/interventions/21?f=2026-259&dt=i", "Interventions", None),
        ("https://crtc.gc.ca/eng/archive/2026/lt261109a.htm", "Staff Letter 9 November 2026", "letter"),
        ("https://www.crtc.gc.ca/eng/archive/2026/lb261030.htm", "x", "letter"),
        ("https://crtc.gc.ca/eng/archive/2026/2026-259.htm", "Notice", "notice"),
        ("https://www.crtc.gc.ca/public/otf/2012/x.PDF", "pdf", "other"),
    ],
)
def test_classify_link(url, text, kind):
    assert classify_link(url, text) == kind


def test_related_documents_include_the_application_zip_source_and_skip_listing_pages():
    docs, dropped = related_documents(
        "202605700",
        f"{APPS}/TransferToWeb/2026/8340-T1-202605700.zip",
        [
            {"url": f"{APPS}/portail-portal/eng/listes-lists/interventions/21?f=2026-0570-0&dt=i", "text": "I"},
            {"url": "https://crtc.gc.ca/eng/archive/2026/lt1.htm", "text": "Staff Letter"},
        ],
    )
    assert dropped == 0
    assert [(d.kind, d.title) for d in docs] == [("archive", "Notice 202605700"), ("letter", "Staff Letter")]


def test_off_allowlist_urls_never_become_documents():
    docs, dropped = related_documents("1", "https://evil.example/x.zip", [{"url": "http://crtc.gc.ca/x.htm", "text": "t"}])
    assert docs == [] and dropped == 2
    with pytest.raises(ValueError):
        RecordDoc(url="https://evil.example/a.pdf", title="t", kind="other")


# ----------------------------------------------------------------------------- full listing


@pytest.fixture
def site():
    return FakeSite()


async def test_list_record_documents_combines_sources_and_signs_archives(make_client, site):
    zip_url = f"{APPS}/TransferToWeb/2026/2026-259_Procedural%20requests.zip"
    site.add_json(f"{LISTING}?f=2026-259&g=num", filings_payload([filing_row(docs=[(5, "A.PDF", "1 KB")])]))
    site.add_zip(zip_url, make_zip({"a.pdf": b"x"}))
    client = make_client(site)
    c = case(links=[(zip_url, "Procedural requests"), ("https://crtc.gc.ca/eng/archive/2026/lt1.htm", "Letter")],
             source_url="https://crtc.gc.ca/eng/archive/2026/2026-259.htm")
    listing = await list_record_documents(c, client)
    kinds = sorted(d.kind for d in listing.documents)
    assert kinds == ["archive", "filing", "letter", "notice"]
    assert listing.listing_ok and listing.dropped_invalid == 0
    z = next(d for d in listing.documents if d.kind == "archive")
    assert z.signature and "|" in z.signature
    assert [m for m, u in site.log if u == zip_url] == ["HEAD"]  # signed without downloading it


async def test_fingerprint_is_stable_and_changes_when_the_record_changes(make_client, site):
    zip_url = f"{APPS}/TransferToWeb/2026/x.zip"
    site.add_json(f"{LISTING}?f=2026-259&g=num", filings_payload([]))
    site.add_zip(zip_url, b"PK" + b"0" * 10, "Mon, 01 Jan 2026 00:00:00 GMT")
    c = case(links=[(zip_url, "Z")])
    a = await list_record_documents(c, make_client(site))
    b = await list_record_documents(c, make_client(site))
    assert a.fingerprint == b.fingerprint
    site.add_zip(zip_url, b"PK" + b"0" * 99, "Tue, 02 Jan 2026 00:00:00 GMT")  # the ZIP grew
    assert (await list_record_documents(c, make_client(site))).fingerprint != a.fingerprint
    assert fingerprint([]) == fingerprint([])


async def test_filings_endpoint_failure_is_not_fatal_when_related_docs_exist(make_client, site):
    site.add(f"{LISTING}?f=2026-259&g=num", b"oops", 500)
    c = case(links=[("https://crtc.gc.ca/eng/archive/2026/lt1.htm", "Letter")])
    listing = await list_record_documents(c, make_client(site, max_retries=0))
    assert not listing.listing_ok and len(listing.documents) == 1 and listing.source_errors


async def test_nothing_listable_raises(make_client, site):
    site.add(f"{LISTING}?f=2026-259&g=num", b"oops", 500)
    with pytest.raises(RecordListingError):
        await list_record_documents(case(), make_client(site, max_retries=0))


async def test_filings_listing_blocked_by_the_server_is_reported_not_retried(make_client, site):
    site.add(f"{LISTING}?f=2026-259&g=num", b"no", 403)
    c = case(links=[("https://crtc.gc.ca/eng/archive/2026/lt1.htm", "Letter")])
    listing = await list_record_documents(c, make_client(site, max_retries=5))
    assert "http_403" in listing.source_errors[0]
    assert len(site.requests(contains="/listes-lists/21/data")) == 1


async def test_duplicates_across_sources_are_collapsed(make_client, site):
    site.add_json(f"{LISTING}?f=2026-259&g=num", filings_payload([]))
    u = "https://crtc.gc.ca/eng/archive/2026/lt1.htm"
    c = case(links=[(u, "A"), (u, "B")], source_url=u)
    listing = await list_record_documents(c, make_client(site))
    assert len(listing.documents) == 1 and listing.dropped_duplicate == 2


async def test_head_failure_leaves_the_archive_unsigned_but_listed(make_client, site):
    zip_url = f"{APPS}/TransferToWeb/2026/x.zip"
    site.add_json(f"{LISTING}?f=2026-259&g=num", filings_payload([]))

    def head_only_405(req: httpx.Request):
        return httpx.Response(405)

    site.routes[zip_url] = head_only_405
    listing = await list_record_documents(case(links=[(zip_url, "Z")]), make_client(site))
    assert listing.documents[0].signature is None and listing.signature_failures == 1
