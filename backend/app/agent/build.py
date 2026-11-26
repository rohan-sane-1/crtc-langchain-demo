from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.context import CaseContext
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.kb_sync import check_kb_status, refresh_knowledge_base
from app.agent.tools.search import get_paragraphs, search_case_documents
from app.config import Settings


def build_agent(settings: Settings, checkpointer: Any):
    if not settings.llm_model:
        raise RuntimeError("LLM_MODEL is not set (copy an exact Gemini model id from AI Studio)")
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")
    def chat(name: str) -> ChatGoogleGenerativeAI:
        return ChatGoogleGenerativeAI(
            model=name, api_key=settings.google_api_key, temperature=0.2, max_retries=2
        )

    fallbacks = [
        chat(m.strip()) for m in settings.llm_fallback_models.split(",") if m.strip()
    ]
    return create_deep_agent(
        model=chat(settings.llm_model),
        middleware=[ModelFallbackMiddleware(*fallbacks)] if fallbacks else [],
        tools=[check_kb_status, refresh_knowledge_base, search_case_documents, get_paragraphs],
        system_prompt=SYSTEM_PROMPT,
        context_schema=CaseContext,
        checkpointer=checkpointer,
        name="crtc_copilot",
    )
