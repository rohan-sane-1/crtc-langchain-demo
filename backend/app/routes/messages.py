"""Chat messages: history, streaming turns (SSE) and feedback.

POST /chats/{id}/messages   {content} or {open: true}  -> text/event-stream
GET  /chats/{id}/messages                              -> display history
POST /chats/{id}/messages/{mid}/feedback {score: 0|1}  -> LangSmith user_score

The agent turn runs in its own asyncio task and pushes events onto a queue; the SSE response
only drains the queue. So a closed tab does not lose the answer: it is still validated, saved
and traced, and shows up in the history.

SSE events: status | token | tool_start | tool_end | progress | final | error | skip.
"""

import asyncio
import hashlib
import json
import logging
import math
import uuid
from datetime import datetime
from typing import Any

import langsmith as ls
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.agent.citations import build_structured_answer, repair_prompt
from app.agent.context import CaseContext
from app.agent.prompts import OPEN_INSTRUCTION
from app.auth import current_user
from app.config import get_settings
from app.db import get_session
from app.ingest.status import progress_registry
from app.models import Case, Chat, ChatMessage, User
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chats", tags=["messages"])

_s = get_settings()
chat_limiter = SlidingWindowLimiter(_s.chat_rate_limit, _s.chat_rate_window_s)
_active: set[uuid.UUID] = set()  # chats with a turn in flight
_tasks: set[asyncio.Task] = set()

TOOL_LABELS = {
    "check_kb_status": "Checking knowledge base…",
    "refresh_knowledge_base": "Adding new documents to the knowledge base…",
    "search_case_documents": "Searching documents…",
    "get_paragraphs": "Reading paragraphs…",
    "task": "Researching…",
}


class MessageIn(BaseModel):
    content: str | None = Field(default=None, max_length=get_settings().max_question_chars)
    open: bool = False

    @model_validator(mode="after")
    def _one_of(self):
        if not self.open and not (self.content and self.content.strip()):
            raise ValueError("content is required")
        return self


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    structured: dict | None
    run_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


async def _own_chat(session: AsyncSession, chat_id: uuid.UUID, user: User) -> Chat:
    chat = await session.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user.id))
    if chat is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Chat not found")
    return chat


@router.get("/{chat_id}/messages", response_model=list[MessageOut])
async def list_messages(
    chat_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ChatMessage]:
    await _own_chat(session, chat_id, user)
    rows = await session.scalars(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.created_at)
    )
    return list(rows)


