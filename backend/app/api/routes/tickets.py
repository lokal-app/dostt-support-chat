import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.db.session import get_db
from app.models.account import Account
from app.schemas.ticket import TicketIntakeRequest, TicketRead
from app.services import ticket_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketRead])
def list_my_tickets(account_id: int = Query(...), db: Session = Depends(get_db)) -> list[TicketRead]:
    logger.info("Tickets: listing tickets for account_id=%d", account_id)
    tickets = ticket_service.list_tickets_for_account(db, account_id)
    logger.info("Tickets: found %d tickets", len(tickets))
    return [TicketRead.model_validate(t) for t in tickets]


@router.post("", response_model=TicketRead)
def create_ticket(payload: TicketIntakeRequest, db: Session = Depends(get_db)) -> TicketRead:
    """A ticket entering the system directly — NOT through the chat agent.
    See ticket_service.create_ticket_with_automation's docstring: for the
    three refund-driven categories, this runs the v1.1 refund-automation
    rules synchronously and may resolve the ticket immediately, purely off
    the data in this request and the account/call rows already in the
    database — no Gemini call involved.
    """
    account = db.execute(select(Account).where(Account.user_id == payload.user_id)).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"No account found for user_id '{payload.user_id}'")

    existing = ticket_service.get_active_ticket_for_category(db, account.id, payload.category)
    if existing is not None:
        raise AppError(
            f"Account already has an active ticket (#{existing.id}, status '{existing.status.value}') "
            "for this category — not raising a duplicate.",
            status_code=409,
        )

    ticket = ticket_service.create_ticket_with_automation(
        db,
        account_id=account.id,
        category=payload.category,
        sub_category=payload.sub_category,
        description=payload.description,
        description_en=payload.description_en or payload.description,
        call_id=payload.call_id,
        callback_requested=payload.callback_requested,
        auth_token=payload.auth_token,
        language_id=payload.language_id,
        user_type_code=payload.user_type_code,
        country_code=payload.country_code,
    )
    return TicketRead.model_validate(ticket)
