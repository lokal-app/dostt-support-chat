import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.agent import orchestrator
from app.agent.client import get_agent_client
from app.agent.context import SessionContext
from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.rate_limit import check_rate_limit, client_ip
from app.data.fallbacks import check_fallbacks
from app.db.session import get_db
from app.models.account import Account
from app.schemas.chat import ChatRequest, ChatResponse, TraceStepOut
from app.services import chat_session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def send_message(payload: ChatRequest, request: Request, db: Session = Depends(get_db)) -> ChatResponse:
    # Checked before touching the DB or Gemini at all — see
    # app/core/rate_limit.py for why this is per-session AND per-IP.
    check_rate_limit("chat_session", payload.session_id, limit=settings.RATE_LIMIT_CHAT_PER_SESSION_PER_MIN)
    check_rate_limit("chat_ip", client_ip(request), limit=settings.RATE_LIMIT_CHAT_PER_IP_PER_MIN)

    logger.info("Chat: session=%s message=%r (history_len=%d)", payload.session_id, payload.message[:120], len(payload.history))

    session = chat_session_service.get_session(db, payload.session_id)
    if session is None:
        raise NotFoundError("Unknown session_id — call /api/session/init first")
    account = db.get(Account, session.account_id)
    if account is None:
        raise NotFoundError("Account not found for this session")

    logger.info("Chat: account_id=%d, role=%s, lang=%s, ticket_count=%d", account.id, account.role.value, session.language, session.ticket_count)
    chat_session_service.append_message(db, session.id, "user", payload.message)

    fallback = check_fallbacks(payload.message)
    if fallback is not None:
        logger.info("Chat: deterministic fallback matched → ends_session=%s", fallback.ends_session)
        chat_session_service.append_message(db, session.id, "assistant", fallback.response)
        return ChatResponse(reply=fallback.response, trace=[], metadata={"session_ended": fallback.ends_session})

    ctx = SessionContext(
        account_id=account.id,
        name=account.name,
        role=account.role.value,
        language=session.language,
        db=db,
        session_id=session.id,
        ticket_count_this_session=session.ticket_count,
        auth_token=payload.auth_token,
        language_id=payload.language_id,
        user_type_code=payload.user_type_code,
        country_code=payload.country_code,
        app_version=payload.app_version,
    )
    history = [orchestrator.HistoryTurn(role=t.role, text=t.text) for t in payload.history]
    logger.info("Chat: calling Gemini agent (history_turns=%d, has_auth_token=%s)", len(history), bool(payload.auth_token))
    client = get_agent_client()
    result = orchestrator.run_chat_turn(client, ctx, payload.message, history=history)

    logger.info("Chat: agent replied (trace_steps=%d, metadata=%s)", len(result.trace), result.metadata)
    logger.info("Chat: reply=%r", result.reply[:200] if len(result.reply) > 200 else result.reply)
    chat_session_service.append_message(db, session.id, "assistant", result.reply)
    return ChatResponse(
        reply=result.reply,
        trace=[TraceStepOut(tool=s.tool, ok=s.ok, summary=s.summary) for s in result.trace],
        metadata=result.metadata,
    )
