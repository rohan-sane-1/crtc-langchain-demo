import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.db import get_session
from app.models import Case, Chat, User

router = APIRouter(prefix="/chats", tags=["chats"])


class ChatOut(BaseModel):
    id: uuid.UUID
    title: str
    case_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatCreate(BaseModel):
    title: str = Field(default="New chat", max_length=200)


@router.get("", response_model=list[ChatOut])
async def list_chats(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Chat]:
    result = await session.scalars(
        select(Chat).where(Chat.user_id == user.id).order_by(Chat.updated_at.desc())
    )
    return list(result)


@router.post("", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
async def create_chat(
    body: ChatCreate | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    chat = Chat(user_id=user.id, title=(body.title if body else "New chat"))
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return chat


@router.get("/{chat_id}", response_model=ChatOut)
async def get_chat(
    chat_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    # Ownership is part of the query: another user's chat id is indistinguishable from a missing one.
    chat = await session.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user.id))
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    return chat


class ChatCaseAttach(BaseModel):
    case_id: uuid.UUID


@router.patch("/{chat_id}", response_model=ChatOut)
async def attach_case(
    chat_id: uuid.UUID,
    body: ChatCaseAttach,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    """Attach a case to the user's chat. Origin/CSRF is enforced app-wide for non-GET routes.

    One chat = one case: re-attaching the same case is a no-op; a different case is a 409
    (start a new chat to switch)."""
    # Ownership is part of the query: someone else's chat looks exactly like a missing one.
    chat = await session.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user.id))
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    case = await session.get(Case, body.case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    if chat.case_id is not None and chat.case_id != case.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Chat already has a case; start a new chat")
    if chat.case_id is None:
        chat.case_id = case.id
        await session.commit()
        await session.refresh(chat)
    return chat
