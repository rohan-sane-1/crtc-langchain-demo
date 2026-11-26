"""Sandboxed parsing (real worker subprocess): text extraction, hostile and broken inputs,
timeout, crash, memory limit."""

import sys
import textwrap
from pathlib import Path

import pytest

from app.ingest.parse import ParseFailed, parse_file
from tests.fakes import make_blank_pdf, make_docx, make_encrypted_pdf, make_html, make_pdf


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def worker(tmp_path: Path, body: str) -> Path:
    """A stand-in worker script (the parent treats it exactly like the real one)."""
    p = tmp_path / "fake_worker.py"
    p.write_text(textwrap.dedent(body))
    return p


# ----------------------------------------------------------------------------- happy paths


def test_pdf_text_and_pages(tmp_path):
    pdf = make_pdf([f"{i}. Paragraph number {i} of the filing." for i in range(1, 16)])
    out = parse_file(write(tmp_path, "a.pdf", pdf), "pdf", timeout_s=60)
    assert out.pages == 2 and out.pages_without_text == 0
    texts = [b.text.strip() for b in out.blocks]
    assert texts[0].startswith("1. Paragraph number 1") and len(texts) == 15
    assert {b.page for b in out.blocks} == {1, 2}


def test_docx_text(tmp_path):
    out = parse_file(write(tmp_path, "a.docx", make_docx(["1. First.", "2. Second & third."])), "docx")
    assert [b.text for b in out.blocks] == ["1. First.", "2. Second & third."]
    assert all(b.page is None for b in out.blocks)


def test_html_is_reduced_to_text_scripts_and_markup_are_gone(tmp_path):
    html = make_html(
        ["1. Visible paragraph.", "2. Another &amp; one."],
        extra=(
            "<script>alert('pwned')</script><style>p{display:none}</style>"
            "<iframe src='https://evil.example/x'></iframe><object data='x'></object>"
            "<p onclick='steal()'>3. Third <b>bold</b> <a href='javascript:x()'>link</a></p>"
            "<noscript>hidden</noscript><template><p>template</p></template>"
        ),
    )
    out = parse_file(write(tmp_path, "a.html", html), "html")
    joined = "\n".join(b.text for b in out.blocks)
    assert "1. Visible paragraph." in joined and "2. Another & one." in joined
    assert "3. Third bold link" in joined
    for forbidden in ("alert", "pwned", "display:none", "evil.example", "steal", "javascript", "template", "hidden"):
        assert forbidden not in joined
    assert "<" not in joined


def test_html_in_windows_1252_is_decoded(tmp_path):
    html = b"<p>Qu\xe9bec \x93quoted\x94</p>"  # raw windows-1252 bytes
    out = parse_file(write(tmp_path, "a.html", html), "html")
    assert out.blocks[0].text == "Québec “quoted”"


# ----------------------------------------------------------------------------- broken / hostile


def test_malformed_pdf_is_a_clean_failure(tmp_path):
    for junk in (b"%PDF-1.7\nthis is not really a pdf", b"%PDF-" + b"\xff" * 500, make_pdf(["x" * 30])[:200]):
        with pytest.raises(ParseFailed) as e:
            parse_file(write(tmp_path, "bad.pdf", junk), "pdf")
        assert e.value.code in ("malformed", "needs_ocr")


def test_pdf_without_a_text_layer_needs_ocr(tmp_path):
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "scan.pdf", make_blank_pdf(3)), "pdf")
    assert e.value.code == "needs_ocr"


def test_encrypted_pdf_is_refused(tmp_path):
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "enc.pdf", make_encrypted_pdf()), "pdf")
    assert e.value.code == "encrypted"


def test_docx_with_an_xml_entity_bomb_or_external_entity_is_refused(tmp_path):
    billion_laughs = (
        '<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
    )
    xxe = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
    for dtd in (billion_laughs, xxe):
        data = make_docx(["&c;" if "lolz" in dtd else "&e;"], extra_xml=dtd)
        with pytest.raises(ParseFailed) as e:
            parse_file(write(tmp_path, "evil.docx", data), "docx")
        assert e.value.code == "malformed"


def test_not_a_docx_is_malformed(tmp_path):
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "x.docx", b"PK\x03\x04 garbage"), "docx")
    assert e.value.code == "malformed"


def test_empty_html_is_reported_empty(tmp_path):
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "e.html", b"<html><script>x()</script></html>"), "html")
    assert e.value.code == "empty"


# ----------------------------------------------------------------------------- the sandbox itself


def test_a_hanging_parser_times_out_and_is_killed(tmp_path):
    w = worker(tmp_path, "import time\ntime.sleep(60)\n")
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", timeout_s=1.0, worker=w)
    assert e.value.code == "timeout"


def test_a_crashing_parser_is_reported_not_propagated(tmp_path):
    w = worker(tmp_path, "import os\nos._exit(139)\n")
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", worker=w)
    assert e.value.code == "crashed"


def test_a_native_segfault_is_reported_as_a_crash(tmp_path):
    w = worker(tmp_path, "import ctypes\nctypes.string_at(0)\n")
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", worker=w)
    assert e.value.code == "crashed"


@pytest.mark.parametrize(
    "stdout",
    ["not json", "[]", '{"ok": true, "blocks": "x"}', '{"ok": true, "blocks": [{"text": 5}]}', ""],
)
def test_garbage_from_the_worker_is_rejected(tmp_path, stdout):
    w = worker(tmp_path, f"import sys\nsys.stdout.write({stdout!r})\n")
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", worker=w)
    assert e.value.code == "malformed"


def test_worker_reported_failure_codes_pass_through(tmp_path):
    w = worker(tmp_path, 'import sys\nsys.stdout.write(\'{"ok": false, "error": "needs_ocr"}\')\n')
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", worker=w)
    assert e.value.code == "needs_ocr"


def test_worker_runs_isolated_with_a_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/x")
    w = worker(
        tmp_path,
        """
        import json, os, sys
        leaked = [k for k in os.environ if k in ("GOOGLE_CLIENT_ID", "DATABASE_URL", "SESSION_SECRET")]
        sys.stdout.write(json.dumps({"ok": True, "blocks": [
            {"text": "leaked=" + ",".join(leaked)},
            {"text": "isolated=" + str(sys.flags.isolated)},
            {"text": "cwd_empty=" + str(os.listdir(".") == [])}]}))
        """,
    )
    out = parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", worker=w)
    assert [b.text for b in out.blocks] == ["leaked=", "isolated=1", "cwd_empty=True"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job object memory limit")
def test_windows_job_object_memory_limit_kills_a_runaway_parser(tmp_path):
    w = worker(tmp_path, "blob = bytearray(1500 * 1024 * 1024)\nblob[::4096] = b'x' * len(blob[::4096])\nprint('survived')\n")
    with pytest.raises(ParseFailed) as e:
        parse_file(write(tmp_path, "a.pdf", b"%PDF-"), "pdf", memory_mb=300, timeout_s=30, worker=w)
    assert e.value.code in ("crashed", "out_of_memory", "malformed")  # never "survived"


def test_a_normal_parse_still_works_under_the_default_limit(tmp_path):
    # Guards against a limit so tight that pymupdf itself can't start.
    out = parse_file(write(tmp_path, "a.pdf", make_pdf(["1. an ordinary paragraph of text here", "2. a second paragraph of text", "3. third"])), "pdf")
    assert len(out.blocks) == 3
