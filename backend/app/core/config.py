from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    DATABASE_URL: str = "postgresql+psycopg2://dostt:dostt@localhost:5435/dostt_support"
    TEST_DATABASE_URL: str = "postgresql+psycopg2://dostt:dostt@localhost:5435/dostt_support_test"

    # --- Admin dashboard auth ---
    # There is no self-service signup or per-admin password reset — every admin
    # is provisioned by scripts/create_admin.py and gets the fixed password for
    # the access_level they were granted (same convention as the AstroHelp
    # sibling project, adapted here since Dostt CS only needs one tier).
    ADMIN_SESSION_SECRET: str = "dev-only-change-me"
    ADMIN_SESSION_EXPIRE_HOURS: int = 8
    ADMIN_PASSWORD: str = "dosttSupport@123"
    # This email always gets admin access with ADMIN_PASSWORD even if no Admin
    # row exists yet — solves the bootstrap chicken-and-egg problem on a brand
    # new database (see auth_service._maybe_bootstrap_owner).
    OWNER_EMAIL: str = "parth.a@getlokalapp.com"

    ENVIRONMENT: str = "dev"

    # --- Gemini, via Vertex AI ---
    # A GCP service account (not a bare API key) — org policy, per the
    # AstroHelp sibling project's Vertex AI migration guide, applies here too:
    # usage on a bare API key lands in one untagged billing bucket nobody can
    # attribute to a team/use case. No project id setting — vertex_client reads
    # it straight off the credential's own project_id field.
    GOOGLE_CLOUD_LOCATION: str = "global"
    GEMINI_VERTEX_CREDENTIALS_JSON: str = ""
    GEMINI_MODEL: str = "gemini-flash-latest"
    # The "{environment}_{feature}" billing label's feature half — coordinate
    # with other teams before changing this; it's the bucket cost gets
    # attributed to.
    GEMINI_BILLING_FEATURE: str = "dostt_support"

    # --- Mocked integrations ---
    # Gates every file in app/integrations/ — see each file's own docstring
    # for exactly what's fabricated and how to make it real.
    MOCK_MODE: bool = True

    SLACK_WEBHOOK_URL: str = "https://hooks.slack.com/services/EXAMPLE/WEBHOOK/URL"
    SLACK_SUPPORT_CHANNEL: str = "#dostt-support-alerts"
    SLACK_MOCK_MODE: bool = True

    # --- Redash (real production data, via BigQuery-backed saved queries) ---
    # Empty REDASH_BASE_URL means "not configured yet" — integrations/redash_client.py
    # raises rather than guessing at a host. Query ids are for the specific saved
    # queries built and validated against real data on 2026-09-08; see each
    # integrations/*_client.py for which query id maps to which real-data need.
    REDASH_BASE_URL: str = ""
    REDASH_API_KEY: str = ""
    REDASH_QUERY_RECENT_CALLS: int = 20279
    REDASH_QUERY_RECENT_RECHARGES: int = 20290
    # Confirmed 2026-09-09 that public_payouts_expertpayout is the live payout
    # table (real recent row vs. public_payments_dosttexpertpayout's stale
    # 2025-09-25 one) — this query id targets that confirmed table.
    REDASH_QUERY_RECENT_WITHDRAWALS: int = 20311
    REDASH_QUERY_SUPPORT_ISSUES: int = 20282
    # role only (via experts_expert/customers_customer) for a newly-seen real
    # user_id — NOT name: confirmed 2026-09-09 that users_user.first_name/
    # last_name are blank across essentially the whole user base (Dostt
    # doesn't collect real names), so there's deliberately no name field in
    # this query — see api/routes/session.py.
    REDASH_QUERY_ACCOUNT_ROLE: int = 20310

    # --- Real Dostt backend API (ticket creation) ---
    # Confirmed working end-to-end via a live curl test on 2026-09-08
    # (ticket_id=2119, staging). Base URL is staging-only for now — nobody
    # has confirmed the production equivalent yet. auth_token is per-session
    # (the user's own JWT, handed off by the app alongside user_id — see
    # 2026-09-08 conversation), never a shared secret, so it's a parameter to
    # dostt_api_client.create_ticket(), not a setting here.
    DOSTT_API_BASE_URL: str = "https://api.dostt.in"
    DOSTT_APP_VERSION: str = "1.1.36"
    DOSTT_APP_VERSION_CODE: str = "136"

    # --- Ticket-attachment uploads ---
    # Local disk for now (matches this project's dev-friendly scope) — a real
    # deployment would want this on durable/shared storage (S3 etc.) instead,
    # especially once running more than one backend instance.
    UPLOAD_DIR: str = "uploads"
    UPLOAD_MAX_BYTES: int = 8 * 1024 * 1024  # 8 MB
    UPLOAD_ALLOWED_CONTENT_TYPES: str = "image/jpeg,image/png,image/webp,image/heic"

    @property
    def upload_allowed_content_types_list(self) -> list[str]:
        return [t.strip() for t in self.UPLOAD_ALLOWED_CONTENT_TYPES.split(",") if t.strip()]

    # --- Rate limiting ---
    # /api/chat calls a paid Gemini API on every request with no cap
    # otherwise — see app/core/rate_limit.py. Per-session catches a single
    # runaway session (bad retry loop, deliberate spam); per-IP catches
    # someone spinning up many sessions from one place to route around the
    # per-session cap. /api/session/init has its own (higher, cheaper)
    # per-IP-only cap since it has no session yet to key on and does less
    # per call (no Gemini), but still hits Redash/DB.
    RATE_LIMIT_CHAT_PER_SESSION_PER_MIN: int = 20
    RATE_LIMIT_CHAT_PER_IP_PER_MIN: int = 60
    RATE_LIMIT_SESSION_INIT_PER_IP_PER_MIN: int = 30

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Values that are safe as a local-dev convenience but must never reach a
# real deployment — checked-into-the-repo defaults are equivalent to "no
# auth" once this is reachable from anywhere but a developer's own machine.
INSECURE_ADMIN_DEFAULTS = {
    "ADMIN_SESSION_SECRET": "dev-only-change-me",
    "ADMIN_PASSWORD": "dosttSupport@123",
}


def assert_admin_secrets_are_safe(s: Settings) -> None:
    """Raises RuntimeError if `s.ENVIRONMENT != "dev"` and the admin
    secret/password are still at their dev defaults — see app/main.py,
    which calls this at import time so it's impossible to boot the app
    outside dev with either one unrotated, rather than relying on someone
    remembering to change both before every deploy."""
    if s.ENVIRONMENT == "dev":
        return
    still_default = [k for k, v in INSECURE_ADMIN_DEFAULTS.items() if getattr(s, k) == v]
    if still_default:
        raise RuntimeError(
            f"Refusing to start with ENVIRONMENT={s.ENVIRONMENT!r}: "
            f"{', '.join(still_default)} still at its dev default. Set a real value in this "
            f"environment's config before deploying."
        )
