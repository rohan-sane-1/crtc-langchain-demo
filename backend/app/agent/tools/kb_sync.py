"""Knowledge-base freshness tools: a read-only check and a refresh that wraps the sync pipeline."""

import asyncio
import json
import time

from langchain.tools import ToolRuntime, tool

from app.agent.context import CaseContext
from app.ingest import runner
from app.ingest.check import check_case_missing_documents
from app.ingest.status import progress_registry

REFRESH_WAIT_S = 45.0


@tool
async def check_kb_status(runtime: ToolRuntime[CaseContext]) -> str:
    """Read-only: compare the CRTC's live record for this case with the knowledge base.
    Reports missing documents and documents that still need (re)processing. Makes no changes."""
    try:
        out = await check_case_missing_documents(runtime.context.case_id, runner.deps_factory())
    except Exception as e:
        return json.dumps({"status": "check_failed", "error": type(e).__name__})
    return json.dumps(out, default=str)


def _compact(result: dict) -> dict:
    keys = (
        "status",
        "total_docs_on_record",
        "embedded",
        "newly_embedded",
        "skipped",
        "blocked",
        "complete",
        "reasons",
    )
    out = {k: result.get(k) for k in keys if k in result}
    out["failed_count"] = len(result.get("failed") or [])
    for k in ("failed", "blocked"):
        items = result.get(k) or []
        out[f"{k}_docs"] = [
            {"title": i.get("title"), "reason": i.get("reason_code")} for i in items[:10]
        ]
    return out


@tool
async def refresh_knowledge_base(runtime: ToolRuntime[CaseContext]) -> str:
    """Download and index documents that are missing or need retrying for this case. Only new or
    changed documents are processed. Waits up to ~45 s; if it is still running it returns
    status "in_progress" (tell the user and check again on the next turn)."""
    case_id = runtime.context.case_id
    runner.start_background_sync(case_id)  # no-op if one is already running
    deadline = time.monotonic() + REFRESH_WAIT_S
    while time.monotonic() < deadline:
        p = progress_registry.peek(case_id)
        if p is not None and p.phase in ("done", "error"):
            break
        await asyncio.sleep(1.0)
    p = progress_registry.peek(case_id)
    if p is None or p.phase not in ("done", "error"):
        return json.dumps(
            {
                "status": "in_progress",
                "done": p.done if p else 0,
                "total": p.total if p else 0,
                "note": "Still indexing in the background; answers may be partial.",
            }
        )
    if p.phase == "error":
        return json.dumps({"status": "error", "error": p.error})
    return json.dumps(_compact(p.result or {}), default=str)