def user_hash(user_id: uuid.UUID) -> str:
    """Pseudonymous id for traces. Never the email."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


def _sse(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, default=str)}


@router.post("/{chat_id}/messages")
async def post_message(
    chat_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    chat = await _own_chat(session, chat_id, user)
    if chat.case_id is None:
        raise HTTPException(http_status.HTTP_409_CONFLICT, "Pick a case for this chat first")
    case = await session.get(Case, chat.case_id)
    if case is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Case not found")
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "Agent is not available")

    if body.open:
        existing = await session.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.chat_id == chat_id)
        )
        if existing:
            return EventSourceResponse(_one(_sse("skip", {"reason": "already opened"})))
    wait = chat_limiter.hit(str(user.id))
    if wait is not None:
        raise HTTPException(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many messages; try again shortly",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    if chat_id in _active:
        raise HTTPException(http_status.HTTP_409_CONFLICT, "Still answering the previous message")

    question = OPEN_INSTRUCTION if body.open else (body.content or "").strip()
    if not body.open:
        session.add(ChatMessage(chat_id=chat_id, role="user", content=question))
        if chat.title == "New chat":
            chat.title = question[:60]
        await session.commit()

    ctx = CaseContext(
        user_id=user.id, chat_id=chat_id, case_id=case.id, notice_number=case.notice_number
    )
    queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
    _active.add(chat_id)
    maker = async_sessionmaker(bind=session.bind, expire_on_commit=False)
    task = asyncio.create_task(_run_turn(agent, ctx, question, body.open, queue, maker))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return EventSourceResponse(_drain(queue), ping=15)


async def _one(item: dict[str, str]):
    yield item


async def _drain(queue: asyncio.Queue):
    while (item := await queue.get()) is not None:
        yield item


async def _stream_agent(agent, payload, config, ctx, queue) -> None:
    """Run one agent invocation, forwarding tokens and tool events to the queue."""
    pollers: dict[str, asyncio.Task] = {}

    async def poll_progress() -> None:
        while True:
            p = progress_registry.peek(ctx.case_id)
            if p is not None:
                queue.put_nowait(_sse("progress", p.as_dict()))
            await asyncio.sleep(2)

    try:
        async for mode, chunk in agent.astream(
            payload,
            config=config,
            context=ctx,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                msg, meta = chunk
                if (
                    isinstance(msg, AIMessageChunk)
                    and meta.get("langgraph_node") == "model"
                    and not msg.tool_call_chunks
                ):
                    text = str(msg.text)
                    if text:
                        queue.put_nowait(_sse("token", {"text": text}))
            elif mode == "updates" and isinstance(chunk, dict):
                for update in chunk.values():
                    for m in (update or {}).get("messages", []) if isinstance(update, dict) else []:
                        if isinstance(m, AIMessage):
                            for tc in m.tool_calls:
                                name = tc["name"]
                                queue.put_nowait(
                                    _sse(
                                        "tool_start",
                                        {"name": name, "label": TOOL_LABELS.get(name, name)},
                                    )
                                )
                                if name == "refresh_knowledge_base":
                                    pollers[tc["id"]] = asyncio.create_task(poll_progress())
                        elif getattr(m, "type", "") == "tool":
                            if t := pollers.pop(getattr(m, "tool_call_id", ""), None):
                                t.cancel()
                            queue.put_nowait(_sse("tool_end", {"name": getattr(m, "name", "")}))
    finally:
        for t in pollers.values():
            t.cancel()


async def _final_text(agent, config) -> str:
    state = await agent.aget_state(config)
    for m in reversed(state.values.get("messages", [])):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return str(m.text).strip()
    return ""


async def _run_turn(
    agent, ctx: CaseContext, question: str, is_open: bool, queue, maker: async_sessionmaker
) -> None:
    s = get_settings()
    tracing = s.langsmith_tracing and bool(s.langsmith_api_key)
    metadata = {
        "user_hash": user_hash(ctx.user_id),
        "thread_id": str(ctx.chat_id),  # groups turns into one LangSmith thread
        "chat_id": str(ctx.chat_id),
        "case_id": str(ctx.case_id),
        "notice_number": ctx.notice_number,
        "app_version": s.app_version,
        "model": s.llm_model,
        "turn_kind": "open" if is_open else "question",
    }
    tags = ["chat", f"case:{ctx.notice_number}"]
    config = {
        "configurable": {"thread_id": str(ctx.chat_id)},
        "recursion_limit": s.agent_recursion_limit,
        "metadata": metadata,
        "tags": tags,
    }
    run_id: str | None = None
    structured: dict | None = None
    text = ""
    try:
        async with ls.trace(
            name="crtc_chat_turn",
            run_type="chain",
            inputs={"question": question},
            metadata=metadata,
            tags=tags,
            # Destination comes from LANGSMITH_AGENT_ID / LANGSMITH_AGENT_ENVIRONMENT (env).
        ) as rt:
            run_id = str(rt.id) if tracing else None
            payload = {"messages": [{"role": "user", "content": question}]}
            await _stream_agent(agent, payload, config, ctx, queue)
            text = await _final_text(agent, config)
            structured = build_structured_answer(
                text, ctx.registry, require_citations=not is_open
            )
            if not structured["citation_valid"]:
                queue.put_nowait(_sse("status", {"label": "Checking citations…"}))
                payload = {"messages": [{"role": "user", "content": repair_prompt(structured["warnings"])}]}
                await _stream_agent(agent, payload, config, ctx, queue)
                text = await _final_text(agent, config) or text
                structured = build_structured_answer(
                    text, ctx.registry, require_citations=not is_open
                )
            rt.add_outputs(
                {
                    "answer": text,
                    "citation_valid": structured["citation_valid"],
                    "n_citations": structured["n_citations"],
                    "sources": [h.as_dict() | {"text": h.text[:300]} for h in ctx.registry.values()],
                }
            )
        message_id = await _save_answer(maker, ctx.chat_id, text, structured, run_id)
        queue.put_nowait(
            _sse("final", {"message_id": message_id, "run_id": run_id, "answer": structured})
        )
        if run_id:
            await asyncio.to_thread(_auto_feedback, run_id, structured)
    except Exception as e:
        log.exception("chat turn failed (%s)", type(e).__name__)
        queue.put_nowait(
            _sse("error", {"message": "Sorry, something went wrong answering that. Please try again."})
        )
    finally:
        _active.discard(ctx.chat_id)
        queue.put_nowait(None)


async def _save_answer(
    maker: async_sessionmaker, chat_id: uuid.UUID, text: str, structured: dict, run_id: str | None
) -> str:
    async with maker() as session:
        row = ChatMessage(
            chat_id=chat_id, role="assistant", content=text, structured=structured, run_id=run_id
        )
        session.add(row)
        chat = await session.get(Chat, chat_id)
        if chat is not None:
            chat.updated_at = func.now()
        await session.commit()
        return str(row.id)


def _auto_feedback(run_id: str, structured: dict) -> None:
    try:
        client = ls.Client()
        client.create_feedback(
            run_id, key="citation_valid", score=1.0 if structured["citation_valid"] else 0.0
        )
        client.create_feedback(run_id, key="n_citations", value=structured["n_citations"])
    except Exception as e:  # observability must never break the chat
        log.warning("auto feedback failed (%s)", type(e).__name__)


class FeedbackIn(BaseModel):
    score: int = Field(ge=0, le=1)


@router.post("/{chat_id}/messages/{message_id}/feedback", status_code=http_status.HTTP_202_ACCEPTED)
async def message_feedback(
    chat_id: uuid.UUID,
    message_id: uuid.UUID,
    body: FeedbackIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await _own_chat(session, chat_id, user)
    msg = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.chat_id == chat_id,
            ChatMessage.role == "assistant",
        )
    )
    if msg is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Message not found")
    sent = False
    if msg.run_id:
        try:
            await asyncio.to_thread(
                ls.Client().create_feedback, msg.run_id, key="user_score", score=float(body.score)
            )
            sent = True
        except Exception as e:
            log.warning("user feedback failed (%s)", type(e).__name__)
    return {"recorded": sent}
