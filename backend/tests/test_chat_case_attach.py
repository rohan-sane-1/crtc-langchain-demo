import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.crtc.cases import CaseRecord, upsert_cases
from app.main import app
from app.models import Case, Chat, User

ORIGIN = {"Origin": "http://localhost:3000"}


@pytest.fixture
async def env(db_session):
    """Two users, a chat each, two cases. `env.as_user(u)` switches who the API sees."""
    from app.auth import current_user
    from app.db import get_session

    s = db_session
    alice = User(id=uuid.uuid4(), google_sub="a", email="alice@example.com")
    bob = User(id=uuid.uuid4(), google_sub="b", email="bob@example.com")
    s.add_all([alice, bob])
    await s.commit()
    recs = [
        CaseRecord(notice_number=n, title=n, status="open", posted_date=date(2026, 1, 1))
        for n in ("2026-1", "2026-2")
    ]
    await upsert_cases(s, recs, datetime.now(UTC))
    case1, case2 = (await s.scalars(select(Case).order_by(Case.notice_number))).all()
    alice_chat, bob_chat = Chat(user_id=alice.id), Chat(user_id=bob.id)
    s.add_all([alice_chat, bob_chat])
    await s.commit()

    current = {"user": alice}

    async def _session():
        async with s.maker() as sess:
            yield sess

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: current["user"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        class Env:
            client = c
            users = (alice, bob)
            cases = (case1, case2)
            chats = (alice_chat, bob_chat)

            @staticmethod
            def as_user(u):
                current["user"] = u

            @staticmethod
            async def case_id_in_db(chat_id):
                async with s.maker() as sess:
                    return await sess.scalar(select(Chat.case_id).where(Chat.id == chat_id))

        yield Env
    app.dependency_overrides.clear()


async def test_attach_case_to_own_chat(env):
    chat, case = env.chats[0], env.cases[0]
    r = await env.client.patch(f"/chats/{chat.id}", json={"case_id": str(case.id)}, headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["case_id"] == str(case.id)
    assert await env.case_id_in_db(chat.id) == case.id
    got = await env.client.get(f"/chats/{chat.id}")
    assert got.json()["case_id"] == str(case.id)


async def test_other_users_chat_is_404_and_untouched(env):
    bob_chat, case = env.chats[1], env.cases[0]
    # alice is logged in; bob's chat must look exactly like a missing one
    r = await env.client.patch(
        f"/chats/{bob_chat.id}", json={"case_id": str(case.id)}, headers=ORIGIN
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Chat not found"
    assert await env.case_id_in_db(bob_chat.id) is None
    # ...and the reverse direction
    env.as_user(env.users[1])
    r = await env.client.patch(
        f"/chats/{env.chats[0].id}", json={"case_id": str(case.id)}, headers=ORIGIN
    )
    assert r.status_code == 404
    assert await env.case_id_in_db(env.chats[0].id) is None


async def test_unknown_chat_is_404(env):
    r = await env.client.patch(
        f"/chats/{uuid.uuid4()}", json={"case_id": str(env.cases[0].id)}, headers=ORIGIN
    )
    assert r.status_code == 404


async def test_nonexistent_case_id_is_404_and_chat_unchanged(env):
    chat = env.chats[0]
    r = await env.client.patch(
        f"/chats/{chat.id}", json={"case_id": str(uuid.uuid4())}, headers=ORIGIN
    )
    assert r.status_code == 404 and r.json()["detail"] == "Case not found"
    assert await env.case_id_in_db(chat.id) is None


@pytest.mark.parametrize(
    "body", [{}, {"case_id": None}, {"case_id": "not-a-uuid"}, {"case_id": 5}, {"x": 1}]
)
async def test_invalid_body_is_422(env, body):
    r = await env.client.patch(f"/chats/{env.chats[0].id}", json=body, headers=ORIGIN)
    assert r.status_code == 422


async def test_malformed_chat_id_is_422(env):
    r = await env.client.patch(
        "/chats/nope", json={"case_id": str(env.cases[0].id)}, headers=ORIGIN
    )
    assert r.status_code == 422


async def test_reattaching_same_case_is_idempotent_but_a_different_case_conflicts(env):
    chat, c1, c2 = env.chats[0], *env.cases
    url = f"/chats/{chat.id}"
    assert (
        await env.client.patch(url, json={"case_id": str(c1.id)}, headers=ORIGIN)
    ).status_code == 200
    assert (
        await env.client.patch(url, json={"case_id": str(c1.id)}, headers=ORIGIN)
    ).status_code == 200
    r = await env.client.patch(url, json={"case_id": str(c2.id)}, headers=ORIGIN)
    assert r.status_code == 409
    assert await env.case_id_in_db(chat.id) == c1.id  # unchanged: one chat = one case


async def test_missing_or_foreign_origin_is_403_csrf(env):
    chat, case = env.chats[0], env.cases[0]
    body = {"case_id": str(case.id)}
    assert (await env.client.patch(f"/chats/{chat.id}", json=body)).status_code == 403
    r = await env.client.patch(
        f"/chats/{chat.id}", json=body, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert await env.case_id_in_db(chat.id) is None


async def test_patch_without_session_cookie_is_401():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(
            f"/chats/{uuid.uuid4()}", json={"case_id": str(uuid.uuid4())}, headers=ORIGIN
        )
    assert r.status_code == 401
