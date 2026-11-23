import json
import logging

import pytest

from app.crtc.cases import CrtcParseError, parse_cases
from app.crtc.client import is_allowed_host
from app.crtc.html import extract_links, html_to_text, safe_url
from tests.conftest import LIST_URL

BASE = "https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data"


def good(**over) -> dict:
    rec = {
        "EventNo": '<a href="https://crtc.gc.ca/eng/archive/2026/2026-1.htm">2026-1</a>',
        "Subject": "A subject",
        "PostedDt": "2026-01-02",
        "IntrDt": "2026-02-03",
        "Doc": "",
        "PT": "Telecom",
        "ET": "Notice of Consultation",
        "Status": "Open",
        "Applct": "",
        "Prov": "",
        "City": "",
        "Search": "2026-1",
    }
    rec.update(over)
    return rec


def payload(*records) -> bytes:
    return json.dumps({"data": list(records)}).encode()


# ----------------------------------------------------------------------------- fixture


def test_parses_saved_sample(sample_bytes, sample_rows):
    res = parse_cases(sample_bytes, LIST_URL)
    assert res.total_raw == len(sample_rows)
    # The sample deliberately contains two rows with the same notice number (a real quirk).
    assert res.dropped_duplicate == 1
    assert res.dropped_invalid == 0
    assert len(res.records) == len(sample_rows) - 1
    by = {r.notice_number: r for r in res.records}
    assert {"2026-259", "2026-294", "2026-47", "202605519"} <= by.keys()
    assert {r.status for r in res.records} == {"open", "closed", "other"}


def test_recent_open_record_details(sample_bytes):
    by = {r.notice_number: r for r in parse_cases(sample_bytes, LIST_URL).records}
    r = by["2026-259"]
    assert r.status == "open" and r.sector == "Telecom"
    assert r.proceeding_type == "Notice of Consultation"
    assert str(r.posted_date) == "2026-10-06"
    assert (
        str(r.intervention_deadline) == "2026-11-30"
    )  # first date; the "New deadline" label ignored
    assert r.source_url == "https://crtc.gc.ca/eng/archive/2026/2026-259.htm"
    assert "<" not in r.title and "Reducing red tape" in r.title
    # 2026-47 has two notice links (original + amendment); the first is the canonical source.
    assert by["2026-47"].source_url.endswith("/2026-47.htm")
    assert len(by["2026-47"].notice_links) == 2


def test_record_without_anchor_has_no_source_url_and_n_a_deadline(sample_bytes):
    recs = parse_cases(sample_bytes, LIST_URL).records
    foa = next(r for r in recs if r.notice_number == "202600783")
    assert foa.status == "other"  # upstream "All"
    assert foa.source_url is None
    assert foa.intervention_deadline is None  # "N/A"


def test_every_extracted_link_is_https_and_allowlisted(sample_bytes):
    res = parse_cases(sample_bytes, LIST_URL)
    links = [l for r in res.records for l in r.notice_links + r.document_links]
    assert links
    for link in links:
        assert link.url.startswith("https://")
        assert is_allowed_host(link.url.split("/")[2])
        assert " " not in link.url  # raw spaces in upstream hrefs are percent-encoded
    assert res.rejected_links > 0  # the sample includes odd hrefs which must be discarded
    assert res.upgraded_links > 0  # ...and http:// links that were upgraded


def test_parse_logs_counts_only(sample_bytes, caplog):
    caplog.set_level(logging.INFO)
    parse_cases(sample_bytes, LIST_URL)
    assert "dropped_invalid=0 dropped_duplicate=1" in caplog.text
    assert "2026-259" not in caplog.text


# ----------------------------------------------------------------------------- bad records


def test_invalid_and_malformed_records_are_dropped_and_counted():
    records = [
        good(Search="ok-1"),
        "a string, not an object",
        None,
        ["list"],
        good(Search="bad-date", PostedDt="not-a-date"),
        good(Search="empty-date", PostedDt=""),
        good(Search="", Subject="no notice number"),
        good(Search="no-subject", Subject=""),
        good(Search="list-subject", Subject=["a", "b"]),
        good(Search="dict-field", PT={"x": 1}),
        good(Search="bool-field", PT=True),
        good(Search="huge", Subject="x" * 300_000),
        good(Search="ok-2"),
    ]
    res = parse_cases(payload(*records), BASE)
    assert [r.notice_number for r in res.records] == ["ok-1", "ok-2"]
    assert res.total_raw == 13
    assert res.dropped_invalid == 11
    assert res.dropped == 11


