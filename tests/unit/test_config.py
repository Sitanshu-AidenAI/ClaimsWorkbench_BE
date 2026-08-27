"""Settings construction and DSN derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import config
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

    def test_the_sweep_does_not_default_to_trusting_the_read_flag(self) -> None:
        """The default that a lost claim paid for.

        `unread_only` was on, so intake asked Graph for `isRead eq false`. But
        `isRead` belongs to the claims team — a handler opening the shared
        mailbox in Outlook clears it — and a message cleared before intake
        reached it was excluded from every subsequent poll, permanently, while
        each poll went on reporting success. What has been collected is a row in
        `mail_intake_messages`; the mailbox is not a cursor. `_env_file=None` for
        the same reason as the test above.
        """
        graph = GraphSettings(_env_file=None)

        assert graph.unread_only is False
        # And the sweep must be able to reach past one page, or the same symptom
        # returns from the other direction on any folder bigger than a batch.
        assert graph.max_pages > 1
        assert graph.lookback_minutes > 0


class TestConfigurationFreshness:
    """A process cannot reload its settings; it can at least admit they are old.

    Mailbox intake once ran for twenty-two hours on a flag that had been changed
    thirteen minutes after the worker started, reporting a successful poll every
    eight seconds against configuration nobody believed it still had. Nothing in
    the code was wrong. This is the guard that makes that visible in one tick.
    """

    def test_configuration_older_than_the_env_file_is_reported_as_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CWB_ENVIRONMENT=local\n")
        monkeypatch.setitem(config._ENV_CONFIG, "env_file", str(env_file))
        monkeypatch.setattr(config, "SETTINGS_LOADED_AT", env_file.stat().st_mtime - 60)

        stale_by = config.env_file_changed_since_load()

        assert stale_by is not None
        assert stale_by == pytest.approx(60, abs=2)

    def test_configuration_newer_than_the_env_file_is_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CWB_ENVIRONMENT=local\n")
        monkeypatch.setitem(config._ENV_CONFIG, "env_file", str(env_file))
        monkeypatch.setattr(config, "SETTINGS_LOADED_AT", env_file.stat().st_mtime + 60)

        assert config.env_file_changed_since_load() is None

    def test_a_deployment_with_no_env_file_cannot_drift_and_says_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A container takes its configuration as real environment variables.
        monkeypatch.setitem(config._ENV_CONFIG, "env_file", str(tmp_path / "absent"))

        assert config.env_file_changed_since_load() is None
