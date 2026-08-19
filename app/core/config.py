"""Application configuration.

Single source of truth for runtime settings. Values are read from the process
environment (and `.env` in development) with the `CWB_` prefix, e.g.
`CWB_POSTGRES_HOST`. Nested settings use a double underscore delimiter, so
`CWB_KEYCLOAK__REALM` maps to `settings.keycloak.realm`.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, PostgresDsn, RedisDsn, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

_ENV_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_nested_delimiter="__",
    extra="ignore",
)


class PostgresSettings(BaseSettings):
    """Postgres connection settings, shared by asyncpg and SQLAlchemy."""

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    user: str = "claims"
    password: str = "claims"
    db: str = "claims_workbench"

    # asyncpg pool sizing for the raw-SQL path
    pool_min_size: int = 5
    pool_max_size: int = 20
    pool_command_timeout: float = 30.0

    # SQLAlchemy engine sizing for the ORM path
    sqlalchemy_pool_size: int = 10
    sqlalchemy_max_overflow: int = 10
    sqlalchemy_echo: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_dsn(self) -> str:
        """DSN for SQLAlchemy's async engine."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=self.db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_dsn(self) -> str:
        """DSN for Alembic's synchronous path (psycopg2)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg2",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=self.db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def asyncpg_dsn(self) -> str:
        """Plain libpq DSN for the asyncpg pool."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class RedisSettings(BaseSettings):
    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_REDIS_")

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    celery_broker_db: int = 1
    celery_result_db: int = 2

    def _dsn(self, db: int) -> str:
        return str(
            RedisDsn.build(
                scheme="redis",
                host=self.host,
                port=self.port,
                password=self.password,
                path=str(db),
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_dsn(self) -> str:
        return self._dsn(self.db)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_broker_dsn(self) -> str:
        return self._dsn(self.celery_broker_db)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_result_dsn(self) -> str:
        return self._dsn(self.celery_result_db)


class KeycloakSettings(BaseSettings):
    """Keycloak/OIDC settings. Tokens are verified in-app against the realm JWKS."""

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_KEYCLOAK_")

    server_url: str = "http://localhost:8080"
    realm: str = "claims-workbench"
    client_id: str = "claims-workbench-api"
    client_secret: str | None = None
    audience: str = "account"
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_cache_ttl_seconds: int = 3600
    verify_audience: bool = True
    leeway_seconds: int = 10

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realm_url(self) -> str:
        return f"{self.server_url.rstrip('/')}/realms/{self.realm}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jwks_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/certs"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def issuer(self) -> str:
        return self.realm_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/token"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def authorization_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/auth"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def end_session_url(self) -> str:
        """Where a sign-out sends the browser to end the *Keycloak* session.

        Clearing our own cookie is not signing out: the realm's SSO session would
        still be live, and the next `/auth/login` would sail through it without
        asking for a password.
        """
        return f"{self.realm_url}/protocol/openid-connect/logout"


class AuthSettings(BaseSettings):
    """The browser's session: a short-lived access token and how it is renewed.

    The access token is handed to the SPA and held in memory there — never in
    `localStorage`, never in a cookie. The *refresh* token never reaches the
    browser at all: it is kept in Redis behind an opaque handle, and the handle is
    what travels in an `HttpOnly` cookie. So the worst an XSS can do is use the
    session while the tab is open; it cannot walk away with a durable credential.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_AUTH_")

    #: Where Keycloak sends the browser back to. Must be registered on the client
    #: as a valid redirect URI, exactly.
    redirect_url: str = "http://localhost:8000/api/v1/auth/callback"

    #: Where the callback forwards the browser once the grant is stored. The SPA
    #: takes it from there and calls `/auth/token` for its first access token.
    frontend_url: str = "http://localhost:5173"

    #: Where a signed-out or refused browser lands.
    #:
    #: The sign-in screen rather than the marketing root: somebody who has just
    #: pressed "sign out" is at a keyboard and means to sign in again or hand the
    #: machine over, and the landing page makes them find the way back in. Must be
    #: covered by the client's registered post-logout redirect URIs, which the
    #: bootstrap script sets to the frontend origin plus a wildcard.
    sign_in_path: str = "/sign-in"

    #: Whether the single sign-on redirect is offered at all.
    #:
    #: Off for now, by decision rather than by omission. The whole path is built and
    #: tested — `GET /auth/sso`, the callback, PKCE, the nonce check — and turning
    #: this on is all that is needed to have it back. It stays in the tree because it
    #: is the only route that can carry MFA, a forced password change or federation
    #: to an external directory; the password grant cannot ask a second question.
    #:
    #: Gating the *route* as well as the button is the point. A hidden control whose
    #: URL still works is not a disabled feature, it is an undocumented one.
    sso_enabled: bool = False

    #: Requested at the authorize endpoint. `openid` is what makes it OIDC rather
    #: than bare OAuth; the other two populate the principal's name and email.
    scopes: str = "openid profile email"

    #: The cookie carrying the refresh handle. Scoped to the auth routes by
    #: `cookie_path`, so it is not attached to a single other request.
    cookie_name: str = "cwb_refresh"
    cookie_path: str = "/api/v1/auth"
    cookie_domain: str | None = None

    #: `None` derives it: on everywhere except a `local` environment. Never a
    #: plain `False` default — a cookie that forgets `Secure` in production is the
    #: kind of mistake that does not announce itself.
    cookie_secure: bool | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    #: The in-flight OIDC handshake: the PKCE verifier, the state and the nonce,
    #: held in their own cookie between `/auth/login` and `/auth/callback`.
    transaction_cookie_name: str = "cwb_oidc_tx"
    transaction_ttl_seconds: int = 300

    #: How long a grant survives without being used, and how long it may live at
    #: all. A desk leaves tabs open all day, so an idle bound alone would never
    #: expire; an absolute bound alone would sign someone out mid-sentence.
    idle_timeout_seconds: int = 1800
    absolute_timeout_seconds: int = 43_200

    #: Refresh a little before the access token actually expires, so a request is
    #: never issued with a token that dies in flight.
    refresh_margin_seconds: int = 30

    #: Failed sign-in attempts allowed per window, counted per IP and per email.
    #: Keycloak locks a single account after its own threshold, but is blind to one
    #: source trying ten thousand accounts once each — which is what this catches.
    login_max_attempts: int = 10
    login_window_seconds: int = 300

    #: Required on the two cookie-bearing endpoints. A cross-site form cannot set
    #: a custom header, and these routes are POST-only, so this plus the origin
    #: check is what stands in for a CSRF token.
    required_header: str = "X-Requested-With"

    timeout_seconds: float = 10.0
    max_attempts: int = 3


class S3Settings(BaseSettings):
    """S3-compatible object storage (MinIO locally)."""

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_S3_")

    endpoint_url: str | None = "http://localhost:9000"
    region: str = "us-east-1"
    access_key_id: str = "minioadmin"
    secret_access_key: str = "minioadmin"
    bucket: str = "claims-documents"
    presign_expiry_seconds: int = 900
    use_path_style: bool = True


class CelerySettings(BaseSettings):
    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_CELERY_")

    task_default_queue: str = "default"
    task_time_limit: int = 600
    task_soft_time_limit: int = 540
    worker_prefetch_multiplier: int = 1
    worker_max_tasks_per_child: int = 200
    task_always_eager: bool = False
    timezone: str = "UTC"


class ObservabilitySettings(BaseSettings):
    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_OBS_")

    log_level: str = "INFO"
    log_json: bool | None = None  # None -> derived from environment
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    request_id_header: str = "X-Request-ID"


class AISettings(BaseSettings):
    """The large-language-model provider used by the FNOL intelligence services.

    The key is read from the environment and never leaves the server. When no key
    is configured the application falls back to the deterministic in-process
    extractor (`app.services.ai.heuristic`) rather than failing — a claims desk
    still has to be able to work an FNOL when the provider is down, and a demo
    environment still has to run end to end.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_AI_")

    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.6-luna"
    # Structured extraction is a long prompt over document text, so the budget is
    # wider than a chat turn's and the timeout longer than the generic HTTP one.
    # Reasoning tokens are billed inside this budget on a reasoning model, but they
    # measure in the tens for an extraction, so the headroom is unchanged.
    max_output_tokens: int = 4096
    # `None` omits the parameter, which is what a reasoning model requires — the
    # GPT-5 family accepts only its default of 1 and rejects the request outright
    # otherwise. Set it to 0.0 when pointing `model` back at a GPT-4-class model
    # that honours it.
    temperature: float | None = None
    # One of none / low / medium / high / xhigh, or `None` to omit and take the
    # provider's default. Extraction is a faithfulness task rather than a puzzle,
    # so it does not need to be turned up.
    reasoning_effort: str | None = None
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    # Hard ceiling on the characters of document text sent in one call. Anything
    # beyond this is truncated per document, which keeps a 400-page schedule from
    # blowing the context window and the bill at the same time.
    max_input_characters: int = 60_000
    enabled: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)


class DocumentIntelligenceSettings(BaseSettings):
    """Reading claim documents into searchable, citable passages.

    Three capabilities, each independently optional, and each degrading rather
    than failing when it is not configured — the same posture `AISettings` takes:

    * **Chunking** is free and deterministic, so it is always on. It is what makes
      a citation possible: a chunk carries the page it came from and its character
      offsets into the document's text, which is what turns "the model said
      CP-2026-4471" into "page 4 of the survey report says CP-2026-4471".
    * **Embeddings and a vector store** narrow a long document set down to the
      passages that mention a field before the extraction prompt is built. Without
      them retrieval falls back to Postgres full-text over the same chunks, and
      below `retrieval_min_chunks` the whole corpus is sent exactly as it is today.
    * **OCR** is reached over HTTP and detected automatically. Without it a scan
      stays `UNSUPPORTED`, which is what it is today.

    Nothing here is required for the FNOL pipeline to run.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_DOCINT_")

    #: Master switch. Off means no chunking, no indexing and no retrieval — the
    #: pipeline reads the whole corpus, which is the behaviour before this module
    #: existed. Kept so a deployment can turn the feature off without redeploying.
    enabled: bool = True

    # --- Chunking ------------------------------------------------------------
    #: Tokens per chunk. Large enough that a claim form's field and its value stay
    #: together, small enough that a retrieved passage is mostly signal.
    chunk_tokens: int = 320
    #: Overlap, so a value split across a boundary is whole in one of the two.
    chunk_overlap_tokens: int = 48
    #: A 400-page schedule truncates with a log line rather than filling a table.
    max_chunks_per_document: int = 2_000

    # --- Embeddings ----------------------------------------------------------
    embedding_model: str = "text-embedding-3-small"
    #: Checked against the provider's first response. A mismatch is an error, not a
    #: silently wrong index that returns plausible nonsense for a year.
    embedding_dimension: int = 1536
    #: Both fall back to the chat provider's settings, so one key configures both
    #: and a deployment that routes them separately still can.
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_batch_size: int = 64
    embedding_timeout_seconds: float = 30.0
    embedding_max_attempts: int = 3

    # --- Vector store --------------------------------------------------------
    #: Unset means no vector store, which means keyword-only retrieval. Not an
    #: error: Postgres full-text over the same chunks is a real fallback.
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "fnol_document_chunks"
    qdrant_timeout_seconds: float = 15.0

    # --- Retrieval -----------------------------------------------------------
    retrieval_enabled: bool = True
    #: Passages per field group. Six is enough for one field's evidence and small
    #: enough that seven groups still fit the prompt budget.
    retrieval_top_k: int = 6
    #: A floor, which IIF's retrieval lacks: top-k with no threshold returns six
    #: passages about nothing on a case whose documents mention none of the terms.
    retrieval_min_score: float = 0.25
    retrieval_semantic_weight: float = 0.7
    retrieval_keyword_weight: float = 0.3
    #: Below this many chunks on a case, retrieval is skipped and the whole corpus
    #: is sent — selecting 6 passages out of 8 is pure overhead and pure risk.
    retrieval_min_chunks: int = 12
    #: Ceiling on passages in one extraction prompt, across all field groups.
    max_evidence_chunks: int = 24
    retrieval_concurrency: int = 4

    # --- OCR -----------------------------------------------------------------
    #: Unset means no OCR: a scan is reported as needing it rather than read.
    ocr_url: str | None = None
    ocr_timeout_seconds: float = 180.0
    ocr_language: str = "en"
    ocr_dpi: int = 300
    #: Cost ceiling per document. A 400-page scan is not OCR'd in full to answer
    #: "what is the policy number".
    ocr_max_pages: int = 50
    ocr_min_confidence: float = 0.40

    # --- OCR auto-detection --------------------------------------------------
    #: The three signals, any of which is enough to send a PDF to OCR. Defaults are
    #: the ones IIF's detector uses.
    ocr_min_chars_per_page: int = 50
    ocr_empty_page_ratio: float = 0.5
    ocr_image_only_ratio: float = 0.5

    # --- Orchestration -------------------------------------------------------
    #: Attempts per document before it is left alone for a human, mirroring
    #: `CWB_GRAPH_MAX_ATTEMPTS`.
    index_max_attempts: int = 3
    #: A document stuck mid-index for longer than this is assumed to have lost its
    #: worker and is returned to the queue.
    stale_index_minutes: int = 30
    #: Registers the beat entry that picks up cases the mailbox left queued. This
    #: is the seam `docs/mail-intake.md` documents and nothing previously fulfilled.
    queue_poll_enabled: bool = True
    queue_poll_interval_seconds: int = 60
    #: Cases enqueued per beat tick.
    queue_batch_size: int = 20

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_embedding_api_key(self) -> str | None:
        """The embedding key, falling back to the chat provider's."""
        return self.embedding_api_key or AISettings().api_key

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_embedding_base_url(self) -> str:
        return self.embedding_base_url or AISettings().base_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def embeddings_configured(self) -> bool:
        """True when a key is resolvable. Never a connectivity check."""
        return bool(self.enabled and self.resolved_embedding_api_key)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vector_configured(self) -> bool:
        return bool(self.enabled and self.qdrant_url and self.embeddings_configured)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ocr_configured(self) -> bool:
        return bool(self.enabled and self.ocr_url)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def index_signature(self) -> str:
        """The chunk and embedding parameters, hashed.

        Feeds every document's index fingerprint, so changing a chunk size
        re-indexes the corpus rather than leaving a collection half in one shape
        and half in another — which is the failure mode that produces retrieval
        results nobody can explain.
        """
        material = "|".join(
            str(part)
            for part in (
                "v1",
                self.chunk_tokens,
                self.chunk_overlap_tokens,
                self.embedding_model if self.embeddings_configured else "none",
                self.embedding_dimension if self.embeddings_configured else 0,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class FNOLSettings(BaseSettings):
    """Underwriting-facing thresholds for the FNOL pipeline.

    These are business rules, not code constants: a carrier changes its major-loss
    threshold far more often than it changes its claims system, so they are
    configuration and the services read them rather than hardcoding numbers.
    Amounts are in the minor unit of `base_currency`.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_FNOL_")

    base_currency: str = "GBP"

    # Severity bands, by estimated loss in minor units.
    severity_medium_threshold_minor: int = 25_000_00
    severity_high_threshold_minor: int = 250_000_00
    severity_critical_threshold_minor: int = 1_000_000_00
    # Above this, triage routes to the major-loss team regardless of other signals.
    major_loss_threshold_minor: int = 500_000_00

    # Scores are 0..1 throughout the pipeline.
    duplicate_similarity_threshold: float = 0.62
    duplicate_strong_threshold: float = 0.85
    policy_match_exact_threshold: float = 0.99
    policy_match_high_threshold: float = 0.80
    policy_match_candidate_threshold: float = 0.45
    low_confidence_threshold: float = 0.60
    cat_match_threshold: float = 0.55
    cat_radius_km: float = 250.0
    # How many days either side of an event window a loss may fall and still match.
    cat_date_tolerance_days: int = 2

    # Fraud indicator scoring.
    fraud_medium_threshold: float = 0.35
    fraud_high_threshold: float = 0.65
    # A loss within this many days of inception or expiry is an indicator.
    policy_edge_window_days: int = 30

    completeness_ready_threshold: float = 0.75
    max_document_bytes: int = 25 * 1024 * 1024
    max_documents_per_case: int = 40

    #: Where attachment bytes live. `s3` is the deployed answer; `filesystem` is
    #: for a developer machine running only Postgres and Redis, and writes under
    #: `document_store_path`. The store is chosen by configuration rather than by
    #: probing, so a misconfigured deployment fails loudly instead of quietly
    #: writing claim documents to a container's local disk.
    document_store: Literal["s3", "filesystem"] = "s3"
    document_store_path: str = "./var/documents"


class GraphSettings(BaseSettings):
    """Microsoft Graph, for the shared-mailbox FNOL intake.

    The client-credentials flow is the whole point of this block: the mailbox is
    read by the *service*, under application permissions granted to the app
    registration, so nothing here needs a signed-in user and nothing here goes
    through Keycloak. Keycloak authorises the humans who look at intake; Graph
    authorises the service that collects it. They are separate concerns and this
    module works with only the second one configured.

    The four values a tenant administrator hands over are accepted under either
    the project's own `CWB_GRAPH_*` names or the bare `GRAPH_*` /
    `OUTLOOK_SHARED_MAILBOX` names Microsoft's own documentation uses, because
    those are the names they usually arrive in and renaming a secret on its way
    into a deployment is a good way to lose it.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_GRAPH_", populate_by_name=True)

    tenant_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CWB_GRAPH_TENANT_ID", "GRAPH_TENANT_ID"),
    )
    client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CWB_GRAPH_CLIENT_ID", "GRAPH_CLIENT_ID"),
    )
    client_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CWB_GRAPH_CLIENT_SECRET", "GRAPH_CLIENT_SECRET"),
    )
    #: The mailbox intake reads, e.g. `claims@carrier.example`. A UPN or an
    #: object id — Graph accepts either in `/users/{id}`.
    shared_mailbox: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CWB_GRAPH_SHARED_MAILBOX",
            "CWB_OUTLOOK_SHARED_MAILBOX",
            "OUTLOOK_SHARED_MAILBOX",
        ),
    )

    authority: str = "https://login.microsoftonline.com"
    api_base_url: str = "https://graph.microsoft.com/v1.0"
    #: `.default` asks for exactly the application permissions already consented
    #: to. There is no other sensible scope for a daemon.
    scope: str = "https://graph.microsoft.com/.default"

    mail_folder: str = "inbox"
    #: Read only what the mailbox has not been read. Turn this off and the ledger
    #: is the only thing keeping the same message from being worked twice — which
    #: it does, but at the cost of re-listing the whole folder every poll.
    unread_only: bool = True
    batch_size: int = 25
    poll_interval_seconds: int = 300
    #: Beat only schedules the poll when this is on *and* the credentials are
    #: present, so an unconfigured environment is silent rather than noisy.
    poll_enabled: bool = False
    #: How many times one message is retried before it is left alone for a human.
    #: Without this, a message that cannot be processed is retried forever.
    max_attempts: int = 3

    timeout_seconds: float = 30.0
    http_max_attempts: int = 3

    # --- What intake does to the mailbox -------------------------------------
    #: Marking read is how the folder drains. It is a flag on a message, not a
    #: move and not a delete, and it needs `Mail.ReadWrite`. Set it false to run
    #: on `Mail.Read` alone; the ledger still prevents double-processing.
    mark_as_read: bool = True
    #: Opt-in, and off by default: moving a broker's notification out of the
    #: inbox changes what a human sees in Outlook, which is not a decision an
    #: integration should make for a claims team without being asked.
    move_to_folder: str | None = None

    #: Which email channel these notifications are recorded as. A shared claims
    #: mailbox is a broker mailbox in nearly every deployment; where it is not,
    #: this is the switch.
    from_broker: bool = True

    #: Inline attachments are signature logos far more often than they are
    #: evidence, and a notice whose document list is four copies of a broker's
    #: letterhead is a notice nobody reads. Turn this on for a mailbox whose
    #: senders paste damage photographs into the body.
    include_inline_attachments: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def configured(self) -> bool:
        """True when all four credentials are present. Never a connectivity check."""
        return bool(
            self.tenant_id and self.client_id and self.client_secret and self.shared_mailbox
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_url(self) -> str:
        return f"{self.authority.rstrip('/')}/{self.tenant_id}/oauth2/v2.0/token"


class ExtractionSettings(BaseSettings):
    """Reading a notice against a configurable dataset.

    The dataset itself is rows in Postgres, edited by an administrator — none of
    it belongs here. What belongs here is the cost and shape of one run: how many
    fields share a model call, how many passages each call may carry, and how
    hard retrieval works per field.

    The defaults are chosen for a claim notice: tens of fields, a handful of
    attachments. A dataset an order of magnitude larger should raise
    `fields_per_call` before anything else — it is the setting that trades a
    linear number of model calls for a longer prompt.
    """

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_EXTRACT_")

    #: Off means the pipeline reads a notice the way it did before configurable
    #: datasets existed: one call against the fixed FNOL schema in
    #: `app.domain.extraction`. Kept so the new path can be disabled without a
    #: rollback, and so the pre-existing behaviour stays reachable and tested.
    schema_driven: bool = True

    #: Create the bundled datasets on boot if they are missing. Additive and
    #: idempotent; it never overwrites an edited field.
    seed_on_startup: bool = True

    #: Fields per model call. One call per field is the naive shape and costs a
    #: call per field; one call for everything loses per-field retrieval, which is
    #: where the accuracy comes from. Batching keeps both: retrieval is per field,
    #: and the passages of a batch's fields are deduplicated into one prompt.
    fields_per_call: int = 10

    #: Passages retrieved per field before deduplication across a batch.
    passages_per_field: int = 4
    #: Ceiling on distinct passages in one call, after deduplication. A batch that
    #: would exceed it keeps the highest-scoring passages.
    max_passages_per_call: int = 30

    #: Chunks of the notification body included in *every* call, whatever
    #: retrieval thought. The body is the notice itself, it is small, and a
    #: retrieval miss on it is the one failure this mechanism must not cause.
    body_chunks_always_included: int = 8

    #: Concurrent model calls. Bounded for the same reason retrieval is: a burst
    #: of thirty calls is how a deployment gets rate-limited on the request that
    #: matters.
    call_concurrency: int = 3

    #: How much of one call's prompt may be passages. The rest is the field list.
    max_passage_characters: int = 60_000

    #: Below this confidence a value is flagged for a human rather than trusted.
    #: A dataset may override it; this is the fallback for one that does not.
    review_threshold: float = 0.6

    #: Weight given to the model's own confidence when blending it with how well
    #: retrieval scored the passage the value came from. The remainder goes to
    #: retrieval. A model is the better judge of whether it read a value; the
    #: retrieval score is the better judge of whether it was looking in the right
    #: place, and neither alone is enough.
    llm_confidence_weight: float = 0.7

    #: Cache resolved highlight rectangles on the value row. Turning this off
    #: re-resolves them on every evidence request, which is correct but costs a
    #: download and a parse each time.
    cache_highlight_rects: bool = True


class Settings(BaseSettings):
    """Root application settings."""

    model_config = _ENV_CONFIG | SettingsConfigDict(env_prefix="CWB_")

    # --- Application ---
    project_name: str = "Claims Workbench API"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = True

    # Comma-separated in the environment, e.g. CWB_CORS_ORIGINS="http://localhost:5173".
    # `NoDecode` stops pydantic-settings from JSON-decoding the raw value in the
    # source layer, which happens before validators run and would reject a plain
    # comma-separated string outright. The validator below accepts both forms.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- Knowledge graph ---
    # Emit the scaffolding entities described in `app.domain.knowledge_graph` — a
    # loss adjuster, SIU indicators and a historical claim — covering the entities
    # a claims graph is expected to carry that this data model has no feed for yet.
    # Each is marked `provenance="seeded"` on the wire and on screen, so the flag
    # changes how complete the graph looks and never whether it is honest. Set
    # `CWB_KNOWLEDGE_GRAPH_SEEDED_ENTITIES=false` for a records-only graph.
    knowledge_graph_seeded_entities: bool = True

    # --- Grouped settings ---
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ai: AISettings = Field(default_factory=AISettings)
    fnol: FNOLSettings = Field(default_factory=FNOLSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    docint: DocumentIntelligenceSettings = Field(default_factory=DocumentIntelligenceSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept either a JSON array or a comma-separated list."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("["):
            return json.loads(stripped)
        return [origin.strip() for origin in stripped.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def sso_available(self) -> bool:
        """Whether the redirect flow can actually complete.

        Both halves are required, and for different reasons: the flag is a decision,
        and the client secret is a capability. Reporting `true` without the secret
        would render a button that answers 401.
        """
        return self.auth.sso_enabled and bool(self.keycloak.client_secret)

    @property
    def sign_in_url(self) -> str:
        """The absolute sign-in URL, for the realm and for redirects."""
        return f"{self.auth.frontend_url.rstrip('/')}{self.auth.sign_in_path}"

    @property
    def cookies_secure(self) -> bool:
        """Whether auth cookies carry `Secure`.

        Derived unless explicitly set, and derived to `True` everywhere except a
        `local` environment — including `test`, so a suite can only ever assert the
        production flag shape. Overriding it is possible and is meant to be a
        deliberate act rather than the default that a forgotten variable produces.
        """
        if self.auth.cookie_secure is not None:
            return self.auth.cookie_secure
        return self.environment != "local"

    @property
    def log_as_json(self) -> bool:
        if self.observability.log_json is not None:
            return self.observability.log_json
        return self.is_production

    @property
    def docs_url(self) -> str | None:
        return None if self.is_production else "/docs"

    @property
    def openapi_url(self) -> str | None:
        return None if self.is_production else "/openapi.json"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use this rather than instantiating `Settings`."""
    return Settings()


settings = get_settings()