def test_missing_none_and_oddly_typed_fields_are_handled():
    rec = good(Search=20260001, Applct=None, Prov=None, IntrDt=None)  # int + None
    del rec["Doc"]  # missing key
    del rec["City"]
    res = parse_cases(payload(rec), BASE)
    r = res.records[0]
    assert r.notice_number == "20260001"
    assert r.applicant is None and r.province is None and r.city is None
    assert r.intervention_deadline is None and r.document_links == []


def test_duplicate_notice_numbers_keep_first_and_are_counted():
    res = parse_cases(payload(good(Subject="first"), good(Subject="second")), BASE)
    assert [r.title for r in res.records] == ["first"]
    assert res.dropped_duplicate == 1


def test_unknown_status_becomes_other():
    for raw in ("All", "ALL", "", "Pending", "  open "):
        res = parse_cases(payload(good(Status=raw)), BASE)
        assert res.records[0].status == ("open" if raw.strip() == "open" else "other")


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"rows": []}', b'{"data": "x"}', b"null", b'{"data": [1, 2, 3]}'],
)
def test_unusable_payload_raises(body):
    with pytest.raises(CrtcParseError):
        parse_cases(body, BASE)


def test_empty_data_list_is_valid_and_empty():
    assert parse_cases(b'{"data": []}', BASE).records == []


# ----------------------------------------------------------------------------- link filtering


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "http://evil.example/x.htm",  # http AND off-host: the upgrade never widens the allowlist
        "http://crtc.gc.ca.evil.example/x.htm",
        "http://crtc.gc.ca:8080/x.htm",  # http on a non-80 port
        "http://user:pw@crtc.gc.ca/x.htm",
        "ttp://www.crtc.gc.ca/x",  # truncated scheme seen in real data
        "https://evil.example/x",  # off-host
        "https://crtc.gc.ca.evil.example/x",
        "https://notcrtc.gc.ca/x",
        "//evil.example/x",  # protocol-relative to another host
        "https://crtc.gc.ca@evil.example/x",
        "https://user:pw@crtc.gc.ca/x",
        "https://crtc.gc.ca:8443/x",
        "www.crtc.gc.ca/broadcast/x.htm",  # scheme-less host (real data): ambiguous, rejected
        "",
        "   ",
    ],
)
def test_dangerous_or_offhost_links_are_filtered(href):
    assert safe_url(href, BASE) is None
    links, rejected, _ = extract_links(f'<a href="{href}">x</a>', BASE)
    assert links == [] and rejected == 1


def test_good_links_are_kept_and_relative_urls_resolved():
    html = (
        '<a href="/eng/archive/2017/lt170811.htm">relative</a>'
        '<a href="https://WWW.CRTC.GC.CA/a.htm">upper</a>'
        '<a href="  https://crtc.gc.ca/with-space.htm  ">padded</a>'
        '<a href="https://applications.crtc.gc.ca/TransferToWeb/2011/2011-31_related documents.zip">z</a>'
        '<a href="https://crtc.gc.ca/a.htm">dup1</a><a href="https://crtc.gc.ca/a.htm">dup2</a>'
    )
    links, rejected, _ = extract_links(html, BASE)
    assert rejected == 0
    assert [l.url for l in links] == [
        "https://applications.crtc.gc.ca/eng/archive/2017/lt170811.htm",
        "https://www.crtc.gc.ca/a.htm",
        "https://crtc.gc.ca/with-space.htm",
        "https://applications.crtc.gc.ca/TransferToWeb/2011/2011-31_related%20documents.zip",
        "https://crtc.gc.ca/a.htm",
    ]


def test_link_extraction_via_full_record_pipeline():
    rec = good(
        Doc=(
            '<a href="javascript:alert(1)">Evil</a><br>'
            '<a href="data:text/html;base64,AAAA">Data</a><br>'
            '<a href="http://crtc.gc.ca/old.htm">Old http</a><br>'
            '<a href="https://evil.example/x.pdf">Off host</a><br>'
            '<a href="https://applications.crtc.gc.ca/TransferToWeb/2026/x.zip">Good</a>'
        ),
        EventNo='<a href="javascript:void(0)">2026-1</a>',
    )
    res = parse_cases(payload(rec), BASE)
    r = res.records[0]
    assert [l.url for l in r.document_links] == [
        "https://crtc.gc.ca/old.htm",  # http -> https upgrade on an allowlisted host
        "https://applications.crtc.gc.ca/TransferToWeb/2026/x.zip",
    ]
    assert r.source_url is None and r.notice_links == []
    assert res.rejected_links == 4 and res.upgraded_links == 1


