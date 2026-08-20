"""
KBCurator — STORM-style multi-perspective synthesis subgraph for the KB
wiki-curation pipeline (KB_WIKI_CURATION_RAG_PLAN §4.3, Phase 1; durable
human-review pause added Phase 2).

Given a raw source document (title + body), produces a curated wiki-style
draft article: perspective discovery -> multi-perspective interview against
the source (+ related existing wiki pages) -> outline -> citation-grounded
section writing -> polish -> lint check -> human review.

The graph pauses at human_review via LangGraph's interrupt() and is resumed
later (a separate Celery task invocation, possibly a different worker
process) via Command(resume=...) against the same thread_id, backed by a
Postgres checkpointer (app/services/ai/kb_curation_checkpointer.py) so the
pause survives process restarts. On reject, the graph loops back to
write_sections with reviewer notes injected into state rather than starting
over — perspective discovery/interview don't depend on reviewer feedback.

Security contract (mirrors app/services/ai/kb_drafter.py, now retired):
  - Every system prompt is 100% static — no user/source content is ever
    interpolated into it.
  - Source document text, related-wiki context, and persona/question text
    are always placed in the `user` turn of the messages list.
"""

import json
import logging
from typing import TYPE_CHECKING, Any, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

if TYPE_CHECKING:
    from app.services.ai.ai_service import AIService
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

logger = logging.getLogger(__name__)

_MAX_PERSONAS = 3
_MAX_ROUNDS = 2
_FEATURE = "kb_curation"


