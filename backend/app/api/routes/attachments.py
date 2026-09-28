"""Ticket-attachment upload — a separate side-channel from the chat/tool-call
flow, since Gemini's function-calling can't carry binary image bytes as a
tool argument. The frontend uploads here as soon as the account picks a
file; app/agent/tool_registry.py picks it up automatically (via
attachment_service.consume_pending_attachment) whenever create_support_ticket
next fires in this session — the model never needs to reference it at all.
"""

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.db.session import get_db
from app.services import attachment_service, chat_session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session", tags=["attachments"])


@router.post("/{session_id}/attachment")
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    if chat_session_service.get_session(db, session_id) is None:
        raise NotFoundError("Unknown session_id — call /api/session/init first")

    data = await file.read()
    logger.info("Attachment: uploading %s (%d bytes, type=%s) for session=%s", file.filename, len(data), file.content_type, session_id)
    try:
        attachment_service.save_pending_attachment(db, session_id, file, data)
    except attachment_service.AttachmentError as exc:
        logger.warning("Attachment: rejected — %s", exc)
        raise AppError(str(exc), status_code=400) from exc

    logger.info("Attachment: saved for session=%s", session_id)
    return {"status": "attached", "filename": file.filename}


@router.delete("/{session_id}/attachment")
def remove_attachment(session_id: str, db: Session = Depends(get_db)) -> dict:
    if chat_session_service.get_session(db, session_id) is None:
        raise NotFoundError("Unknown session_id — call /api/session/init first")
    removed = attachment_service.clear_pending_attachment(db, session_id)
    logger.info("Attachment: remove for session=%s → %s", session_id, "removed" if removed else "nothing_pending")
    return {"status": "removed" if removed else "nothing_pending"}