def test_link_count_per_field_is_capped():
    html = "".join(f'<a href="https://crtc.gc.ca/{i}">{i}</a>' for i in range(500))
    links, _, _ = extract_links(html, BASE)
    assert len(links) == 50


# ----------------------------------------------------------------------------- text hygiene


def test_html_in_subject_is_stripped():
    rec = good(
        Subject=(
            '<script>alert("x")</script><b>Bold</b> <img src=x onerror=alert(1)>'
            '<a href="javascript:alert(2)">link text</a> &lt;not-a-tag&gt; &amp; more'
            "<style>body{display:none}</style>"
        )
    )
    title = parse_cases(payload(rec), BASE).records[0].title
    assert title == "Bold link text <not-a-tag> & more"
    assert "alert" not in title and "onerror" not in title and "display" not in title


def test_br_separated_subject_becomes_single_line():
    rec = good(Subject="8740-N1-1<br><br>Correction<br><br>General Tariff")
    assert parse_cases(payload(rec), BASE).records[0].title == (
        "8740-N1-1 | Correction | General Tariff"
    )


def test_unterminated_and_weird_markup_does_not_crash():
    for s in ["<div", "<<<>>>", "<a href='x", "a < b > c", "<!-- unterminated", "<![CDATA[x"]:
        assert isinstance(html_to_text(s, max_len=100), str)


def test_control_and_zero_width_characters_are_removed():
    rec = good(
        Subject="Re\u200bne\u200dw\u2060al\ufeff \u202eevil\u202c\x00\x07 text\u00a0here\u2028x",
        Applct="Bell\u200b Canada\x1b[31m",
    )
    r = parse_cases(payload(rec), BASE).records[0]
    assert r.title == "Renewal evil text here x"
    assert r.applicant == "Bell Canada[31m"  # ESC stripped; the rest is inert plain text


def test_unicode_is_nfc_normalised():
    decomposed = "Québec"  # e + combining acute
    r = parse_cases(payload(good(Subject=decomposed)), BASE).records[0]
    assert r.title == "Québec"


def test_field_lengths_are_capped():
    rec = good(Subject="word " * 5000, Applct="A" * 5000, City="C" * 5000, PT="T" * 5000)
    r = parse_cases(payload(rec), BASE).records[0]
    assert len(r.title) <= 2000 and r.title.endswith("…")
    assert len(r.applicant) <= 200 and len(r.city) <= 200 and len(r.sector) <= 100


def test_notice_number_too_long_is_dropped():
    res = parse_cases(payload(good(Search="9" * 500), good(Search="ok")), BASE)
    assert [r.notice_number for r in res.records] == ["ok"]
    assert res.dropped_invalid == 1


# ----------------------------------------------------------------------------- http -> https


@pytest.mark.parametrize(
    "href",
    [
        "http://www.crtc.gc.ca/eng/archive/2020/2020-101.htm",
        "Http://www.crtc.gc.ca/eng/archive/2020/2020-101.htm",
        "HTTP://WWW.CRTC.GC.CA/eng/archive/2020/2020-101.htm",
        " http://www.crtc.gc.ca/eng/archive/2020/2020-101.htm ",
        "http://www.crtc.gc.ca:80/eng/archive/2020/2020-101.htm",
    ],
)
def test_http_on_allowlisted_host_is_upgraded_to_https(href):
    assert safe_url(href, BASE) == "https://www.crtc.gc.ca/eng/archive/2020/2020-101.htm"
    links, rejected, upgraded = extract_links(f'<a href="{href}">x</a>', BASE)
    assert [lk.url for lk in links] == ["https://www.crtc.gc.ca/eng/archive/2020/2020-101.htm"]
    assert (rejected, upgraded) == (0, 1)


def test_https_links_are_not_counted_as_upgraded():
    _, _, upgraded = extract_links('<a href="https://crtc.gc.ca/a.htm">x</a>', BASE)
    assert upgraded == 0


def test_upgrade_keeps_query_and_encodes_spaces():
    url = safe_url("http://applications.crtc.gc.ca/TransferToWeb/2011/a b.zip?x=1&y=2", BASE)
    assert url == "https://applications.crtc.gc.ca/TransferToWeb/2011/a%20b.zip?x=1&y=2"


