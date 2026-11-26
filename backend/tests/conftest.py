import os

# Must be set before `app` is imported (Settings is read at import time).
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("SESSION_SECRET", "x" * 48)
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("ALLOWED_EMAILS", "me@example.com")
os.environ.setdefault("CRTC_MIN_REQUEST_INTERVAL_S", "0")
os.environ.setdefault("LANGSMITH_TRACING", "false")  # never post traces from tests
os.environ.setdefault("EMBEDDING_DIM", "384")

import json
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FIXTURES = Path(__file__).parent / "fixtures"
LIST_URL = "https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data"


@pytest.fixture
def sample_bytes() -> bytes:
    return (FIXTURES / "crtc_list_sample.json").read_bytes()


@pytest.fixture
def sample_rows(sample_bytes) -> list[dict]:
    return json.loads(sample_bytes)["data"]


async def _public(host: str) -> list[str]:
    return ["52.60.1.1"]


@pytest.fixture
def make_client():
    """Build a CrtcClient wired to an in-memory transport (no network, no DNS, no sleeping).
    Returns (client, requests_seen)."""
    from app.crtc.client import CrtcClient

    def factory(handler, *, resolver=_public, **kw):
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request):
            seen.append(request)
            return handler(request)

        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        client = CrtcClient(
            transport=httpx.MockTransport(wrapped),
            resolver=resolver,
            sleep=fake_sleep,
            min_interval_s=kw.pop("min_interval_s", 0),
            **kw,
        )
        client.seen = seen  # type: ignore[attr-defined]
        client.slept = slept  # type: ignore[attr-defined]
        return client

    return factory


@pytest.fixture
def list_client(make_client, sample_bytes):
    """Client that serves the saved fixture at the real list URL."""
    return make_client(lambda req: httpx.Response(200, content=sample_bytes))


@pytest.fixture
async def db_session():
    """A throwaway Postgres database (created and dropped per test) with users/cases/chats tables.
    Skips if the dev Postgres from docker-compose isn't reachable."""
    from sqlalchemy import text

    from app.config import get_settings
    from app.models import Case, Chat, User

    url = make_url(get_settings().database_url)
    name = f"crtc_test_{uuid.uuid4().hex[:10]}"
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    try:
        admin = await asyncpg.connect(admin_dsn, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable (docker compose up -d)")
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(url.set(database=name))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(
            lambda c: Case.metadata.create_all(
                c, tables=[User.__table__, Case.__table__, Chat.__table__]
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.maker = maker  # type: ignore[attr-defined]
        yield session
    await engine.dispose()
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


# --------------------------------------------------------------------------- Phase 3 fixtures


@pytest.fixture
async def kb_db():
    """A throwaway Postgres with the FULL schema (incl. pgvector, documents, chunks).
    Yields an object with .engine and .maker. Skips if Postgres isn't reachable."""
    from types import SimpleNamespace

    from sqlalchemy import text

    import app.models  # noqa: F401  (registers all tables on Base.metadata)
    from app.config import get_settings
    from app.db import Base

    url = make_url(get_settings().database_url)
    name = f"crtc_test_{uuid.uuid4().hex[:10]}"
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    try:
        admin = await asyncpg.connect(admin_dsn, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable (docker compose up -d)")
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(url.set(database=name))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(engine=engine, maker=maker, name=name, url=url.set(database=name))
    await engine.dispose()
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


@pytest.fixture
def site():
    from tests.fakes import FakeSite

    return FakeSite()


@pytest.fixture
def embedder():
    from tests.fakes import HashEmbedder

    return HashEmbedder()


@pytest.fixture
def make_deps(kb_db, make_client, site, embedder, tmp_path):
    """Deps wired to the throwaway DB, the fake site, a fake embedder and a temp originals dir."""
    from app.ingest.pipeline import Deps
    from app.ingest.store import OriginalsStore

    def factory(**over):
        client_kw = over.pop("client_kw", {})
        deps = Deps(
            engine=kb_db.engine,
            session_factory=kb_db.maker,
            client=make_client(site, **client_kw),
            embedder=over.pop("embedder", embedder),
            store=OriginalsStore(tmp_path / "originals"),
            **over,
        )
        return deps

    return factory


@pytest.fixture
def make_case(kb_db):
    from app.models import Case

    async def factory(notice="2026-259", **kw):
        async with kb_db.maker() as s, s.begin():
            case = Case(notice_number=notice, title=f"Case {notice}", status="open", **kw)
            s.add(case)
        return case

    return factory
