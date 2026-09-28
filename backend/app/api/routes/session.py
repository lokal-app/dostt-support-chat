"""Session init — the chatbot PRD's "Session Init: chatbot initialises with
the user's token, type, language and platform automatically." There is no
token to verify here, by design: the main Dostt app is assumed to hand off a
plain, already-authenticated user_id in the webview URL (the same handoff
shape the AstroHelp sibling project uses for astrologers) — this endpoint
only resolves it against `accounts.user_id`, it never authenticates anyone
itself.

The real banner passes this user_id base64-encoded (confirmed against the
Milestone-Dostt sibling app's own decode: `atob()` then `parseInt()`,
2026-09-08 — not real encryption, just an obfuscated wrapper around the same
plain numeric dostt_user_id). _decode_banner_user_id mirrors that decode so
this endpoint accepts either form: the real base64-wrapped id, or a plain
string id (our own seed/demo accounts, tests) unchanged.

A resolved id with no matching Account row is auto-created on the spot —
every account in the system before 2026-09-09 was seed/demo data, so without
this, no real first-time user could ever open the chat at all. Only done
for a resolved id that's purely numeric digits (a real dostt_user_id shape);
anything else (e.g. a garbage/test string) still 404s rather than silently
creating junk accounts — see test_init_session_unknown_user_id_is_404.

`name` is a fixed generic greeting, not a placeholder awaiting a real lookup
— confirmed 2026-09-09 (a whole users_user admin listing, not just one
account) that first_name/last_name are blank for essentially the entire user
base, so there is no real name to fetch here at all, ever. `role` and
`onboarded_language_id` ARE a real lookup (REDASH_QUERY_ACCOUNT_ROLE,
extended 2026-09-10 to also select customers_customer.onboarded_language_id,
confirmed against real data both times): role from whether a matching row
exists in experts_expert vs. customers_customer; language_id only has a real
source on the customers_customer side, so it's null for Listener-only
accounts for now. Role falls back to a user_type_code-derived guess, then to
AccountRole.USER; language_id has no fallback (null just means
ticket_service's real-API mirror can't run for this account yet — see its
_mirror_to_real_api). Either way MOCK_MODE or a lookup failure never blocks
account creation itself.
"""

import base64
import binascii
import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.rate_limit import check_rate_limit, client_ip
from app.db.session import get_db
from app.integrations import redash_client
from app.integrations.config import MOCK_MODE
from app.models.account import Account
from app.models.enums import USER_TYPE_CODE_BY_ROLE, AccountRole
from app.schemas.chat import SessionInitResponse
from app.services import chat_session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session", tags=["session"])

_ROLE_BY_USER_TYPE_CODE = {code: role for role, code in USER_TYPE_CODE_BY_ROLE.items()}
_ROLE_BY_NAME = {"user": AccountRole.USER, "listener": AccountRole.LISTENER}


def _decode_banner_user_id(raw: str) -> str:
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return raw
    return decoded if decoded.isdigit() else raw


def _resolve_role_and_language(user_id: str, user_type_code: int | None) -> tuple[AccountRole, int | None]:
    fallback_role = _ROLE_BY_USER_TYPE_CODE.get(user_type_code, AccountRole.USER)
    if MOCK_MODE:
        return fallback_role, None
    try:
        rows = redash_client.run_query(settings.REDASH_QUERY_ACCOUNT_ROLE, {"user_id": user_id}, max_age=3600)
    except Exception:
        logger.exception("Redash account-role lookup failed for user_id %s — using fallback", user_id)
        return fallback_role, None
    if not rows:
        return fallback_role, None
    row = rows[0]
    return _ROLE_BY_NAME.get(row.get("role"), fallback_role), row.get("language_id")


def _create_account_for_real_user(
    db: Session, user_id: str, language: str, user_type_code: int | None
) -> Account:
    role, onboarded_language_id = _resolve_role_and_language(user_id, user_type_code)
    account = Account(
        user_id=user_id, name="Dostt User", role=role, preferred_language=language,
        onboarded_language_id=onboarded_language_id,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("/init", response_model=SessionInitResponse)
def init_session(
    user_id: str = Query(..., description="The account's external user_id, handed off by the host app (base64-encoded by the real banner, plain for demo/test accounts)"),
    language: str = Query("en"),
    user_type_code: int | None = Query(
        None, description="The account's real user_type claim, if the host app sends one — 0=user, 1=listener (best guess, see models/enums.USER_TYPE_CODE_BY_ROLE)"
    ),
    request: Request = None,
    db: Session = Depends(get_db),
) -> SessionInitResponse:
    check_rate_limit("session_init_ip", client_ip(request), limit=settings.RATE_LIMIT_SESSION_INIT_PER_IP_PER_MIN)
    resolved_user_id = _decode_banner_user_id(user_id)
    logger.info("Session init: raw_user_id=%s → resolved=%s, language=%s", user_id, resolved_user_id, language)

    account = db.execute(select(Account).where(Account.user_id == resolved_user_id)).scalar_one_or_none()
    if account is None:
        if not resolved_user_id.isdigit():
            logger.warning("Session init: no account for non-numeric user_id '%s' → 404", resolved_user_id)
            raise NotFoundError(f"No account found for user_id '{resolved_user_id}'")
        try:
            logger.info("Session init: first-time user %s — creating account", resolved_user_id)
            account = _create_account_for_real_user(db, resolved_user_id, language, user_type_code)
            logger.info("Session init: created account_id=%d for user %s", account.id, resolved_user_id)
        except IntegrityError:
            db.rollback()
            account = db.execute(select(Account).where(Account.user_id == resolved_user_id)).scalar_one_or_none()
            if account is None:
                raise
            logger.info("Session init: race-condition fallback — found existing account_id=%d", account.id)
    else:
        logger.info("Session init: found existing account_id=%d, name=%s, role=%s", account.id, account.name, account.role.value)

    session = chat_session_service.start_session(db, account.id, language)
    logger.info("Session init: started session=%s for account_id=%d", session.id, account.id)
    return SessionInitResponse(
        session_id=session.id,
        account_id=account.id,
        name=account.name,
        role=account.role.value,
        language=language,
    )
