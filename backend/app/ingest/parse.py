"""Safe document parsing: run the extractor in a separate, constrained process.

A hostile or broken file can hang, crash or exhaust memory in the parser. That must cost us
exactly one document, so each parse runs ``parse_worker.py`` in its own interpreter
(``python -I``) with:

* a wall-clock timeout (the process tree is killed on expiry),
* a memory limit: a Windows **Job object** (process + job memory limit, active-process limit,
  kill-on-close) here, ``RLIMIT_AS`` set inside the worker on POSIX,
* a scrubbed environment, an empty working directory and no inherited stdin,
* a bounded, schema-validated JSON result (the worker itself is treated as untrusted).

``ParseFailed.code`` is a stable machine-readable reason (``timeout``, ``crashed``,
``needs_ocr``, ``encrypted``, ``malformed``, ...). Document text is never logged.

Known limitation (Windows): the Job object limits committed memory, so a parser that tries
to allocate past the limit gets an allocation failure/crash (reported as ``crashed`` or
``out_of_memory``) rather than a graceful message. It does not limit CPU beyond the timeout
or disk writes (the worker never writes).
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings
from app.ingest.chunk import Block

log = logging.getLogger(__name__)

WORKER_SCRIPT = Path(__file__).with_name("parse_worker.py")
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_BLOCKS = 200_000
MAX_BLOCK_CHARS = 2_000_000


class ParseFailed(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class _WorkerBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    page: int | None = Field(default=None, ge=1, le=100_000)
    text: str = Field(max_length=MAX_BLOCK_CHARS)


class _WorkerResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ok: bool
    error: str | None = Field(default=None, max_length=64)
    kind: str | None = None
    pages: int | None = Field(default=None, ge=0, le=1_000_000)
    pages_without_text: int = Field(default=0, ge=0, le=1_000_000)
    blocks: list[_WorkerBlock] = Field(default_factory=list, max_length=MAX_BLOCKS)


@dataclass(frozen=True)
class ParsedDocument:
    blocks: list[Block]
    pages: int | None
    pages_without_text: int


# --------------------------------------------------------------------------- Windows Job object


class _WindowsJob:
    """Job object: memory limit, at most a few processes, everything killed when closed."""

    def __init__(self, memory_mb: int) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        ACTIVE_PROCESS, PROCESS_MEMORY, JOB_MEMORY, KILL_ON_CLOSE = 0x8, 0x100, 0x200, 0x2000
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        self._job = k32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = EXT()
        info.BasicLimitInformation.LimitFlags = (
            ACTIVE_PROCESS | PROCESS_MEMORY | JOB_MEMORY | KILL_ON_CLOSE
        )
        info.BasicLimitInformation.ActiveProcessLimit = 4  # venv launcher + interpreter (+ slack)
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
        info.JobMemoryLimit = memory_mb * 1024 * 1024
        # 9 = JobObjectExtendedLimitInformation
        if not k32.SetInformationJobObject(self._job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k32.CloseHandle(self._job)
            raise OSError(err, "SetInformationJobObject failed")

    def assign(self, process_handle: int) -> None:
        if not self._k32.AssignProcessToJobObject(self._job, int(process_handle)):
            raise OSError(self._ct.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        self._k32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        if self._job:
            self._k32.CloseHandle(self._job)
            self._job = None


def _resume_windows_process(handle: int) -> None:
    import ctypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess(int(handle))


def _scrubbed_env(memory_mb: int) -> dict[str, str]:
    env = {
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "KB_PARSE_MEMORY_MB": str(memory_mb),
    }
    for key in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "PATH", "LANG"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_worker(
    kind: str, path: Path, timeout_s: float, memory_mb: int, worker: Path
) -> bytes:
    """Blocking. Returns the worker's stdout or raises ParseFailed."""
    cmd = [sys.executable, "-I", str(worker), kind, str(path)]
    is_windows = sys.platform == "win32"
    job: _WindowsJob | None = None
    flags = 0
    if is_windows:
        # CREATE_SUSPENDED: the process must be in the Job before it runs a single instruction.
        flags = subprocess.CREATE_NO_WINDOW | 0x00000004
        try:
            job = _WindowsJob(memory_mb)
        except OSError as e:  # fail closed: do not parse unconstrained
            raise ParseFailed("sandbox_unavailable", f"job object: {e}") from e
    with tempfile.TemporaryDirectory(prefix="kbparse-") as cwd:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=_scrubbed_env(memory_mb),
                creationflags=flags,
                close_fds=True,
            )
        except OSError as e:
            if job:
                job.close()
            raise ParseFailed("crashed", f"could not start worker: {type(e).__name__}") from e
        try:
            if job is not None:
                try:
                    job.assign(proc._handle)  # type: ignore[attr-defined]
                except OSError as e:
                    proc.kill()
                    raise ParseFailed("sandbox_unavailable", f"assign: {e}") from e
                _resume_windows_process(proc._handle)  # type: ignore[attr-defined]
            try:
                out, _ = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                if job is not None:
                    job.terminate()
                proc.kill()
                proc.communicate()
                raise ParseFailed("timeout", f"parser exceeded {timeout_s:g}s") from e
            if proc.returncode != 0:
                raise ParseFailed("crashed", f"worker exited with status {proc.returncode}")
            if len(out) > MAX_OUTPUT_BYTES:
                raise ParseFailed("malformed", "worker output too large")
            return out
        finally:
            if proc.poll() is None:
                proc.kill()
            if job is not None:
                job.close()


def _validate(raw: bytes) -> ParsedDocument:
    try:
        result = _WorkerResult.model_validate(json.loads(raw))
    except (ValueError, ValidationError, RecursionError) as e:
        raise ParseFailed("malformed", "worker produced invalid output") from e
    if not result.ok:
        raise ParseFailed(result.error or "malformed")
    return ParsedDocument(
        blocks=[Block(b.text, b.page) for b in result.blocks],
        pages=result.pages,
        pages_without_text=result.pages_without_text,
    )


def parse_file(
    path: Path,
    kind: str,
    *,
    timeout_s: float | None = None,
    memory_mb: int | None = None,
    worker: Path | None = None,
) -> ParsedDocument:
    """Blocking parse of `path` (a pdf/docx/html file we saved ourselves)."""
    s = get_settings()
    out = _run_worker(
        kind,
        path,
        timeout_s or s.kb_parse_timeout_s,
        memory_mb or s.kb_parse_memory_mb,
        worker or WORKER_SCRIPT,
    )
    return _validate(out)


async def parse_file_async(path: Path, kind: str, **kw) -> ParsedDocument:
    return await asyncio.to_thread(parse_file, path, kind, **kw)
