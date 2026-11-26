"""Content-addressed storage for downloaded originals.

Files are stored as ``<data_dir>/originals/<sha[:2]>/<sha256>`` with NO extension. The name is
computed from the bytes, never taken from a URL, header or archive member, so there is no
path-injection surface. Writes are atomic (temp file + rename) so a crash never leaves a
half-written original. The directory is git-ignored (see .gitignore: ``backend/data/``).
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from app.config import get_settings

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OriginalsStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (get_settings().data_dir / "originals")).resolve()

    def path_for(self, sha: str) -> Path:
        if not _SHA_RE.match(sha):  # only ever called with our own digests; belt and braces
            raise ValueError("not a sha256 hex digest")
        return self.root / sha[:2] / sha

    def has(self, sha: str) -> bool:
        return self.path_for(sha).is_file()

    def save(self, data: bytes) -> str:
        """Store `data`, return its sha256. Idempotent."""
        sha = sha256_hex(data)
        target = self.path_for(sha)
        if target.is_file():
            return sha
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def read(self, sha: str) -> bytes:
        return self.path_for(sha).read_bytes()
