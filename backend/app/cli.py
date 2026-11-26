"""Command line helpers.

    uv run python -m app.cli sync <notice-number> [--force]
    uv run python -m app.cli refresh-all [--scope chatted|all]
    uv run python -m app.cli ingest-file <notice-number> <path> [--title T] [--filer F] [--for-url URL]

``ingest-file`` is the fallback for documents the site would not let us download (they show up
as ``blocked``): download the file yourself in a browser, then ingest it. It goes through the
same magic-byte type check, sandboxed parse, chunking and embedding as an automatic download.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.ingest.pipeline import default_deps, ingest_local_file, sync_case_knowledge_base
from app.models import Case


async def _case_id(notice: str):
    async with SessionLocal() as s:
        case = await s.scalar(select(Case).where(Case.notice_number == notice))
    if case is None:
        raise SystemExit(f"no case with notice number {notice!r} (open the case list first)")
    return case.id


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="sync one case's knowledge base")
    p.add_argument("notice")
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("refresh-all", help="check + refresh every case people are chatting about")
    r.add_argument("--scope", choices=["chatted", "all"], default="chatted")
    f = sub.add_parser("ingest-file", help="ingest a file you downloaded by hand")
    f.add_argument("notice")
    f.add_argument("path", type=Path)
    f.add_argument("--title")
    f.add_argument("--filer")
    f.add_argument("--for-url", help="fill an existing (e.g. blocked) document with this URL")
    args = ap.parse_args(argv)
    # Shows every request (URL without query string, status, size) and nothing else.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    for noisy in ("httpx2", "httpx", "httpcore", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        if args.cmd == "refresh-all":
            from dotenv import load_dotenv

            from app.jobs.refresh import refresh_all_cases

            load_dotenv()  # LangSmith SDK reads LANGSMITH_* from the environment
            print(json.dumps(await refresh_all_cases(args.scope), indent=2, default=str))
            return 0
        case_id = await _case_id(args.notice)
        deps = default_deps()
        if args.cmd == "sync":
            out = await sync_case_knowledge_base(case_id, deps=deps, force=args.force)
        else:
            out = await ingest_local_file(
                case_id,
                args.path,
                title=args.title,
                filer=args.filer,
                for_url=args.for_url,
                deps=deps,
            )
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
