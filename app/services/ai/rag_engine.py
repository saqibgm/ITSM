"""
RAG engine for the Virtual Agent (S4B.1).

Provides two operations:
  1. retrieve_context  — embed the query, vector-search the KB, return excerpts
  2. generate_response — build a user-turn prompt with KB context + history,
                         call Claude, return the response text

Security rules:
  - System prompt is 100% static.
  - KB excerpts, history and user message are ALWAYS in the user turn.
  - Excerpt length is capped at 300 characters to control token usage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import UUID

from app.exceptions import AIBudgetExhaustedError
from app.repositories.kb_repo import KBRepository
from app.services.ai.ai_service import AIService
from app.services.ai.embedder import EmbedderService

if TYPE_CHECKING:
    from app.models.virtual_agent import VirtualAgentMessage

logger = logging.getLogger(__name__)

_EXCERPT_MAX = 300  # characters

_SYSTEM_PROMPT = (
    "You are a helpful IT support assistant. "
    "Answer questions using the knowledge base context provided. "
    "If you cannot answer from the context, suggest creating a ticket or "
    "speaking with a human agent. Be concise and professional."
)


class RAGEngine:
    FALLBACK_RESPONSE = (
        "I'm unable to fully process your request right now. "
        "Would you like me to create a support ticket or connect you with a human agent?"
    )

    # ------------------------------------------------------------------
    # retrieve_context
    # ------------------------------------------------------------------

    async def retrieve_context(
        self,
        query: str,
        tenant_id: str,
        db,
        embedder: EmbedderService,
    ) -> list[dict]:
        """Embed *query* and return up to 5 nearest KB articles as context dicts.

        Returns:
            List of ``{"article_id": str, "title": str, "excerpt": str}``.
            Returns an empty list on any failure (budget exhausted, network
            error, no articles found) — never raises.
        """
        try:
            query_embedding = await embedder.embed_text(query)
        except AIBudgetExhaustedError:
            logger.warning(
                "rag_embed_budget_exhausted",
                extra={"tenant_id": str(tenant_id)},
            )
            return []
        except Exception as exc:
            logger.warning(
                "rag_embed_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            return []

        try:
            kb_repo = KBRepository(db)
            articles = await kb_repo.suggest_by_vector(
                tenant_id=UUID(tenant_id),
                query_embedding=query_embedding,
                limit=5,
            )
        except Exception as exc:
            logger.warning(
                "rag_vector_search_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            return []

        context: list[dict] = []
        for article in articles:
            body: str = article.body or ""
            excerpt = body[:_EXCERPT_MAX]
            context.append(
                {
                    "article_id": str(article.id),
                    "title": article.title,
                    "excerpt": excerpt,
                }
            )

        return context

    # ------------------------------------------------------------------
    # generate_response
    # ------------------------------------------------------------------

    async def generate_response(
        self,
        user_message: str,
        context_articles: list[dict],
        session_history: list["VirtualAgentMessage"],
        ai_service: AIService,
        tenant_id: str,
        redis,
    ) -> str:
        """Generate an assistant reply using KB context and conversation history.

        The system prompt is static.  All user-supplied content (KB excerpts,
        history, user message) is placed in the user turn to satisfy the
        security rule that user content never appears in the system prompt.

        Returns:
            The assistant's reply text, or FALLBACK_RESPONSE on any failure.
        """
        # --- Build KB context block ---
        if context_articles:
            kb_lines = []
            for i, art in enumerate(context_articles, start=1):
                kb_lines.append(
                    f"{i}. [{art['title']}]\n{art['excerpt']}"
                )
            kb_block = "\n\n".join(kb_lines)
        else:
            kb_block = "No relevant knowledge base articles found."

        # --- Build conversation history block (last 5 messages) ---
        recent = session_history[-5:] if len(session_history) > 5 else session_history
        if recent:
            history_lines = []
            for msg in recent:
                role_label = "User" if msg.role == "user" else "Assistant"
                history_lines.append(f"{role_label}: {msg.content}")
            history_block = "\n".join(history_lines)
        else:
            history_block = "No previous messages."

        user_turn = (
            "[KNOWLEDGE BASE CONTEXT]\n"
            f"{kb_block}\n\n"
            "[CONVERSATION HISTORY]\n"
            f"{history_block}\n\n"
            "[USER MESSAGE]\n"
            f"{user_message}"
        )

        try:
            response = await ai_service.generate(
                tenant_id=tenant_id,
                redis=redis,
                messages=[{"role": "user", "content": user_turn}],
                system=_SYSTEM_PROMPT,
                feature="virtual_agent_response",
            )
            return response
        except AIBudgetExhaustedError:
            logger.warning(
                "rag_generate_budget_exhausted",
                extra={"tenant_id": str(tenant_id)},
            )
            return self.FALLBACK_RESPONSE
        except Exception as exc:
            logger.warning(
                "rag_generate_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            return self.FALLBACK_RESPONSE

    # ------------------------------------------------------------------
    # generate_response_stream
    # ------------------------------------------------------------------

    async def generate_response_stream(
        self,
        user_message: str,
        context_articles: list[dict],
        session_history: list,
        ai_service: "AIService",
        tenant_id: str,
        redis,
    ) -> AsyncGenerator[str, None]:
        """Streaming variant of generate_response.

        Yields text deltas as they arrive from the Claude streaming API.
        On budget exhaustion, yields FALLBACK_RESPONSE and returns.
        On other errors, yields FALLBACK_RESPONSE and returns.

        Identical prompt construction to ``generate_response`` — system
        prompt is 100% static, user content is in the user turn.
        """
        # --- Build KB context block ---
        if context_articles:
            kb_lines = []
            for i, art in enumerate(context_articles, start=1):
                kb_lines.append(
                    f"{i}. [{art['title']}]\n{art['excerpt']}"
                )
            kb_block = "\n\n".join(kb_lines)
        else:
            kb_block = "No relevant knowledge base articles found."

        # --- Build conversation history block (last 5 messages) ---
        recent = session_history[-5:] if len(session_history) > 5 else session_history
        if recent:
            history_lines = []
            for msg in recent:
                role_label = "User" if msg.role == "user" else "Assistant"
                history_lines.append(f"{role_label}: {msg.content}")
            history_block = "\n".join(history_lines)
        else:
            history_block = "No previous messages."

        user_turn = (
            "[KNOWLEDGE BASE CONTEXT]\n"
            f"{kb_block}\n\n"
            "[CONVERSATION HISTORY]\n"
            f"{history_block}\n\n"
            "[USER MESSAGE]\n"
            f"{user_message}"
        )

        try:
            async for delta in ai_service.generate_stream(
                system_prompt=_SYSTEM_PROMPT,
                user_content=user_turn,
                tenant_id=tenant_id,
                feature="virtual_agent_response",
                redis=redis,
            ):
                yield delta
        except AIBudgetExhaustedError:
            logger.warning(
                "rag_stream_budget_exhausted",
                extra={"tenant_id": str(tenant_id)},
            )
            yield self.FALLBACK_RESPONSE
        except Exception as exc:
            logger.warning(
                "rag_stream_generate_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            yield self.FALLBACK_RESPONSE
