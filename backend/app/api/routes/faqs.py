"""Read-only FAQ listing for the guided Calls/Transactions card flows — the
single source of truth for which FAQs appear as cards, so the frontend never
duplicates this content and can't drift from it (see app/data/faqs.py's
docstring and the chatbot PRD's warning about "Charged Incorrectly for Call"
content staying in sync with the live refund-automation rules).
"""

import logging

from fastapi import APIRouter, Query

from app.data.faqs import FAQS
from app.schemas.chat import FaqOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/faqs", tags=["faqs"])


@router.get("", response_model=list[FaqOut])
def list_faqs(
    landing_category: str = Query(...),
    audience: str = Query(...),
) -> list[FaqOut]:
    matches = [
        f for f in FAQS
        if f.landing_category == landing_category and f.audience in ("both", audience)
    ]
    logger.info("FAQs: category=%s, audience=%s → %d matches", landing_category, audience, len(matches))
    return [FaqOut(id=f.id, category=f.category, sub_issue=f.sub_issue, landing_category=f.landing_category) for f in matches]
