"""Application configuration.

Single source of truth for runtime settings. Values are read from the process
environment (and `.env` in development) with the `CWB_` prefix, e.g.
`CWB_POSTGRES_HOST`. Nested settings use a double underscore delimiter, so
`CWB_KEYCLOAK__REALM` maps to `settings.keycloak.realm`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, computed_field, field_validator
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
    model: str = "gpt-4o-mini"
    # Structured extraction is a long prompt over document text, so the budget is
    # wider than a chat turn's and the timeout longer than the generic HTTP one.
    max_output_tokens: int = 4096
    temperature: float = 0.0
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

    # --- Authentication ---
    # Accept locally-minted development tokens instead of Keycloak-issued ones.
    # Guarded by `dev_auth_enabled` below, which refuses outright outside
    # local/test: the flag existing is not the same as the flag being honoured.
    dev_auth: bool = False
    dev_auth_default_roles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "fnol-officer",
            "claims-handler",
            "claims-manager",
            "claims-admin",
            "business-admin",
        ]
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
    s3: S3Settings = Field(default_factory=S3Settings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ai: AISettings = Field(default_factory=AISettings)
    fnol: FNOLSettings = Field(default_factory=FNOLSettings)

    @field_validator("cors_origins", "dev_auth_default_roles", mode="before")
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
    def dev_auth_enabled(self) -> bool:
        """Development tokens are only ever honoured outside staging/production.

        The environment check is here rather than at the call site so there is one
        place to read: setting `CWB_DEV_AUTH=true` on a production deployment
        changes nothing.
        """
        return self.dev_auth and not self.is_production

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
