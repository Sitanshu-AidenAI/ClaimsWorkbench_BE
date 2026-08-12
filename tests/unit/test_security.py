"""Keycloak token verification.

An RSA keypair is generated in-process and the JWKS lookup is stubbed, so these
tests exercise the real `jwt.decode` path — signature, issuer, audience and
expiry — without needing a Keycloak instance.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import KeycloakSettings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import JWTVerifier, principal_from_claims

KEYCLOAK = KeycloakSettings(
    server_url="http://keycloak.test",
    realm="claims-workbench",
    client_id="claims-workbench-api",
    audience="claims-workbench-api",
)


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(private_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> JWTVerifier:
    instance = JWTVerifier(KEYCLOAK)

    class _StubSigningKey:
        key = private_key.public_key()

    class _StubClient:
        def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
            return _StubSigningKey()

    monkeypatch.setattr(instance, "_client", lambda: _StubClient())
    return instance


def make_token(private_key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-1",
        "iss": KEYCLOAK.issuer,
        "aud": KEYCLOAK.audience,
        "iat": now,
        "exp": now + 300,
        "preferred_username": "adjuster",
        "email": "adjuster@example.com",
        "name": "A Adjuster",
        "realm_access": {"roles": ["claims-adjuster"]},
        "resource_access": {KEYCLOAK.client_id: {"roles": ["claims-admin"]}},
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_valid_token_yields_a_principal(
    verifier: JWTVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    principal = verifier.principal_from_token(make_token(private_key))

    assert principal.subject == "user-1"
    assert principal.username == "adjuster"
    assert principal.realm_roles == frozenset({"claims-adjuster"})
    assert principal.client_roles == frozenset({"claims-admin"})
    assert principal.roles == frozenset({"claims-adjuster", "claims-admin"})


def test_expired_token_is_rejected(verifier: JWTVerifier, private_key: rsa.RSAPrivateKey) -> None:
    now = int(time.time())
    token = make_token(private_key, iat=now - 600, exp=now - 300)

    with pytest.raises(AuthenticationError):
        verifier.decode(token)


def test_wrong_issuer_is_rejected(verifier: JWTVerifier, private_key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(AuthenticationError):
        verifier.decode(make_token(private_key, iss="http://evil.test/realms/other"))


def test_wrong_audience_is_rejected(verifier: JWTVerifier, private_key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(AuthenticationError):
        verifier.decode(make_token(private_key, aud="some-other-client"))


def test_token_signed_by_another_key_is_rejected(verifier: JWTVerifier) -> None:
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(AuthenticationError):
        verifier.decode(make_token(attacker_key))


def test_missing_subject_is_rejected() -> None:
    with pytest.raises(AuthenticationError):
        principal_from_claims({"iss": KEYCLOAK.issuer}, client_id=KEYCLOAK.client_id)


def test_require_role_allows_a_held_role() -> None:
    principal = principal_from_claims(
        {"sub": "u", "realm_access": {"roles": ["claims-admin"]}},
        client_id=KEYCLOAK.client_id,
    )

    principal.require_role("claims-admin", "supervisor")  # does not raise


def test_require_role_rejects_a_missing_role() -> None:
    principal = principal_from_claims(
        {"sub": "u", "realm_access": {"roles": ["claims-adjuster"]}},
        client_id=KEYCLOAK.client_id,
    )

    with pytest.raises(PermissionDeniedError):
        principal.require_role("claims-admin")
