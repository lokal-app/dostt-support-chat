"""The Gemini tool-calling loop for one chat turn.

Hand-written (rather than an auto-continuing SDK helper) so the account_id
enforcement in agent/executor.py and the trace-building below both sit
explicitly between "the model asked for a tool" and "the tool actually ran"
— a boundary worth keeping visible and reviewable rather than hidden inside
a generic runner's hook API.

This module imports app.agent.tool_schemas (pure data) plus app.agent.executor
for dispatch — never app.services or app.integrations directly. It does talk
directly to the google.genai `types` module, which is the AI provider's wire
format, not a business-logic dependency.
"""

import logging
from dataclasses import dataclass, field

from google.genai import types

from app.agent import executor, tool_schemas
from app.agent.client import AgentClient
from app.agent.context import SessionContext
from app.agent.prompt import render_system_prompt
from app.agent.trace import AgentTrace, AgentTraceStep

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8
_APOLOGY_REPLY = "Sorry, I couldn't process that — could you try again?"
_MAX_GENERATE_ATTEMPTS = 5


def _is_usable(response: types.GenerateContentResponse) -> bool:
    if not response.candidates or response.candidates[0].content is None:
        return False
    return response.candidates[0].finish_reason in (None, types.FinishReason.STOP)


def _generate_with_retry(
    client: AgentClient, *, system: str, contents: list[types.Content], tools: list[types.Tool]
) -> types.GenerateContentResponse:
    response = client.generate(system=system, contents=contents, tools=tools)
    for attempt in range(1, _MAX_GENERATE_ATTEMPTS):
        if _is_usable(response):
            return response
        logger.warning(
            "Gemini response unusable (finish_reason=%s) on attempt %d/%d — retrying",
            response.candidates[0].finish_reason if response.candidates else None,
            attempt,
            _MAX_GENERATE_ATTEMPTS,
        )
        response = client.generate(system=system, contents=contents, tools=tools)
    return response


@dataclass(frozen=True)
class ChatTurnResult:
    reply: str
    trace: list[AgentTraceStep]
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryTurn:
    """One earlier turn of this conversation, supplied by the caller. The
    backend is stateless across /api/chat requests, so without this the
    model has no memory of anything said before the current message."""

    role: str  # "account" | "assistant"
    text: str


def _build_tools() -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters_json_schema=tool["input_schema"],
                )
                for tool in tool_schemas.ALL_TOOLS
            ]
        )
    ]


def run_chat_turn(
    client: AgentClient,
    ctx: SessionContext,
    user_message: str,
    *,
    history: list[HistoryTurn] | None = None,
) -> ChatTurnResult:
    system = render_system_prompt(name=ctx.name, role=ctx.role, language=ctx.language)
    tools = _build_tools()
    contents: list[types.Content] = [
        types.Content(
            role="user" if turn.role == "account" else "model",
            parts=[types.Part(text=turn.text)],
        )
        for turn in (history or [])
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))
    trace = AgentTrace()
    metadata: dict = {}

    for iteration in range(MAX_ITERATIONS):
        logger.info("Agent: iteration %d/%d — calling Gemini", iteration + 1, MAX_ITERATIONS)
        response = _generate_with_retry(client, system=system, contents=contents, tools=tools)

        if not _is_usable(response):
            logger.warning(
                "Gemini response still unusable after %d attempts (finish_reason=%s) — "
                "returning apology instead of a possibly-empty reply",
                _MAX_GENERATE_ATTEMPTS,
                response.candidates[0].finish_reason if response.candidates else None,
            )
            return ChatTurnResult(reply=_APOLOGY_REPLY, trace=trace.to_list(), metadata=metadata)

        model_content = response.candidates[0].content
        contents.append(model_content)
        parts = model_content.parts or []

        function_call_parts = [p for p in parts if p.function_call is not None]
        if not function_call_parts:
            final_text = "".join(p.text for p in parts if p.text is not None)
            logger.info("Agent: model returned final text (len=%d) after %d iteration(s)", len(final_text), iteration + 1)
            return ChatTurnResult(reply=final_text, trace=trace.to_list(), metadata=metadata)

        response_parts = []
        for part in function_call_parts:
            call = part.function_call
            args = dict(call.args or {})
            logger.info("Agent: tool call → %s(%s)", call.name, ", ".join(f"{k}={v!r}" for k, v in args.items()))
            result = executor.execute(call.name, args, ctx)
            logger.info("Agent: tool result ← %s ok=%s summary=%r", call.name, not result.is_error, result.summary_for_trace)
            if result.metadata:
                logger.info("Agent: tool metadata: %s", result.metadata)
            trace.add(tool=call.name, ok=not result.is_error, summary=result.summary_for_trace)
            metadata.update(result.metadata)
            response_parts.append(
                types.Part.from_function_response(
                    name=call.name, response={"result": result.content_for_model}
                )
            )
        contents.append(types.Content(role="user", parts=response_parts))

    trace.truncated = True
    return ChatTurnResult(
        reply="I've had to pause here — could you try rephrasing your question?",
        trace=trace.to_list(),
        metadata=metadata,
    )
