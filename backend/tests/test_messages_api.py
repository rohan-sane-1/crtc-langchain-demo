"""Chat message route with a fake agent (no LLM, no network): SSE shape, persistence, guards."""

import json
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import select

from app.agent.retrieval import Hit
from app.db import get_session
from app.main import app
from app.models import Case, Chat, ChatMessage, User
from app.routes import messages as msg_routes

ORIGIN = {"Origin": "http://localhost:3000"}


class FakeAgent:
    """Registers one citation like the search tool would, then answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def astream(self, payload, config=None, context=None, stream_mode=None):
        self.calls.append(payload["messages"][0]["content"])
        context.register(
            Hit(1, "d1", "Doc", "Rogers", "2026-01-01", 3, 4, 2, "https://x.crtc.gc.ca/a.pdf", "text")
        )
        call = {"name": "search_case_documents", "args": {}, "id": "t1"}
        yield "updates", {"model": {"messages": [AIMessage("", tool_calls=[call])]}}
        yield "messages", (AIMessageChunk(content="partial"), {"langgraph_node": "model"})

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": [AIMessage(self.answers.pop(0))]})


@pytest.fixture
async def env(db_session):
    from app.auth import current_user

    s = db_session
    async with s.maker().bind.begin() as conn:
        await conn.run_sync(
            lambda c: ChatMessage.metadata.create_all(c, tables=[ChatMessage.__table__])
        )
    alice = User(id=uuid.uuid4(), google_sub="a", email="a@example.com")
    bob = User(id=uuid.uuid4(), google_sub="b", email="b@example.com")
    case = Case(notice_number="2026-9", title="T")
    s.add_all([alice, bob, case])
    await s.commit()
    chat = Chat(user_id=alice.id, case_id=case.id)
    nocase = Chat(user_id=alice.id)
    s.add_all([chat, nocase])
    await s.commit()

    async def _session():
        async with s.maker() as sess:
            yield sess

    current = {"user": alice}
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: current["user"]
    msg_routes.chat_limiter.reset()
    app.state.agent = FakeAgent([])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(
            client=c, chat=chat, nocase=nocase, maker=s.maker, alice=alice, bob=bob, current=current
        )
    app.dependency_overrides.clear()
    del app.state.agent


def parse_sse(text: str):
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev:
            out.append((ev, data))
    return out


async def post(env, body, chat=None, headers=ORIGIN):
    return await env.client.post(f"/chats/{(chat or env.chat).id}/messages", json=body, headers=headers)


async def test_question_streams_validates_and_persists(env):
    app.state.agent = FakeAgent(["Main themes\n\nRates should fall [c1]."])
    r = await post(env, {"content": "themes?"})
    assert r.status_code == 200
    events = parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "tool_start" and "token" in names and names[-1] == "final"
    final = events[-1][1]
    assert final["answer"]["citation_valid"] and final["answer"]["n_citations"] == 1
    assert final["answer"]["paragraphs"][1]["citations"][0]["url"] == "https://x.crtc.gc.ca/a.pdf"

    hist = (await env.client.get(f"/chats/{env.chat.id}/messages")).json()
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[1]["structured"]["citation_valid"] is True


async def test_uncited_answer_gets_one_repair_pass(env):
    agent = FakeAgent(["Rates should fall.", "Rates should fall [c1]."])
    app.state.agent = agent
    final = parse_sse((await post(env, {"content": "q"})).text)[-1][1]
    assert len(agent.calls) == 2 and "failed citation validation" in agent.calls[1]
    assert final["answer"]["citation_valid"]


async def test_open_turn_is_not_stored_as_user_message_and_only_runs_once(env):
    app.state.agent = FakeAgent(["Knowledge base is up to date: 5 documents searchable."])
    assert parse_sse((await post(env, {"open": True})).text)[-1][0] == "final"
    again = await post(env, {"open": True})
    assert parse_sse(again.text) == [("skip", {"reason": "already opened"})]
    async with env.maker() as s:
        rows = list(await s.scalars(select(ChatMessage)))
    assert [m.role for m in rows] == ["assistant"]


async def test_guards(env):
    assert (await post(env, {"content": "x"}, chat=env.nocase)).status_code == 409
    assert (await post(env, {"content": "x" * 2001})).status_code == 422
    assert (await post(env, {})).status_code == 422
    assert (await post(env, {"content": "x"}, headers={})).status_code == 403  # no Origin
    env.current["user"] = env.bob  # another user's chat looks like a missing one
    assert (await post(env, {"content": "x"})).status_code == 404
    assert (await env.client.get(f"/chats/{env.chat.id}/messages")).status_code == 404


async def test_rate_limit(env, monkeypatch):
    monkeypatch.setattr(msg_routes, "chat_limiter", msg_routes.SlidingWindowLimiter(1, 600))
    app.state.agent = FakeAgent(["A [c1].", "B [c1]."])
    assert (await post(env, {"content": "1"})).status_code == 200
    again = await post(env, {"content": "2"})
    assert again.status_code == 429 and "retry-after" in again.headers
