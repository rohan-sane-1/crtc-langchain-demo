"""Magic-byte type detection, claimed-vs-detected rejection, and ZIP safety."""

import io
import struct
import zipfile

import pytest

from app.ingest import filetype
from app.ingest.archive import ArchiveLimits, SafeZip, UnsafeArchive, is_nested_archive
from tests.fakes import make_docx, make_html, make_pdf, make_zip

# ----------------------------------------------------------------------------- file types


def test_detects_by_content_not_by_name():
    assert filetype.detect_type(make_pdf(["1. hello"])).type == filetype.PDF
    assert filetype.detect_type(make_docx(["hello"])).type == filetype.DOCX
    assert filetype.detect_type(make_html(["hello"])).type == filetype.HTML
    assert filetype.detect_type(make_zip({"a.txt": b"x"})).type == filetype.ZIP
    assert filetype.detect_type(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest").type == filetype.OLE
    assert filetype.detect_type(b"{\\rtf1 hello}").type == filetype.RTF
    assert filetype.detect_type(b"MZ\x90\x00 an executable").type == filetype.UNKNOWN
    assert filetype.detect_type(b"").type == filetype.UNKNOWN
    assert filetype.detect_type(b"plain text, no markup").type == filetype.UNKNOWN


def test_pdf_with_leading_junk_and_html_with_bom_are_recognised():
    assert filetype.detect_type(b"\n\n" + make_pdf(["x"])).type == filetype.PDF
    assert filetype.detect_type(b"\xef\xbb\xbf  <html><body>x</body></html>").type == filetype.HTML


def test_magic_byte_mismatch_is_rejected():
    html_pretending_to_be_pdf = make_html(["Security check"])
    detected = filetype.detect_type(html_pretending_to_be_pdf)
    claimed = filetype.claimed_type("https://crtc.gc.ca/x/report.pdf", "application/pdf")
    assert claimed == filetype.PDF
    assert filetype.check_type(detected, claimed).startswith("type_mismatch: claimed pdf, detected html")


def test_pdf_pretending_to_be_html_and_exe_pretending_to_be_pdf_are_rejected():
    pdf = filetype.detect_type(make_pdf(["x"]))
    assert filetype.check_type(pdf, filetype.claimed_type("https://crtc.gc.ca/x.htm", None))
    exe = filetype.detect_type(b"MZ\x90\x00\x03")
    assert filetype.check_type(exe, filetype.PDF) == "type_mismatch: claimed pdf, detected unknown"


def test_conflicting_claims_are_a_mismatch_and_absent_claims_are_not():
    claimed = filetype.claimed_type("https://crtc.gc.ca/x.htm", "application/pdf")
    assert claimed == "conflict"
    assert filetype.check_type(filetype.detect_type(make_pdf(["x"])), claimed)
    assert filetype.check_type(filetype.detect_type(make_pdf(["x"])), None) is None
    # octet-stream / .aspx make no claim
    assert filetype.claimed_type("https://x.crtc.gc.ca/Doc.aspx?DMID=1", "application/octet-stream") is None


def test_docx_is_a_zip_container_but_a_plain_zip_is_not_a_docx():
    docx = filetype.detect_type(make_docx(["x"]))
    assert filetype.check_type(docx, filetype.ZIP) is None
    assert filetype.check_type(filetype.detect_type(make_zip({"a": b"x"})), filetype.DOCX) is None
    assert filetype.detect_type(make_zip({"word/document.xml": b"x"})).type == filetype.ZIP


def test_dot_doc_claims_a_word_document_which_may_be_docx_or_ole():
    # 14 of 17 real ".doc" files on the CRTC site are DOCX: a strict equality rule would reject them.
    claimed = filetype.claimed_type("", None, "Reply.doc")
    assert claimed == filetype.WORD
    assert filetype.check_type(filetype.detect_type(make_docx(["x"])), claimed) is None
    assert filetype.check_type(filetype.detect_type(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x"), claimed) is None
    # ...but a ".doc" that is really a PDF or HTML page is still refused.
    assert filetype.check_type(filetype.detect_type(make_pdf(["x"])), claimed)
    assert filetype.check_type(filetype.detect_type(make_html(["x"])), claimed)


# ----------------------------------------------------------------------------- zip safety


def test_normal_zip_lists_and_reads_members():
    data = make_zip({"DM#1 - a.pdf": make_pdf(["1. one"]), "b.html": make_html(["two"])})
    with SafeZip(data) as z:
        assert [e.name for e in z.entries] == ["DM#1 - a.pdf", "b.html"]
        assert filetype.detect_type(z.read(z.entries[0])).type == filetype.PDF


def raw_zip(entries: list[tuple[str, bytes, int]]) -> bytes:
    """entries: (name, data, unix_mode). Lets us forge symlinks and odd names."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            z.writestr(info, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    "name",
    ["../evil.pdf", "a/../../evil.pdf", "/etc/passwd", "C:/Windows/evil.pdf", "..\\evil.pdf",
     "a\\..\\..\\evil.pdf", "/abs/path.pdf"],  # (zipfile itself truncates names at a NUL byte)
)
def test_zip_slip_is_rejected(name):
    with pytest.raises(UnsafeArchive) as e:
        SafeZip(raw_zip([("fine.pdf", b"x", 0o100644), (name, b"x", 0o100644)]))
    assert e.value.reason == "path_traversal"


def test_symlink_entries_are_rejected():
    with pytest.raises(UnsafeArchive) as e:
        SafeZip(raw_zip([("fine.pdf", b"x", 0o100644), ("link", b"/etc/passwd", 0o120777)]))
    assert e.value.reason == "symlink"


def test_too_many_entries_is_rejected():
    data = make_zip({f"f{i}.txt": b"x" for i in range(30)})
    with pytest.raises(UnsafeArchive) as e:
        SafeZip(data, ArchiveLimits(max_entries=10))
    assert e.value.reason == "too_many_entries"


def test_zip_bomb_by_declared_ratio_is_rejected():
    bomb = make_zip({"zeros.bin": b"\x00" * (50 * 1024 * 1024)})  # ~50 KB compressed
    assert len(bomb) < 200_000
    with pytest.raises(UnsafeArchive) as e:
        SafeZip(bomb)
    assert e.value.reason == "zip_bomb"


def test_total_uncompressed_size_cap():
    data = make_zip({f"{i}.bin": bytes(range(256)) * 1024 for i in range(4)})  # 4 x 256 KB
    with pytest.raises(UnsafeArchive) as e:
        SafeZip(data, ArchiveLimits(max_total_bytes=600 * 1024, max_ratio=10_000))
    assert e.value.reason == "too_large_uncompressed"


def test_lying_headers_are_caught_while_reading():
    """A member whose central directory claims a tiny size but inflates hugely."""
    data = bytearray(make_zip({"zeros.bin": b"\x00" * (20 * 1024 * 1024)}))
    # Patch the central-directory uncompressed size (offset 24 in the CD header) to 1000.
    cd = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, cd + 24, 1000)
    limits = ArchiveLimits(max_member_bytes=5 * 1024 * 1024, max_ratio=10_000)
    with SafeZip(bytes(data), limits) as z:  # passes the header checks...
        with pytest.raises(UnsafeArchive) as e:
            z.read(z.entries[0])  # ...but not the real-size check
        assert e.value.reason in ("zip_bomb", "corrupt_member")


def test_member_over_cap_is_skipped_not_fatal():
    data = make_zip({"big.bin": bytes(range(256)) * 4096, "small.txt": b"hi"})
    with SafeZip(data, ArchiveLimits(max_member_bytes=100_000, max_ratio=10_000)) as z:
        by = {e.name: e for e in z.entries}
        assert by["big.bin"].skip_reason == "member_too_large"
        assert by["small.txt"].skip_reason is None


def test_encrypted_member_is_skipped():
    data = bytearray(make_zip({"s.pdf": b"x" * 100}))
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # set the "encrypted" flag bit
        i = data.find(sig)
        data[i + off] |= 0x1
    with SafeZip(bytes(data)) as z:
        assert z.entries[0].skip_reason == "encrypted_member"


def test_nested_archives_are_never_opened():
    inner = make_zip({"deep.pdf": make_pdf(["x"])})
    assert is_nested_archive(inner)
    assert not is_nested_archive(make_docx(["x"]))  # a DOCX is a document, not a nested archive
    assert not is_nested_archive(make_pdf(["x"]))


def test_corrupt_and_non_zip_data_is_refused():
    for junk in (b"not a zip", make_zip({"a": b"x"})[:-30], b""):
        with pytest.raises(UnsafeArchive) as e:
            SafeZip(junk)
        assert e.value.reason == "bad_zip"
