import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

# The LangSmith SDK reads LANGSMITH_* (and the Google SDK GOOGLE_API_KEY) from os.environ;
# pydantic-settings only reads .env into Settings. Must run before the first traced call.
load_dotenv()

from app.agent.build import build_agent
from app.agent.checkpointer import open_checkpointer
from app.auth import check_origin
from app.config import get_settings
from app.db import engine
from app.routes import auth, cases, chats, messages, sync

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    scheduler = None
    with open_checkpointer(settings.checkpointer_url) as saver:
        app.state.agent = build_agent(settings, saver)  # fails fast if LLM_MODEL/key missing
        if settings.refresh_cron_enabled:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            from app.jobs.refresh import refresh_all_cases

            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                refresh_all_cases,
                CronTrigger.from_crontab(settings.refresh_cron_schedule),
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            log.info("scheduled refresh enabled: %s", settings.refresh_cron_schedule)
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
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
app.include_router(messages.router)
app.include_router(cases.router)
app.include_router(sync.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
