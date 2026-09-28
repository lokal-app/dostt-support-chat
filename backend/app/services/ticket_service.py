"""Owns every write to Ticket.status and TicketStatusHistory — the only
module allowed to mutate either, so `ticket.status` can never drift from the
latest history row (enforced here in code, not a DB trigger, so the two
writes stay inside one Python transaction and one code path to review).

`create_ticket` does create -> auto-assign -> Slack-notify in a single
transaction, the same "services do cross-integration business logic, routes
and the agent never call integrations directly" shape as the AstroHelp
sibling project.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.jwt_claims import country_from_token, decode_unverified_claims, user_type_from_token
from app.core.time import utcnow
from app.data.issue_catalog import issue_id_for
from app.integrations import admin_mapping_client, call_client, dostt_api_client, slack_client
from app.models.account import Account
from app.models.enums import TicketStatus, USER_TYPE_CODE_BY_ROLE
from app.models.ticket import Ticket
from app.models.ticket_status_history import TicketStatusHistory
from app.services import attachment_service, refund_service

logger = logging.getLogger(__name__)

# Which team an escalated ticket routes to, derived from category rather than
# a separate DB column — drawn from the SOP's own escalation-target column
# (Trust and Safety - Vibin / Tech Support / L2 Onboarding Team / L2 Refund).
ESCALATION_TEAM_BY_CATEGORY: dict[str, str] = {
    "report_user": "Trust & Safety",
    "personal_info_request": "Trust & Safety",
    "not_showing_face": "Trust & Safety",
    "account_blocked": "Trust & Safety",
    "tech_audio_video": "Tech Support",
    "tech_video_issue": "Tech Support",
    "app_crash_freeze": "Tech Support",
    "gender_change": "L2 Onboarding Team",
    "gender_mismatch": "L2 Onboarding Team",
    "change_username": "L2 Onboarding Team",
    "access_request": "L2 Onboarding Team",
    "kyc_pan_delink": "L2 Onboarding Team",
    "kyc_limit_exceeded": "L2 Onboarding Team",
    "pan_delink_transfer": "L2 Onboarding Team",
    "multiple_languages_request": "L2 Onboarding Team",
    "negative_balance": "L2 Refund",
    "recharge_not_added": "L2 Refund",
    "recharge_twice": "L2 Refund",
    "withdrawal_not_received": "L2 Refund",
    "tds_help": "L2 Refund",
    "charged_incorrectly": "L2 Refund",
    "cannot_hear": "L2 Refund",
}

# Per-session ticket cap — chatbot PRD: "Max 2 tickets per session; limit
# reached -> suppress ticket option."
MAX_TICKETS_PER_SESSION = 2


def _record_status(db: Session, ticket: Ticket, status: TicketStatus, note: str | None, changed_by: str) -> None:
    ticket.status = status
    db.add(TicketStatusHistory(ticket=ticket, status=status, note=note, changed_by=changed_by))


def get_active_ticket_for_category(db: Session, account_id: int, category: str) -> Ticket | None:
    return db.execute(
        select(Ticket)
        .where(
            Ticket.account_id == account_id,
            Ticket.category == category,
            Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
        )
        .order_by(Ticket.created_at.desc())
    ).scalars().first()


def create_ticket(
    db: Session,
    *,
    account_id: int,
    category: str,
    sub_category: str,
    description: str,
    description_en: str,
    preferred_language: str,
    callback_requested: bool = False,
    refund_checked: bool = False,
    refund_rule_label: str | None = None,
) -> Ticket:
    account = db.get(Account, account_id)
    sla_due_at = utcnow() + timedelta(hours=refund_service.sla_hours_for(account)) if account else None

    logger.info("TicketService: creating ticket — account_id=%d, category=%s, sub=%s", account_id, category, sub_category)

    ticket = Ticket(
        account_id=account_id,
        category=category,
        sub_category=sub_category,
        description=description,
        description_en=description_en,
        preferred_language=preferred_language,
        callback_requested=callback_requested,
        refund_checked=refund_checked,
        refund_rule_label=refund_rule_label,
        sla_due_at=sla_due_at,
    )
    db.add(ticket)
    db.flush()  # assigns ticket.id
    logger.info("TicketService: ticket #%d flushed (status → SUBMITTED)", ticket.id)
    _record_status(db, ticket, TicketStatus.SUBMITTED, note=None, changed_by="system")

    team = ESCALATION_TEAM_BY_CATEGORY.get(category)
    ticket.escalation_team = team
    tickets_so_far = db.execute(select(Ticket.id).where(Ticket.category == category)).all()
    admin = admin_mapping_client.assign_admin(db, ticket_count_so_far=len(tickets_so_far))
    if admin is not None:
        ticket.assigned_admin_id = admin.id
        logger.info("TicketService: ticket #%d auto-assigned to admin %s (team=%s)", ticket.id, admin.name, team)
        _record_status(
            db,
            ticket,
            TicketStatus.ASSIGNED,
            note=f"Auto-assigned to {admin.name}" + (f" ({team})" if team else ""),
            changed_by="system",
        )
    else:
        logger.info("TicketService: ticket #%d — no admin available for assignment", ticket.id)

    slack_message = (
        f":ticket: New ticket #{ticket.id} — *{category}* / {sub_category}\n"
        f"{description_en}\n"
        f"Assigned: {admin.name if admin else 'unassigned'}"
        + (f" · Escalation: {team}" if team else "")
    )
    slack_client.notify_ticket_created(db, ticket_id=ticket.id, message=slack_message)

    db.commit()
    db.refresh(ticket)
    logger.info("TicketService: ticket #%d committed — final status=%s, sla_due=%s", ticket.id, ticket.status.value, ticket.sla_due_at)
    return ticket


def create_ticket_with_automation(
    db: Session,
    *,
    account_id: int,
    category: str,
    sub_category: str,
    description: str,
    description_en: str,
    call_id: int | None = None,
    callback_requested: bool = False,
    auth_token: str | None = None,
    language_id: int | None = None,
    user_type_code: int | None = None,
    country_code: str | None = None,
    app_version: str | None = None,
    session_id: str | None = None,
) -> Ticket:
    """The one place the v1.1 refund-automation rules actually run — called
    both by the chat agent's create_support_ticket tool (agent/tool_registry.py)
    and by the direct ticket-intake route (api/routes/tickets.py), for a
    ticket arriving from anywhere OTHER than a live Gemini chat turn (the
    in-app Help & Support form, a CS agent logging a call-in complaint, a
    future webhook). For the three refund-driven categories, if `call_id` is
    given, the rules run right here, synchronously, before the ticket is
    even written — triggered by this data landing in the table. The model
    is never handed the eligible/not-eligible outcome (product decision,
    2026-09-08 — see tool_schemas.py's comment): every refund-driven ticket
    is told to the account as "raised for review," whether or not it just
    silently auto-resolved a moment later.

    auth_token/language_id/user_type_code/country_code are for ALSO mirroring
    this ticket to the real Dostt backend (app/integrations/dostt_api_client.py)
    — see that module and app/data/issue_catalog.py for exactly what's
    confirmed vs. still missing. This local Ticket row is always the ticket
    of record regardless; the real-API call is a best-effort mirror on top,
    never something its failure should block or roll back the local write for.

    session_id (only meaningful from the chat agent path) picks up whatever
    screenshot was uploaded via api/routes/attachments.py earlier in this
    session, if any — see attachment_service.py. Not passed from the direct
    ticket-intake route (api/routes/tickets.py), which has no chat session
    to attach anything from.
    """
    logger.info("TicketAutomation: account_id=%d, category=%s, sub=%s, call_id=%s, session_id=%s",
                account_id, category, sub_category, call_id, session_id)

    refund_checked = False
    refund_rule_label = None
    decision = None

    if category in refund_service.REFUND_DRIVEN_ISSUE_TAGS and call_id is not None:
        logger.info("TicketAutomation: refund-driven category — running v1.1 rules for call_id=%s", call_id)
        account = db.get(Account, account_id)
        call = call_client.get_call(db, account_id, call_id)
        if account is not None and call is not None:
            decision = refund_service.decide(account, call, category)
            refund_checked = True
            refund_rule_label = decision.rule_label
            logger.info("TicketAutomation: refund decision → eligible=%s, rule=%s, reason=%s",
                        decision.eligible, decision.rule_label, decision.reason)
        else:
            logger.info("TicketAutomation: skipped refund check — account=%s, call=%s", account is not None, call is not None)
    else:
        logger.info("TicketAutomation: not refund-driven (category=%s, call_id=%s) — skipping refund rules", category, call_id)

    account_for_language = db.get(Account, account_id)
    ticket = create_ticket(
        db,
        account_id=account_id,
        category=category,
        sub_category=sub_category,
        description=description,
        description_en=description_en,
        preferred_language=account_for_language.preferred_language if account_for_language else "en",
        callback_requested=callback_requested,
        refund_checked=refund_checked,
        refund_rule_label=refund_rule_label,
    )

    if decision is not None and decision.eligible:
        note = f"Auto-refunded {decision.refund_coins} coins ({decision.rule_label}) — {decision.reason}"
        logger.info("TicketAutomation: auto-resolving ticket #%d — %s", ticket.id, note)
        update_status(db, ticket, TicketStatus.RESOLVED, note=note, changed_by="refund automation")

    attachment = None
    if session_id is not None:
        attachment = attachment_service.consume_pending_attachment(db, session_id)
        if attachment is not None:
            logger.info("TicketAutomation: attaching screenshot to ticket #%d — %s", ticket.id, attachment.file_path)
            ticket.attachment_path = attachment.file_path
            db.commit()

    _mirror_to_real_api(
        db,
        ticket,
        account=account_for_language,
        auth_token=auth_token,
        language_id=language_id,
        user_type_code=user_type_code,
        country_code=country_code,
        app_version=app_version,
        attachment=attachment,
    )

    return ticket


def _mirror_to_real_api(
    db: Session,
    ticket: Ticket,
    *,
    account: Account | None,
    auth_token: str | None,
    language_id: int | None,
    user_type_code: int | None,
    country_code: str | None,
    app_version: str | None = None,
    attachment: attachment_service.ConsumedAttachment | None,
) -> None:
    if auth_token is None:
        logger.info("Ticket #%s: real-API mirror SKIPPED — no auth_token provided", ticket.id)
        return  # not wired up yet for this session — local ticket stands alone, not an error

    resolved_language_id = language_id
    if resolved_language_id is None and account is not None:
        # Falls back to the real onboarded_language_id looked up once at
        # account-creation time (session.py, confirmed 2026-09-10) — only
        # populated for User accounts (customers_customer side); still None
        # for Listener-only accounts and local seed/demo accounts.
        resolved_language_id = account.onboarded_language_id
    if resolved_language_id is None:
        # Last resort: the token's own onboarded_language_id claim (covers
        # Listener-only accounts, which the Redash lookup above can't).
        claims = decode_unverified_claims(auth_token)
        candidate = claims.get("user_payload", {}).get("onboarded_language_id") if claims else None
        resolved_language_id = candidate if isinstance(candidate, int) else None
    if resolved_language_id is None:
        logger.info(
            "Ticket #%s: real-API mirror SKIPPED — no language_id from explicit param, "
            "account.onboarded_language_id, or the token's own claim (category=%s)",
            ticket.id, ticket.category,
        )
        return  # no language_id from any source yet — can't call the real API without one

    issue_id = issue_id_for(ticket.category)
    if issue_id is None:
        logger.info(
            "Ticket #%s: real-API mirror SKIPPED — category '%s' has no confirmed issue_id "
            "for the current DOSTT_API_BASE_URL (%s) — see app/data/issue_catalog.py",
            ticket.id, ticket.category, settings.DOSTT_API_BASE_URL,
        )
        return  # category not yet confirmed against the real issue catalog — see issue_catalog.py

    resolved_user_type = user_type_code
    if resolved_user_type is None and account is not None:
        resolved_user_type = USER_TYPE_CODE_BY_ROLE.get(account.role, 0)
    if resolved_user_type is None:
        # No account-derived role either (e.g. account lookup failed) —
        # fall back to the token's own user_type claim rather than
        # defaulting straight to 0/User, same trust level as user_id
        # already gets from the URL (see jwt_claims.py's own docstring).
        resolved_user_type = user_type_from_token(auth_token)

    # country_code had no real data source at all before this — every
    # ticket silently defaulted to "IN" regardless of the account's actual
    # country. The token's own `country` claim is the real value; only
    # falls back to "IN" if decoding fails entirely (malformed/absent token).
    resolved_country = country_code or country_from_token(auth_token) or "IN"

    try:
        real_id = dostt_api_client.create_ticket(
            auth_token=auth_token,
            description=ticket.description_en,
            language_id=resolved_language_id,
            is_call_opted_in=ticket.callback_requested,
            user_type=resolved_user_type if resolved_user_type is not None else 0,
            issue_id=issue_id,
            country_code=resolved_country,
            image_path=attachment.file_path if attachment else None,
            image_content_type=attachment.content_type if attachment else "image/jpeg",
            app_version=app_version,
        )
    except dostt_api_client.DosttApiError as e:
        logger.exception(
            "Ticket #%s: real-API mirror FAILED — status=%s body=%s (issue_id=%s, language_id=%s, "
            "user_type=%s, country=%s) — local ticket stands alone",
            ticket.id, e.status_code, e.body, issue_id, resolved_language_id, resolved_user_type, resolved_country,
        )
        return

    logger.info("Ticket #%s: real-API mirror SUCCEEDED — real_ticket_id=%s", ticket.id, real_id)
    ticket.real_ticket_id = real_id
    db.commit()


def update_status(db: Session, ticket: Ticket, status: TicketStatus, note: str | None, changed_by: str) -> Ticket:
    _record_status(db, ticket, status, note=note, changed_by=changed_by)
    db.commit()
    db.refresh(ticket)
    return ticket


def list_tickets_for_account(db: Session, account_id: int) -> list[Ticket]:
    return list(
        db.execute(
            select(Ticket).where(Ticket.account_id == account_id).order_by(Ticket.created_at.desc())
        ).scalars()
    )


def list_tickets_for_queue(db: Session, status: TicketStatus | None = None) -> list[Ticket]:
    stmt = select(Ticket).order_by(Ticket.created_at.desc())
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    return list(db.execute(stmt).scalars())