def _parse_json_response(raw: str) -> Any:
    """Strip markdown fences (if any) and parse JSON."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner_lines = [ln for ln in lines[1:] if not ln.strip().startswith("```")]
        stripped = "\n".join(inner_lines).strip()
    return json.loads(stripped)


class CurationState(TypedDict):
    tenant_id: str
    source_title: str
    source_body: str
    related_context: str  # formatted text block of related existing wiki pages, "" if none
    perspectives: list[str]
    qa_transcript: list[dict]
    outline: list[str]
    draft_sections: list[dict]
    final_title: str
    final_body: str
    citations: list[str]
    lint_findings: list[str]
    reviewer_notes: Optional[str]
    review_decision: Optional[str]


class CurationResult(TypedDict):
    paused: bool
    title: str
    body: str
    citations: list[str]
    lint_findings: list[str]


class KBCurator:
    """Runs the STORM-style synthesis subgraph + durable human-review pause."""

    def __init__(self, ai_service: "AIService", redis: Any):
        self._ai = ai_service
        self._redis = redis
        self._builder = self._build_graph()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def curate(
        self,
        thread_id: str,
        checkpointer: "AsyncPostgresSaver",
        tenant_id: str,
        source_title: str,
        source_body: str,
        related_context: str = "",
    ) -> CurationResult:
        """Start a new synthesis run. Pauses at human_review (interrupt())."""
        initial_state: CurationState = {
            "tenant_id": tenant_id,
            "source_title": source_title,
            "source_body": source_body,
            "related_context": related_context,
            "perspectives": [],
            "qa_transcript": [],
            "outline": [],
            "draft_sections": [],
            "final_title": "",
            "final_body": "",
            "citations": [],
            "lint_findings": [],
            "reviewer_notes": None,
            "review_decision": None,
        }
        graph = self._builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        result = await graph.ainvoke(initial_state, config=config)
        return self._interpret(result, source_title)

    async def resume(
        self,
        thread_id: str,
        checkpointer: "AsyncPostgresSaver",
        decision: str,
        notes: Optional[str],
    ) -> CurationResult:
        """Resume a paused run with a reviewer decision ('approve' | 'reject')."""
        graph = self._builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        result = await graph.ainvoke(
            Command(resume={"decision": decision, "notes": notes}), config=config
        )
        return self._interpret(result, "")

    @staticmethod
    def _interpret(result: dict, fallback_title: str) -> CurationResult:
        return {
            "paused": bool(result.get("__interrupt__")),
            "title": result.get("final_title") or fallback_title,
            "body": result.get("final_body", ""),
            "citations": result.get("citations", []),
            "lint_findings": result.get("lint_findings", []),
        }

    # ------------------------------------------------------------------
    # Graph wiring
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(CurationState)
        graph.add_node("discover_perspectives", self._discover_perspectives)
        graph.add_node("interview", self._interview)
        graph.add_node("generate_outline", self._generate_outline)
        graph.add_node("write_sections", self._write_sections)
        graph.add_node("polish", self._polish)
        graph.add_node("lint_check", self._lint_check)
        graph.add_node("human_review", self._human_review)

        graph.set_entry_point("discover_perspectives")
        graph.add_edge("discover_perspectives", "interview")
        graph.add_edge("interview", "generate_outline")
        graph.add_edge("generate_outline", "write_sections")
        graph.add_edge("write_sections", "polish")
        graph.add_edge("polish", "lint_check")
        graph.add_edge("lint_check", "human_review")
        graph.add_conditional_edges(
            "human_review",
            self._route_after_review,
            {"write_sections": "write_sections", END: END},
        )
        return graph

    @staticmethod
    def _route_after_review(state: CurationState) -> str:
        return "write_sections" if state.get("review_decision") == "reject" else END

    # ------------------------------------------------------------------
    # Node: perspective discovery
    # ------------------------------------------------------------------

    async def _discover_perspectives(self, state: CurationState) -> dict:
        system = (
            "You help curate a company support knowledge base. Given the TITLE "
            "of a source document (not its content), propose up to 3 short reader "
            "personas who would each approach this topic differently — e.g. a "
            "customer new to the feature, an agent troubleshooting an edge case, "
            "someone checking a policy detail. Respond with ONLY a JSON array of "
            "1-3 short strings, each describing one persona in under 12 words. "
            "Do not include any text outside the JSON array."
        )
        user_content = f"Document title: {state['source_title']}"
        raw = await self._ai.generate(
            tenant_id=state["tenant_id"], redis=self._redis,
            messages=[{"role": "user", "content": user_content}],
            system=system, feature=_FEATURE,
        )
        try:
            personas = _parse_json_response(raw)
            if not isinstance(personas, list) or not personas:
                raise ValueError("Response is not a non-empty JSON array")
            personas = [str(p) for p in personas[:_MAX_PERSONAS]]
        except Exception as exc:
            logger.warning(
                "kb_curator_perspectives_parse_failure",
                extra={"exception_type": type(exc).__name__, "exception": str(exc)},
            )
            personas = ["A user encountering this topic for the first time"]
        return {"perspectives": personas}

    # ------------------------------------------------------------------
    # Node: multi-perspective interview (question -> grounded answer)
    # ------------------------------------------------------------------

    async def _interview(self, state: CurationState) -> dict:
        transcript: list[dict] = []
        question_system = (
            "You are role-playing a specific reader persona. Ask ONE short, "
            "concrete question you would want answered about the given topic, "
            "based on your persona and (if provided) the questions already "
            "asked. Respond with ONLY the question text, no preamble."
        )
        answer_system = (
            "You are an expert answering strictly from the SOURCE DOCUMENT and "
            "RELATED CONTEXT provided in the user turn below — never from "
            "outside knowledge. If the source does not answer the question, "
            "say so explicitly rather than guessing. Respond with ONLY the "
            "answer text, in 1-3 sentences."
        )

        for persona in state["perspectives"]:
            prior_qs = ""
            for round_idx in range(_MAX_ROUNDS):
                q_user = (
                    f"Persona: {persona}\n"
                    f"Topic: {state['source_title']}\n"
                    f"Questions already asked this round: {prior_qs or 'none'}"
                )
                question = await self._ai.generate(
                    tenant_id=state["tenant_id"], redis=self._redis,
                    messages=[{"role": "user", "content": q_user}],
                    system=question_system, feature=_FEATURE,
                )
                question = question.strip()

                a_user = (
                    f"SOURCE DOCUMENT (title: {state['source_title']}):\n{state['source_body']}\n\n"
                    f"RELATED CONTEXT:\n{state['related_context'] or '(none)'}\n\n"
                    f"QUESTION: {question}"
                )
                answer = await self._ai.generate(
                    tenant_id=state["tenant_id"], redis=self._redis,
                    messages=[{"role": "user", "content": a_user}],
                    system=answer_system, feature=_FEATURE,
                )
                answer = answer.strip()

                transcript.append({"persona": persona, "question": question, "answer": answer})
                prior_qs = f"{prior_qs}; {question}" if prior_qs else question

        return {"qa_transcript": transcript}

    # ------------------------------------------------------------------
    # Node: outline generation
    # ------------------------------------------------------------------

    async def _generate_outline(self, state: CurationState) -> dict:
        system = (
            "You outline knowledge-base articles. Given a source document title "
            "and a Q&A transcript from interviewing several reader personas, "
            "produce a hierarchical outline as a JSON array of 3-6 short section "
            "heading strings covering what the article should address. Respond "
            "with ONLY the JSON array."
        )
        qa_text = "\n".join(
            f"- ({qa['persona']}) Q: {qa['question']} A: {qa['answer']}"
            for qa in state["qa_transcript"]
        )
        user_content = f"Title: {state['source_title']}\n\nQ&A transcript:\n{qa_text or '(none)'}"
        raw = await self._ai.generate(
            tenant_id=state["tenant_id"], redis=self._redis,
            messages=[{"role": "user", "content": user_content}],
            system=system, feature=_FEATURE,
        )
        try:
            outline = _parse_json_response(raw)
            if not isinstance(outline, list) or not outline:
                raise ValueError("Response is not a non-empty JSON array")
            outline = [str(h) for h in outline]
        except Exception as exc:
            logger.warning(
                "kb_curator_outline_parse_failure",
                extra={"exception_type": type(exc).__name__, "exception": str(exc)},
            )
            outline = ["Overview"]
        return {"outline": outline}

    # ------------------------------------------------------------------
    # Node: citation-grounded section writing
    # ------------------------------------------------------------------

    async def _write_sections(self, state: CurationState) -> dict:
        system = (
            "You write one section of a knowledge-base article, grounded "
            "strictly in the SOURCE DOCUMENT provided in the user turn. If "
            "reviewer guidance is provided, address it directly. Respond "
            "with ONLY a JSON object with two keys: \"content\" (the section "
            "body, plain text, 2-5 sentences) and \"citation\" (a short verbatim "
            "quote, under 25 words, copied directly from the source document "
            "that supports this section — this is what a human reviewer checks "
            "the section against)."
        )
        reviewer_notes = state.get("reviewer_notes")
        sections: list[dict] = []
        citations: list[str] = []
        for heading in state["outline"]:
            user_content = (
                f"Section heading: {heading}\n\n"
                f"SOURCE DOCUMENT (title: {state['source_title']}):\n{state['source_body']}"
            )
            if reviewer_notes:
                user_content += f"\n\nReviewer guidance to address in this revision: {reviewer_notes}"
            raw = await self._ai.generate(
                tenant_id=state["tenant_id"], redis=self._redis,
                messages=[{"role": "user", "content": user_content}],
                system=system, feature=_FEATURE,
            )
            try:
                parsed = _parse_json_response(raw)
                content = str(parsed["content"]).strip()
                citation = str(parsed.get("citation", "")).strip()
            except Exception as exc:
                logger.warning(
                    "kb_curator_section_parse_failure",
                    extra={"heading": heading, "exception_type": type(exc).__name__, "exception": str(exc)},
                )
                content = ""
                citation = ""
            sections.append({"heading": heading, "content": content, "citation": citation})
            if citation:
                citations.append(citation)

        return {"draft_sections": sections, "citations": citations}

    # ------------------------------------------------------------------
    # Node: polish / dedup / final assembly
    # ------------------------------------------------------------------

    async def _polish(self, state: CurationState) -> dict:
        system = (
            "You polish a knowledge-base article draft: fix terminology "
            "consistency, remove redundancy between sections, and write a "
            "concise title. Respond with ONLY a JSON object with two keys: "
            "\"title\" (string, max 200 chars) and \"body\" (the full polished "
            "article, markdown with '## ' section headings matching the "
            "sections given)."
        )
        sections_text = "\n\n".join(
            f"## {s['heading']}\n{s['content']}" for s in state["draft_sections"] if s["content"]
        )
        user_content = f"Working title: {state['source_title']}\n\nDraft sections:\n\n{sections_text}"
        raw = await self._ai.generate(
            tenant_id=state["tenant_id"], redis=self._redis,
            messages=[{"role": "user", "content": user_content}],
            system=system, feature=_FEATURE,
        )
        try:
            parsed = _parse_json_response(raw)
            final_title = str(parsed["title"]).strip()[:200]
            final_body = str(parsed["body"]).strip()
            if not final_title or not final_body:
                raise ValueError("Missing or empty title/body")
        except Exception as exc:
            logger.warning(
                "kb_curator_polish_parse_failure",
                extra={"exception_type": type(exc).__name__, "exception": str(exc)},
            )
            final_title = state["source_title"][:200]
            final_body = sections_text or state["source_body"]

        return {"final_title": final_title, "final_body": final_body}

    # ------------------------------------------------------------------
    # Node: lint check (lightweight — §4.5's contradiction/staleness check only;
    # the orphan/category check is done outside the graph, in the Celery task,
    # since category_id isn't part of synthesis state)
    # ------------------------------------------------------------------

    async def _lint_check(self, state: CurationState) -> dict:
        system = (
            "You review a knowledge-base draft for problems before it reaches a "
            "human reviewer. Compare the DRAFT against the RELATED CONTEXT (other "
            "existing wiki pages on similar topics) and flag any contradictions "
            "or claims that look stale or unsupported. Respond with ONLY a JSON "
            "array of short finding strings — an empty array if you find nothing."
        )
        user_content = (
            f"DRAFT:\n{state['final_body']}\n\n"
            f"RELATED CONTEXT:\n{state['related_context'] or '(none)'}"
        )
        raw = await self._ai.generate(
            tenant_id=state["tenant_id"], redis=self._redis,
            messages=[{"role": "user", "content": user_content}],
            system=system, feature=_FEATURE,
        )
        try:
            findings = _parse_json_response(raw)
            findings = [str(f) for f in findings] if isinstance(findings, list) else []
        except Exception as exc:
            logger.warning(
                "kb_curator_lint_parse_failure",
                extra={"exception_type": type(exc).__name__, "exception": str(exc)},
            )
            findings = []
        return {"lint_findings": findings}

    # ------------------------------------------------------------------
    # Node: human review — pauses via interrupt(), resumed via Command(resume=...)
    # ------------------------------------------------------------------

    async def _human_review(self, state: CurationState) -> dict:
        decision_payload = interrupt({
            "title": state["final_title"],
            "body": state["final_body"],
            "citations": state["citations"],
            "lint_findings": state["lint_findings"],
        })
        return {
            "review_decision": decision_payload.get("decision"),
            "reviewer_notes": decision_payload.get("notes"),
        }