# ----------------------------------------------------------------------------- deadlines


def test_deadline_comes_from_intrdt_not_the_stale_subject_date():
    rec = good(
        Subject="Call for comments - Deadline for submission of interventions: 20 November 2026",
        IntrDt="2026-11-30<br><br>New deadline Interventions<br><br><a href='x'>Submit</a>",
    )
    r = parse_cases(payload(rec), BASE).records[0]
    assert str(r.intervention_deadline) == "2026-11-30" and r.deadline_source == "intrdt"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Notice - Deadline for submission of interventions: 20 November 2026", "2026-11-20"),
        (
            "Hearing - Deadline for submission of interventions/comments/answers: 7 January 2027",
            "2027-01-07",
        ),
        (
            "Call - Deadline for submission of interventions: 18 September 2026 - Deadline for "
            "submission of replies: 18 December 2026 | 2026-47-1 - Revised deadline for "
            "submission of interventions: 1 December 2026",
            "2026-12-01",  # the revised deadline wins; the replies deadline is not used
        ),
        ("Deadline for submission of interventions: 31 February 2026", None),  # impossible date
        ("Deadline for submission of interventions: 3 Smarch 2026", None),
        ("No dates here", None),
    ],
)
def test_deadline_falls_back_to_subject_text(subject, expected):
    r = parse_cases(payload(good(Subject=subject, IntrDt="N/A")), BASE).records[0]
    assert (str(r.intervention_deadline) if r.intervention_deadline else None) == expected
    assert r.deadline_source == ("subject" if expected else None)


def test_open_without_deadline_is_counted():
    res = parse_cases(
        payload(
            good(Search="a", IntrDt="N/A", Subject="x"),
            good(Search="b"),
            good(Search="c", Status="Closed", IntrDt=""),
        ),
        BASE,
    )
    assert res.open_without_deadline == 1


# ----------------------------------------------------------------------------- applicant


@pytest.mark.parametrize(
    ("et", "applct", "subject", "expected"),
    [
        (
            "Part 1 Licence Renewal Applications",
            "Bell Media Inc.",
            "Bell Media Inc.<br>Radio",
            "Bell Media Inc.",
        ),
        (
            "Part 1 Applications",
            "",
            "8662-C450-202605809<br>Application for stay<br>Canadian Telecommunications Association",
            "Canadian Telecommunications Association",
        ),
        (
            "Part 1 Applications",
            "",
            "Cogeco inc.<br>Amendment<br>Quebec<br>CKVD-FM;<br>Click on the application number",
            "Cogeco inc.",
        ),
        (
            "Basic International Telecommunications Services (BITS)",
            "",
            "8190-A277-202605353<br>BOSATEL COMMUNICATIONS<br>Licence Approved: 2026-10-20",
            "BOSATEL COMMUNICATIONS",
        ),
        (
            "Tariff Applications",
            "",
            "8740-N1-202604405<br>Residential Offer<br>General Tariff<br>Northwestel Inc.",
            "Northwestel Inc.",
        ),
        (
            "Agreement Applications",
            "",
            "8340-N7-202203636<br>Some agreement<br>North Frontenac Telephone Corporation Ltd.",
            "North Frontenac Telephone Corporation Ltd.",
        ),
        (
            "Administrative - Ownership",
            "",
            "Rock 95 Broadcasting Ltd.<br>Change in ownership<br>Click on",
            "Rock 95 Broadcasting Ltd.",
        ),
        (
            "Notice of Consultation",
            "",
            "Call for comments<br>Something",
            None,
        ),  # several parties: unknown
        ("Part 1 Applications", "", "Only one line", None),  # unrecognised layout: don't guess
    ],
)
def test_applicant_derivation(et, applct, subject, expected):
    rec = good(ET=et, Applct=applct, Subject=subject)
    assert parse_cases(payload(rec), BASE).records[0].applicant == expected


def test_province_document_links_and_year_are_parsed():
    rec = good(
        Prov="Ontario",
        PostedDt="2019-05-06",
        Doc='<a href="http://www.crtc.gc.ca/a.htm">A</a><a href="javascript:x">B</a>',
    )
    r = parse_cases(payload(rec), BASE).records[0]
    assert r.province == "Ontario"
    assert [lk.url for lk in r.document_links] == ["https://www.crtc.gc.ca/a.htm"]
