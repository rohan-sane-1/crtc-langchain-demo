from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import check_origin
from app.config import get_settings
from app.db import engine
from app.routes import auth, cases, chats


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(title="CRTC Copilot", lifespan=lifespan, dependencies=[Depends(check_origin)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

app.include_router(auth.router)
app.include_router(chats.router)
app.include_router(cases.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
