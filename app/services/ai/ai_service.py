"""
Central AI wrapper for IQ-ITSM.

Provides:
  - ``AIService.generate()``  — Claude completions with budget gating, retry,
    and token-usage accounting.
  - ``AIService.embed()``     — OpenAI text-embedding-3-small for pgvector KB
    search.  No budget tracking (cost negligible).
  - ``AIService.check_budget()`` / ``record_usage()`` — per-tenant monthly
    token budget backed by Redis.  Celery beat flushes Redis counters to
    ``ai_usage_daily`` nightly (future session).

Security rules enforced here:
  - User content is ALWAYS passed as a separate ``user`` message in
    ``messages[]`` — it is NEVER interpolated into the ``system`` prompt.
  - API keys are read from settings and passed to client constructors only;
    they are never logged.
  - Budget warnings (80 %) fire a WARNING log so SystemLogDBHandler
    dual-writes to the DB — visible to tenant admins.
"""

import asyncio
import logging
from typing import AsyncGenerator

import anthropic
import openai
from google import genai
from google.genai import types as genai_types

from app.config import get_settings
from app.exceptions import AIBudgetExhaustedError, ExternalServiceError

logger = logging.getLogger(__name__)


class AIService:
    """Thin, stateless wrapper around Anthropic, OpenAI, Gemini, and Groq async clients.

    Deliberately holds NO client instances on self — every method constructs
    its provider client fresh, per call. `ai_service` (bottom of this file)
    is a module-level singleton, constructed once at import time and reused
    across every Celery task's own separate `asyncio.run()` call in the same
    worker process. A client instance attribute stored at __init__ time gets
    reused across those calls too, and several of these SDKs' async clients
    eagerly bind an httpx transport (or similar) to whatever event loop first
    touches them — reused from a later, different asyncio.run() loop, that
    either hangs silently (confirmed live with google-genai) or raises
    "RuntimeError: Event loop is closed" once the client's own retry logic
    fires (confirmed live with openai's embeddings client, 2026-08-19). This
    bit app/database.py's SQLAlchemy engine too — fixed there via NullPool.
    Constructing a client is cheap (no network call happens until the first
    real request), so there's no real cost to doing it fresh every time.
    """

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Budget management
    # ------------------------------------------------------------------

    async def check_budget(self, tenant_id: str, redis) -> None:
        """Check per-tenant monthly token budget from Redis.

        Key pattern: ``ai_budget:{tenant_id}:{YYYY-MM}``

        Raises:
            AIBudgetExhaustedError: if used >= limit (hard stop).

        Emits a WARNING at 80 % utilisation so tenant admins see the alert in
        the system_logs UI before they hit the hard ceiling.
        """
        s = get_settings()
        from datetime import datetime

        month = datetime.now().strftime("%Y-%m")
        key = f"ai_budget:{tenant_id}:{month}"
        used = int(await redis.get(key) or 0)
        limit = s.AI_DEFAULT_MONTHLY_TOKEN_BUDGET

        if used >= limit:
            raise AIBudgetExhaustedError()

        if used >= int(limit * 0.8):
            logger.warning(
                "ai_budget_80_percent",
                extra={
                    "tenant_id": str(tenant_id),
                    "used_tokens": used,
                    "limit_tokens": limit,
                },
            )

    async def record_usage(
        self,
        tenant_id: str,
        redis,
        input_tokens: int,
        output_tokens: int,
        feature: str,
        model: str,
    ) -> None:
        """Increment Redis token counters and emit an INFO log.

        Two key namespaces are written:

        1. **Budget key** (existing — checked by ``check_budget``):
           ``ai_budget:{tenant_id}:{YYYY-MM}``
           Tracks total tokens consumed this calendar month.

        2. **Granular keys** (new — consumed by ``flush_daily_usage``):
           ``ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:input``
           ``ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:output``
           ``ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:calls``
           TTL: 32 days (buffer beyond month-end so the nightly job always
           catches the last day of the month regardless of timezone drift).

        Celery beat ``flush_daily_usage`` reads the granular keys nightly and
        upserts into ``ai_usage_daily`` for historical reporting.
        """
        from datetime import datetime

        now = datetime.now()
        month = now.strftime("%Y-%m")
        day = now.strftime("%Y-%m-%d")

        # 1. Monthly budget counter (existing)
        budget_key = f"ai_budget:{tenant_id}:{month}"
        await redis.incrby(budget_key, input_tokens + output_tokens)

        # 2. Granular daily counters (new)
        _TTL = 32 * 24 * 3600  # 32 days in seconds
        input_key  = f"ai_usage:{tenant_id}:{day}:{feature}:{model}:input"
        output_key = f"ai_usage:{tenant_id}:{day}:{feature}:{model}:output"
        calls_key  = f"ai_usage:{tenant_id}:{day}:{feature}:{model}:calls"

        await redis.incrby(input_key, input_tokens)
        await redis.expire(input_key, _TTL)

        await redis.incrby(output_key, output_tokens)
        await redis.expire(output_key, _TTL)

        await redis.incr(calls_key)
        await redis.expire(calls_key, _TTL)

        logger.info(
            "ai_call_completed",
            extra={
                "tenant_id": str(tenant_id),
                "ai_feature": feature,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    # ------------------------------------------------------------------
    # Claude generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        tenant_id: str,
        redis,
        messages: list[dict],
        system: str = "",
        feature: str = "general",
    ) -> str:
        """Generate a Claude completion, gated by budget and retried on failure.

        ``messages`` must be a list of ``{"role": ..., "content": ...}`` dicts.
        User-supplied content MUST be passed in a ``user`` message — callers
        must never interpolate untrusted content into the ``system`` argument.

        Retry schedule (per 06-engineering-standards.md §3.4):
          attempt 1: immediate
          attempt 2: after 15 s
          attempt 3: after 60 s
          attempt 4: after 240 s

        Raises:
            AIBudgetExhaustedError: if monthly budget is exhausted.
            ExternalServiceError("anthropic" | "gemini" | "groq"): after all
                retries are exhausted (provider depends on AI_PROVIDER).
        """
        await self.check_budget(tenant_id, redis)
        s = get_settings()
        provider = s.AI_PROVIDER
        model_name = {"gemini": s.GEMINI_MODEL, "groq": s.GROQ_MODEL}.get(provider, s.CLAUDE_MODEL)

        last_exc: Exception | None = None
        retry_delays = [0, 15, 60, 240]

        for attempt, delay in enumerate(retry_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                if provider == "gemini":
                    # Anthropic uses "assistant" for its own turns; Gemini
                    # uses "model" — everything else about the shape is the
                    # same one-message-per-turn structure.
                    contents = [
                        {
                            "role": "model" if m["role"] == "assistant" else "user",
                            "parts": [{"text": m["content"]}],
                        }
                        for m in messages
                    ]
                    # Fresh client per call — see __init__'s comment on why
                    # this can't be a reused instance attribute here.
                    gemini_client = genai.Client(api_key=s.GOOGLE_API_KEY)
                    response = await gemini_client.aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system or None,
                            max_output_tokens=s.GEMINI_MAX_OUTPUT_TOKENS,
                        ),
                    )
                    text: str = response.text or ""
                    usage = response.usage_metadata
                    input_tokens = usage.prompt_token_count if usage else 0
                    output_tokens = usage.candidates_token_count if usage else 0
                elif provider == "groq":
                    # OpenAI-compatible endpoint — same openai SDK, just a
                    # different base_url/key. Fresh client per call, same
                    # cross-event-loop reasoning as the Gemini branch above
                    # (this client is never stored on self).
                    groq_client = openai.AsyncOpenAI(api_key=s.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
                    groq_messages = ([{"role": "system", "content": system}] if system else []) + messages
                    response = await groq_client.chat.completions.create(
                        model=model_name,
                        messages=groq_messages,
                        max_tokens=s.CLAUDE_MAX_TOKENS,
                    )
                    text = response.choices[0].message.content or ""
                    input_tokens = response.usage.prompt_tokens if response.usage else 0
                    output_tokens = response.usage.completion_tokens if response.usage else 0
                else:
                    anthropic_client = anthropic.AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY)
                    response = await anthropic_client.messages.create(
                        model=model_name,
                        max_tokens=s.CLAUDE_MAX_TOKENS,
                        system=system,
                        messages=messages,
                        timeout=s.CLAUDE_TIMEOUT_SECONDS,
                    )
                    text = response.content[0].text
                    input_tokens = response.usage.input_tokens
                    output_tokens = response.usage.output_tokens

                await self.record_usage(
                    tenant_id,
                    redis,
                    input_tokens,
                    output_tokens,
                    feature,
                    model_name,
                )
                return text

            except Exception as exc:
                last_exc = exc
                logger.error(
                    "ai_generate_failed",
                    extra={
                        "tenant_id": str(tenant_id),
                        "attempt": attempt + 1,
                        "exception_type": type(exc).__name__,
                        "provider": provider,
                    },
                )

        raise ExternalServiceError(provider) from last_exc

    # ------------------------------------------------------------------
    # Streaming Claude generation
    # ------------------------------------------------------------------

    async def generate_stream(
        self,
        system_prompt: str,
        user_content: str,
        tenant_id: str,
        feature: str,
        redis,
        model: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Yield text deltas from a streaming Claude completion.

        Budget is checked before the first token is requested.  If the budget
        is exhausted the fallback message is yielded and the generator returns
        immediately.

        Usage::

            async for delta in ai_service.generate_stream(...):
                yield delta

        After the stream ends, accumulated token usage is recorded via
        ``record_usage()``.  Usage counts come from Anthropic's stream
        ``message_stop`` / ``message_delta`` events.
        """
        try:
            await self.check_budget(tenant_id, redis)
        except AIBudgetExhaustedError:
            yield AIBudgetExhaustedError.message
            return

        s = get_settings()
        resolved_model = model or s.CLAUDE_MODEL

        input_tokens: int = 0
        output_tokens: int = 0

        try:
            anthropic_client = anthropic.AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY)
            async with anthropic_client.messages.stream(
                model=resolved_model,
                max_tokens=s.CLAUDE_MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
                timeout=s.CLAUDE_TIMEOUT_SECONDS,
            ) as stream:
                async for text_delta in stream.text_stream:
                    yield text_delta

                # Collect final usage after the stream is exhausted
                final_message = await stream.get_final_message()
                input_tokens = final_message.usage.input_tokens
                output_tokens = final_message.usage.output_tokens

        except Exception as exc:
            logger.error(
                "ai_generate_stream_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            # Swallow the exception — caller receives whatever was streamed so far
            return
        finally:
            # Record usage even on partial stream (best-effort)
            if input_tokens or output_tokens:
                try:
                    await self.record_usage(
                        tenant_id,
                        redis,
                        input_tokens,
                        output_tokens,
                        feature,
                        resolved_model,
                    )
                except Exception:
                    logger.warning("ai_stream_record_usage_failed", extra={"tenant_id": str(tenant_id)})

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding via OpenAI text-embedding-3-small.

        No budget tracking — embedding calls are inexpensive and are not
        counted against the per-tenant AI token budget.

        Raises:
            ExternalServiceError("openai"): on any OpenAI API failure.
        """
        s = get_settings()
        try:
            # Fresh client per call — same cross-event-loop reasoning as the
            # gemini/groq branches in generate(): self.openai is a
            # module-level-singleton instance attribute reused across every
            # Celery task's own separate asyncio.run() call in this worker
            # process. Once a call needed its own internal retry (e.g. on a
            # 429), that retry's internals stayed bound to a stale, since-
            # closed event loop from an earlier task — raised "RuntimeError:
            # Event loop is closed" (confirmed live, 2026-08-19).
            client = openai.AsyncOpenAI(api_key=s.OPENAI_API_KEY)
            response = await client.embeddings.create(
                model=s.EMBEDDING_MODEL,
                input=text,
                dimensions=s.EMBEDDING_DIMENSIONS,
            )
            return response.data[0].embedding
        except Exception as exc:
            raise ExternalServiceError("openai") from exc


# Module-level singleton — imported by Celery tasks and API endpoints.
ai_service = AIService()
