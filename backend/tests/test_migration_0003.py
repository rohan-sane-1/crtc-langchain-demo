"""Migration 0003 upgrades and downgrades cleanly on a real (throwaway) database."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]


def alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.fixture
async def empty_db():
    from app.config import get_settings

    url = make_url(get_settings().database_url)
    name = f"crtc_mig_{uuid.uuid4().hex[:10]}"
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    try:
        admin = await asyncpg.connect(admin_dsn, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable (docker compose up -d)")
    await admin.execute(f'CREATE DATABASE "{name}"')
    yield url.set(database=name)
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


async def connect(url):
    return await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))


async def test_upgrade_downgrade_upgrade(empty_db):
    url = empty_db.render_as_string(hide_password=False)

    r = alembic(url, "upgrade", "0002")
    assert r.returncode == 0, r.stderr
    conn = await connect(empty_db)
    cols = {r["column_name"] for r in await conn.fetch(
        "select column_name from information_schema.columns where table_name='documents'")}
    assert "numbering" not in cols
    assert "blocked" not in [r["enumlabel"] for r in await conn.fetch(
        "select enumlabel from pg_enum e join pg_type t on t.oid=e.enumtypid where typname='document_status'")]
    await conn.close()

    r = alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    conn = await connect(empty_db)
    cols = {r["column_name"] for r in await conn.fetch(
        "select column_name from information_schema.columns where table_name='documents'")}
    assert {"kind", "detected_type", "bytes", "numbering", "source_signature", "note", "attempted_at",
            "source_url", "title", "filer", "filed_date", "mime", "sha256", "status", "error",
            "n_paragraphs", "fetched_at"} <= cols
    ccols = {r["column_name"] for r in await conn.fetch(
        "select column_name from information_schema.columns where table_name='chunks'")}
    assert {"para_start", "para_end", "page", "text", "embedding", "tsv"} <= ccols
    case_cols = {r["column_name"] for r in await conn.fetch(
        "select column_name from information_schema.columns where table_name='cases'")}
    assert {"record_fingerprint", "last_synced_at"} <= case_cols
    labels = [r["enumlabel"] for r in await conn.fetch(
        "select enumlabel from pg_enum e join pg_type t on t.oid=e.enumtypid where typname='document_status'")]
    assert set(labels) == {"pending", "embedded", "failed", "skipped", "blocked"}

    # the new constraint is enforced, source_url is unique, and the new enum value works
    case_id = uuid.uuid4()
    await conn.execute("insert into cases (id, notice_number) values ($1, 'T-1')", case_id)
    await conn.execute(
        "insert into documents (id, case_id, source_url, status, numbering) values ($1,$2,'u1','blocked','source')",
        uuid.uuid4(), case_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "insert into documents (id, case_id, source_url, numbering) values ($1,$2,'u2','bogus')",
            uuid.uuid4(), case_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("insert into documents (id, case_id, source_url) values ($1,$2,'u1')", uuid.uuid4(), case_id)
    await conn.close()

    # downgrade: blocked rows become skipped, new columns vanish, old enum restored
    r = alembic(url, "downgrade", "0002")
    assert r.returncode == 0, r.stderr
    conn = await connect(empty_db)
    assert await conn.fetchval("select status::text from documents where source_url='u1'") == "skipped"
    cols = {r["column_name"] for r in await conn.fetch(
        "select column_name from information_schema.columns where table_name='documents'")}
    assert "numbering" not in cols and "source_signature" not in cols and "mime" in cols
    labels = [r["enumlabel"] for r in await conn.fetch(
        "select enumlabel from pg_enum e join pg_type t on t.oid=e.enumtypid where typname='document_status'")]
    assert set(labels) == {"pending", "embedded", "failed", "skipped"}
    await conn.close()

    # ...and it can come back up again
    r = alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    conn = await connect(empty_db)
    assert await conn.fetchval("select count(*) from documents") == 1  # data survived the round trip
    await conn.close()
    r = alembic(url, "current")
    assert "0003" in (r.stdout + r.stderr)
