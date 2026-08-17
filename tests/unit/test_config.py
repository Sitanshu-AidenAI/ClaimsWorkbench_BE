"""Settings construction and DSN derivation."""

from __future__ import annotations

import pytest

from app.core.config import GraphSettings, PostgresSettings, RedisSettings, Settings


def test_postgres_dsns_use_the_right_drivers() -> None:
    postgres = PostgresSettings(host="db", port=5433, user="u", password="p", db="claims")

    assert postgres.async_dsn == "postgresql+asyncpg://u:p@db:5433/claims"
    assert postgres.sync_dsn == "postgresql+psycopg2://u:p@db:5433/claims"
    assert postgres.asyncpg_dsn == "postgresql://u:p@db:5433/claims"


def test_redis_separates_cache_broker_and_result_databases() -> None:
    redis = RedisSettings(host="cache", port=6380)

    assert redis.cache_dsn == "redis://cache:6380/0"
    assert redis.celery_broker_dsn == "redis://cache:6380/1"
    assert redis.celery_result_dsn == "redis://cache:6380/2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://a.test,http://b.test", ["http://a.test", "http://b.test"]),
        ("http://a.test, http://b.test ", ["http://a.test", "http://b.test"]),
        ('["http://a.test"]', ["http://a.test"]),
    ],
)
def test_cors_origins_accept_comma_separated_and_json(raw: str, expected: list[str]) -> None:
    assert Settings(cors_origins=raw).cors_origins == expected


def test_docs_are_disabled_in_production() -> None:
    production = Settings(environment="production")

    assert production.is_production is True
    assert production.docs_url is None
    assert production.openapi_url is None
    assert production.log_as_json is True


def test_docs_are_enabled_locally() -> None:
    local = Settings(environment="local")

    assert local.docs_url == "/docs"
    assert local.log_as_json is False


def test_keycloak_urls_derive_from_server_and_realm() -> None:
    settings = Settings()
    keycloak = settings.keycloak

    assert keycloak.issuer == keycloak.realm_url
    assert keycloak.jwks_url.endswith("/protocol/openid-connect/certs")
    assert keycloak.token_url.endswith("/protocol/openid-connect/token")


class TestGraphSettings:
    """Mailbox intake reads its four credentials from the environment only."""

    def test_a_deployment_without_credentials_is_not_configured(self) -> None:
        graph = GraphSettings(
            tenant_id=None, client_id=None, client_secret=None, shared_mailbox=None
        )

        assert graph.configured is False

    def test_all_four_credentials_make_it_configured(self) -> None:
        graph = GraphSettings(
            tenant_id="t", client_id="c", client_secret="s", shared_mailbox="claims@carrier.test"
        )

        assert graph.configured is True
        assert graph.token_url == "https://login.microsoftonline.com/t/oauth2/v2.0/token"

    def test_three_of_four_is_not_configured(self) -> None:
        # A half-configured mailbox is a misconfiguration, not a feature flag.
        graph = GraphSettings(tenant_id="t", client_id="c", client_secret="s", shared_mailbox=None)

        assert graph.configured is False

    @pytest.mark.parametrize(
        "names",
        [
            {
                "CWB_GRAPH_TENANT_ID": "t",
                "CWB_GRAPH_CLIENT_ID": "c",
                "CWB_GRAPH_CLIENT_SECRET": "s",
                "CWB_GRAPH_SHARED_MAILBOX": "claims@carrier.test",
            },
            # The names an Azure administrator hands over, unrenamed.
            {
                "GRAPH_TENANT_ID": "t",
                "GRAPH_CLIENT_ID": "c",
                "GRAPH_CLIENT_SECRET": "s",
                "OUTLOOK_SHARED_MAILBOX": "claims@carrier.test",
            },
        ],
    )
    def test_either_set_of_environment_names_is_accepted(
        self, names: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in names.items():
            monkeypatch.setenv(key, value)

        graph = GraphSettings()

        assert graph.configured is True
        assert graph.shared_mailbox == "claims@carrier.test"

    def test_nothing_is_marked_or_moved_without_being_asked(self) -> None:
        # `_env_file=None` because this test is about the *declared defaults*, and
        # `GraphSettings` reads `.env` unconditionally. A developer whose own `.env`
        # points at a real mailbox with polling on would otherwise fail this — which
        # it did: the assertion below was reporting the machine it ran on rather
        # than the code, and a default that only holds on a clean checkout is not
        # a default anybody can rely on.
        graph = GraphSettings(_env_file=None)

        # Reading a mailbox is safe; changing it is a decision. Marking read is
        # the one flag on by default, and it is not destructive.
        assert graph.move_to_folder is None
        assert graph.mark_as_read is True
        assert graph.poll_enabled is False
        assert graph.include_inline_attachments is False
