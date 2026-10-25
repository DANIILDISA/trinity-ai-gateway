"""DEMO_MODE proxy — emits a realistic SSE stream without calling OpenRouter.

Used when settings.openrouter_demo_mode is true and no explicit proxy is
supplied to process_chat_request. Mirrors the StreamChunk contract of OpenRouterProxy so
the billing pipeline (reservation, finalization, balance debit, history
persistence) runs end-to-end.
"""
import asyncio
import random
from collections.abc import AsyncGenerator
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.openrouter_proxy import StreamChunk

DEMO_RESPONSES: tuple[str, ...] = (
    "Привет! Я Trinity — ваш AI-ассистент. Чем могу помочь?",
    "Это демонстрационный режим. Реальные запросы к AI-моделям будут "
    "доступны после пополнения баланса.",
    "транзакции создаются, история сохраняется.",
)

_CHAR_DELAY_SECONDS = 0.03  # ~33 chars/sec

# Canned "thinking" text streamed when a reasoning budget is requested, so the
# demo mode exercises the extended-thinking UI + billing path end-to-end.
_DEMO_THINKING = (
    "Хорошо, давайте разберёмся пошагово. Сначала уточню, что именно спрашивает "
    "пользователь, затем сформулирую ответ простыми словами."
)


async def demo_proxy_stream(
    messages: list[dict],
    model_id: str,  # noqa: ARG001 — kept to match the real proxy signature
    reasoning_budget: int | None = None,
) -> AsyncGenerator[StreamChunk, None]:
    """Yield optional reasoning chunks, then content chunks, then usage + done."""
    if reasoning_budget:
        for char in _DEMO_THINKING:
            yield StreamChunk(type="reasoning", delta=char)
            await asyncio.sleep(_CHAR_DELAY_SECONDS)

    response = random.choice(DEMO_RESPONSES)
    for char in response:
        yield StreamChunk(type="content", delta=char)
        await asyncio.sleep(_CHAR_DELAY_SECONDS)

    # Reasoning tokens are a SUBSET of output_tokens (OpenRouter contract).
    reasoning_tokens = len(_DEMO_THINKING.split()) * 2 if reasoning_budget else None
    visible_output = len(response.split()) * 2
    yield StreamChunk(
        type="usage",
        input_tokens=len(messages) * 12,
        output_tokens=visible_output + (reasoning_tokens or 0),
        reasoning_tokens=reasoning_tokens,
    )
    yield StreamChunk(type="done")


class DemoProxy:
    """Thin shim around demo_proxy_stream matching the OpenRouterProxy API."""

    def __init__(self, db: AsyncSession | None = None) -> None:
        self.db = db
        self.request_id = f"demo-{uuid4().hex[:8]}"

    async def proxy_to_openrouter(
        self,
        messages: list[dict],
        model_id: str,
        stream: bool = True,  # noqa: ARG002
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in demo_proxy_stream(messages, model_id, reasoning_budget):
            yield chunk

    async def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,  # noqa: ARG002
        plugins: list[dict] | None = None,  # noqa: ARG002 — demo ignores web search
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in demo_proxy_stream(messages, model_id, reasoning_budget):
            yield chunk
