"""Safe ZIP handling. We never extract to disk by member name and never trust declared sizes.

Archive-level rejections (``UnsafeArchive``; the whole ZIP is refused, nothing is processed):
  * not a ZIP / corrupt central directory
  * more than ``max_entries`` entries
  * zip-slip: absolute path, drive letter, backslash tricks, ``..`` segments, NUL bytes
  * symlink entries (Unix mode bits in the external attributes)
  * declared total uncompressed size over the cap, or an implausible compression ratio
  * while reading: a member that actually inflates past the cap or ratio (declared sizes lie)

Member-level skips (the member is not processed; the others are):
  * encrypted members, and members that are themselves archives (no nested extraction ever)
  * anything whose bytes are not an allowlisted type (decided by the caller via filetype)

Member names are only ever used as display text (after normalisation); a stored file's name is
always its SHA-256.
"""

import io
import stat
import zipfile
from dataclasses import dataclass
from typing import Self

from app.ingest import filetype

MIN_RATIO_SIZE = 64 * 1024  # ratio is meaningless for tiny files (100 zero bytes compress 100:1)
READ_CHUNK = 64 * 1024


class UnsafeArchive(Exception):
    """The archive as a whole must be refused. `reason` is a stable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 500
    max_total_bytes: int = 300 * 1024 * 1024
    max_member_bytes: int = 50 * 1024 * 1024
    max_ratio: float = 100.0


@dataclass(frozen=True)
class Entry:
    index: int
    name: str  # display only; may contain odd characters
    size: int  # declared uncompressed size
    compressed: int
    crc: int
    skip_reason: str | None = None  # set => member must not be processed


def _bad_name(name: str) -> bool:
    if "\x00" in name:
        return True
    unified = name.replace("\\", "/")
    if unified.startswith("/") or (len(unified) > 1 and unified[1] == ":"):
        return True
    return any(part == ".." for part in unified.split("/"))


class SafeZip:
    """Validated, read-only view of a ZIP held in memory."""

    def __init__(self, data: bytes, limits: ArchiveLimits | None = None) -> None:
        self.limits = limits or ArchiveLimits()
        self._read_total = 0
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(data))
            infos = self._zf.infolist()
        except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError, EOFError) as e:
            raise UnsafeArchive("bad_zip", type(e).__name__) from e
        if len(infos) > self.limits.max_entries:
            raise UnsafeArchive("too_many_entries", f"{len(infos)} > {self.limits.max_entries}")

        declared_total = 0
        entries: list[Entry] = []
        for i, info in enumerate(infos):
            if _bad_name(info.filename):
                raise UnsafeArchive("path_traversal", "unsafe member path")
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)):
                raise UnsafeArchive("symlink", "link or device entry")
            declared_total += info.file_size
            if declared_total > self.limits.max_total_bytes:
                raise UnsafeArchive("too_large_uncompressed", f"> {self.limits.max_total_bytes}")
            if (
                info.file_size > MIN_RATIO_SIZE
                and info.file_size / max(info.compress_size, 1) > self.limits.max_ratio
            ):
                raise UnsafeArchive("zip_bomb", "compression ratio too high")
            skip = None
            if info.flag_bits & 0x1:
                skip = "encrypted_member"
            elif info.file_size > self.limits.max_member_bytes:
                skip = "member_too_large"
            entries.append(
                Entry(i, info.filename, info.file_size, info.compress_size, info.CRC, skip)
            )
        self.entries = entries

    def read(self, entry: Entry) -> bytes:
        """Return the member's bytes, enforcing the real (not declared) size/ratio limits."""
        info = self._zf.infolist()[entry.index]
        cap = min(self.limits.max_member_bytes, self.limits.max_total_bytes - self._read_total)
        out = bytearray()
        try:
            with self._zf.open(info) as f:
                while True:
                    chunk = f.read(READ_CHUNK)
                    if not chunk:
                        break
                    out += chunk
                    if len(out) > cap:
                        raise UnsafeArchive("zip_bomb", "member inflates past the size cap")
                    if (
                        len(out) > MIN_RATIO_SIZE
                        and len(out) / max(info.compress_size, 1) > self.limits.max_ratio
                    ):
                        raise UnsafeArchive("zip_bomb", "member compression ratio too high")
        except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError, EOFError) as e:
            # CRC mismatch, truncated data, unsupported compression method, bad password flag...
            raise UnsafeArchive("corrupt_member", type(e).__name__) from e
        self._read_total += len(out)
        return bytes(out)

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def is_nested_archive(data: bytes) -> bool:
    """True if `data` is itself a (non-DOCX) archive. Nested archives are never opened."""
    return filetype.detect_type(data).type == filetype.ZIP
