import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.api.routes import attachments, calls, chat, faqs, session, tickets, transactions
from app.api.routes.admin import login as admin_login
from app.api.routes.admin import tickets as admin_tickets
from app.core.config import assert_admin_secrets_are_safe, settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.DEBUG if settings.ENVIRONMENT == "dev" else logging.INFO,
    format="\033[2m%(asctime)s\033[0m %(levelname)s \033[36m%(name)s\033[0m: %(message)s",
    datefmt="%H:%M:%S",
)

assert_admin_secrets_are_safe(settings)

# The chat webview and the admin dashboard are both plain static
# HTML/CSS/JS (no build step, no separate Node app — see README) — served
# directly by FastAPI rather than a second frontend service, per "Python
# only". frontend/ holds the account-facing chat UI; admin/ holds the CS
# ticket-queue dashboard.
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
ADMIN_DIR = Path(__file__).resolve().parents[2] / "admin"
# Design tokens (colors/type/radii) shared by both frontends — see its own
# header comment for why this lives outside either app's directory.
SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"

app = FastAPI(title="Dostt Support Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def dev_request_logger(request: Request, call_next):
    path = request.url.path

    # Skip static asset noise (CSS/JS/images/fonts)
    if path.startswith(("/shared/", "/css/", "/js/", "/admin/admin.")) or path.endswith((".ico", ".png", ".map")):
        return await call_next(request)

    method = request.method
    qs = str(request.url.query) if request.url.query else ""

    # Read request body for POST/PUT/PATCH (cache it so downstream can still read it)
    req_body_str = ""
    if method in ("POST", "PUT", "PATCH"):
        body_bytes = await request.body()
        if body_bytes:
            content_type = request.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    parsed = json.loads(body_bytes)
                    # Truncate large fields (e.g. history array in /api/chat)
                    req_body_str = json.dumps(parsed, default=str)
                    if len(req_body_str) > 1500:
                        req_body_str = req_body_str[:1500] + "...(truncated)"
                except Exception:
                    req_body_str = body_bytes[:500].decode(errors="replace")
            elif "multipart" in content_type:
                req_body_str = f"[multipart form data, {len(body_bytes)} bytes]"
            else:
                req_body_str = body_bytes[:500].decode(errors="replace")

    # Webview entry (special tag for easy grepping)
    if path == "/" and method == "GET" and qs:
        logger.info("\033[1;35mWEBVIEW_ENTRY\033[0m %s", qs)

    header = f"\033[1;33m{'─' * 60}\033[0m"
    logger.info(header)
    logger.info("\033[1;32m▶ %s %s\033[0m%s", method, path, f"?{qs}" if qs else "")
    if req_body_str:
        logger.info("\033[33m  ↳ body:\033[0m %s", req_body_str)

    t0 = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Read response body for API routes (not static HTML pages)
    res_body_str = ""
    if path.startswith("/api/"):
        # Collect the streamed response body
        body_chunks = []
        async for chunk in response.body_iterator:
            body_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        res_bytes = b"".join(body_chunks)
        # Rebuild response so it can still be sent to the client
        response = Response(
            content=res_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        if res_bytes:
            try:
                res_body_str = res_bytes.decode()
                if len(res_body_str) > 2000:
                    res_body_str = res_body_str[:2000] + "...(truncated)"
            except Exception:
                res_body_str = f"[{len(res_bytes)} bytes]"

    status = response.status_code
    color = "\033[1;32m" if status < 400 else "\033[1;31m"
    logger.info("%s◀ %d\033[0m in \033[36m%.0fms\033[0m", color, status, elapsed_ms)
    if res_body_str:
        logger.info("\033[33m  ↳ response:\033[0m %s", res_body_str)

    return response


@app.exception_handler(AppError)
def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Without this, an unhandled exception propagates past CORSMiddleware
    # entirely (Starlette's ServerErrorMiddleware sits outside it), so the
    # browser reports a confusing "CORS blocked" error instead of the real
    # failure. Catching it here keeps the response inside the CORS-wrapped
    # layer and gives the frontend a real (if generic) error body to show.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})


app.include_router(session.router)
app.include_router(attachments.router)
app.include_router(calls.router)
app.include_router(transactions.router)
app.include_router(faqs.router)
app.include_router(chat.router)
app.include_router(tickets.router)
app.include_router(admin_login.router)
app.include_router(admin_tickets.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if SHARED_DIR.exists():
    app.mount("/shared", StaticFiles(directory=str(SHARED_DIR)), name="shared")

if ADMIN_DIR.exists():
    app.mount("/admin", StaticFiles(directory=str(ADMIN_DIR), html=True), name="admin")

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
