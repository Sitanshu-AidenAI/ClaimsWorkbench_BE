"""Settings construction and DSN derivation."""

from __future__ import annotations

import pytest

from app.core.config import PostgresSettings, RedisSettings, Settings


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
