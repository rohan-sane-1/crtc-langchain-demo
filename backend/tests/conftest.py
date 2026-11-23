import os

# Must be set before `app` is imported (Settings is read at import time).
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("SESSION_SECRET", "x" * 48)
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("ALLOWED_EMAILS", "me@example.com")
os.environ.setdefault("CRTC_MIN_REQUEST_INTERVAL_S", "0")

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
