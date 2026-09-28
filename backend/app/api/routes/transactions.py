import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.integrations import transaction_client
from app.schemas.chat import TransactionOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


@router.get("", response_model=list[TransactionOut])
def list_recent_transactions(
    account_id: int = Query(...),
    kind: str = Query("recharge", pattern="^(recharge|withdrawal)$"),
    db: Session = Depends(get_db),
) -> list[TransactionOut]:
    logger.info("Transactions: fetching %s for account_id=%d", kind, account_id)
    txs = transaction_client.get_recent_transactions(db, account_id, kind)
    logger.info("Transactions: found %d %s records", len(txs), kind)
    return [TransactionOut.model_validate(t, from_attributes=True) for t in txs]
