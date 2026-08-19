"""The browser session: refresh grants, the OIDC routes, and what they refuse.

Redis and Keycloak are both faked, so this runs on every commit. What it pins is
the security behaviour rather than the plumbing: that a rotated handle cannot be
replayed, that a cross-site caller cannot reach the cookie endpoints, that no
unsigned token is accepted any more, and that the access token is the only thing
that ever leaves the server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps.db import get_session
from app.core.config import AuthSettings, settings
from app.core.errors import AuthenticationError
from app.core.oidc import PkcePair, TokenResponse
from app.domain.capabilities import Capability, capabilities_for
from app.services.access.service import AccessService
from app.services.auth.store import GrantReuseError, RefreshGrantStore
from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.anyio


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.services.auth.store.get_redis", lambda: fake)
    monkeypatch.setattr("app.services.auth.throttle.get_redis", lambda: fake)
    return fake


@pytest.fixture
def store() -> RefreshGrantStore:
    return RefreshGrantStore(AuthSettings(idle_timeout_seconds=1800, absolute_timeout_seconds=7200))


# --- The store ----------------------------------------------------------------


async def test_a_grant_round_trips(redis: FakeRedis, store: RefreshGrantStore) -> None:
    grant = await store.create(refresh_token="refresh-1", subject="sub-1", id_token="id-1")

    resolved = await store.get(grant.handle)

    assert resolved is not None
    assert resolved.refresh_token == "refresh-1"
    assert resolved.subject == "sub-1"
    assert resolved.id_token == "id-1"


async def test_the_handle_is_opaque_and_carries_nothing(
    store: RefreshGrantStore, redis: FakeRedis
) -> None:
    """The cookie value must not be the credential, or be derivable from it."""
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")

    assert "refresh-1" not in grant.handle
    assert "sub-1" not in grant.handle
    # 256 bits, urlsafe-base64: comfortably unguessable.
    assert len(grant.handle) >= 40


async def test_an_unknown_handle_resolves_to_nothing(
    store: RefreshGrantStore, redis: FakeRedis
) -> None:
    assert await store.get("never-issued") is None


async def test_rotation_issues_a_new_handle(redis: FakeRedis, store: RefreshGrantStore) -> None:
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")

    rotated = await store.rotate(grant, refresh_token="refresh-2")

    assert rotated.handle != grant.handle
    assert rotated.family == grant.family
    resolved = await store.get(rotated.handle)
    assert resolved is not None
    assert resolved.refresh_token == "refresh-2"


async def test_replaying_a_rotated_handle_revokes_the_whole_family(
    redis: FakeRedis, store: RefreshGrantStore
) -> None:
    """The property that makes rotation worth doing.

    A cookie captured off the wire is useless once the legitimate tab has
    refreshed — and presenting it *announces the theft* rather than failing
    quietly, because there is no innocent reason for two parties to hold one
    handle.
    """
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")
    rotated = await store.rotate(grant, refresh_token="refresh-2")

    with pytest.raises(GrantReuseError):
        await store.get(grant.handle)

    # Not just the replayed handle: the live one goes too, so the attacker and the
    # legitimate session are both stopped.
    assert await store.get(rotated.handle) is None


async def test_a_retired_handle_is_kept_known_rather_than_deleted(
    redis: FakeRedis, store: RefreshGrantStore
) -> None:
    """Deleting it would be tidier and would hide the theft.

    An unknown handle is what an ordinary expiry looks like. If rotation deleted
    the old key, a stolen cookie would expire quietly instead of raising.
    """
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")
    await store.rotate(grant, refresh_token="refresh-2")

    stored = json.loads(redis.values[f"auth:grant:{grant.handle}"])
    assert stored["retired"] is True
    assert "refresh_token" not in stored


async def test_the_absolute_deadline_ends_a_session_however_active(
    redis: FakeRedis, store: RefreshGrantStore
) -> None:
    """The idle bound is the key TTL; this one is checked on read.

    A desk that keeps using a session must still be signed out eventually, and a
    grant kept alive by frequent use is exactly what an absolute bound is for.
    """
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")

    key = f"auth:grant:{grant.handle}"
    payload = json.loads(redis.values[key])
    payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    redis.values[key] = json.dumps(payload)

    assert await store.get(grant.handle) is None


async def test_the_idle_ttl_never_outlives_the_absolute_deadline(redis: FakeRedis) -> None:
    """A short-lived session must not be handed a long idle window."""
    store = RefreshGrantStore(AuthSettings(idle_timeout_seconds=1800, absolute_timeout_seconds=60))
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")

    assert redis.ttls[f"auth:grant:{grant.handle}"] <= 60


async def test_destroying_the_family_revokes_every_handle(
    redis: FakeRedis, store: RefreshGrantStore
) -> None:
    first = await store.create(refresh_token="refresh-1", subject="sub-1")
    second = await store.rotate(first, refresh_token="refresh-2")
    third = await store.rotate(second, refresh_token="refresh-3")

    await store.destroy_family(third.family)

    assert await store.get(third.handle) is None


async def test_corrupt_stored_json_is_treated_as_no_session(
    redis: FakeRedis, store: RefreshGrantStore
) -> None:
    grant = await store.create(refresh_token="refresh-1", subject="sub-1")
    redis.values[f"auth:grant:{grant.handle}"] = "{not json"

    assert await store.get(grant.handle) is None


# --- PKCE ---------------------------------------------------------------------


def test_pkce_challenge_is_s256_and_unpadded() -> None:
    pair = PkcePair.generate()

    assert pair.verifier != pair.challenge
    assert "=" not in pair.challenge
    assert "+" not in pair.challenge and "/" not in pair.challenge
    # Two generations must not collide, or the verifier is not doing its job.
    assert PkcePair.generate().verifier != pair.verifier


def test_a_token_response_without_an_access_token_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        TokenResponse.from_payload({"refresh_token": "r"})


def test_a_missing_expiry_defaults_rather_than_meaning_forever() -> None:
    tokens = TokenResponse.from_payload({"access_token": "a"})

    assert tokens.expires_in == 300


# --- The routes ---------------------------------------------------------------


class FakeOIDC:
    """A Keycloak that answers instantly and records what it was asked."""

    def __init__(self) -> None:
        self.configured = True
        self.exchanges: list[dict[str, str]] = []
        self.refreshes: list[str] = []
        self.revoked: list[str] = []
        self.refresh_fails = False
        self.password_attempts: list[str] = []
        #: What the realm says when the password is wrong. Swapped by the lockout
        #: test, because distinguishing the two is the whole of `_signin_refusal`.
        self.refusal = "Invalid user credentials"

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        return (
            "https://keycloak.example/realms/r/protocol/openid-connect/auth"
            f"?state={state}&nonce={nonce}&code_challenge={challenge}"
            "&code_challenge_method=S256&response_type=code"
        )

    def end_session_url(self, *, id_token: str | None = None) -> str:
        return (
            "https://keycloak.example/realms/r/protocol/openid-connect/logout"
            "?post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A5173%2Fsign-in"
        )

    async def exchange_code(self, *, code: str, verifier: str) -> TokenResponse:
        self.exchanges.append({"code": code, "verifier": verifier})
        return TokenResponse(
            access_token="access-1",
            refresh_token="refresh-1",
            id_token=None,
            expires_in=300,
            scope="openid",
        )

    async def password_grant(self, *, username: str, password: str) -> TokenResponse:
        self.password_attempts.append(username)
        if password != CORRECT_PASSWORD:
            raise AuthenticationError(f"The identity provider refused the request: {self.refusal}")
        return TokenResponse(
            access_token="access-1",
            refresh_token="refresh-1",
            id_token=None,
            expires_in=300,
            scope="openid",
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        self.refreshes.append(refresh_token)
        if self.refresh_fails:
            raise AuthenticationError("The identity provider refused the request: expired")
        return TokenResponse(
            access_token=f"access-{len(self.refreshes) + 1}",
            refresh_token=f"refresh-{len(self.refreshes) + 1}",
            id_token=None,
            expires_in=300,
            scope="openid",
        )

    async def revoke(self, refresh_token: str) -> None:
        self.revoked.append(refresh_token)


CORRECT_PASSWORD = "Workbench2026"

CLAIMS = {
    "sub": "sub-1",
    "preferred_username": "r.marsh",
    "email": "r.marsh@carrier.com",
    "name": "Rebecca Marsh",
    "realm_access": {"roles": ["claims-handler"]},
}


@pytest.fixture
def auth_app(app: object, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The application with the realm faked and the grant store on fake Redis."""
    from fastapi import FastAPI

    from app.api.v1.routes import auth as auth_routes

    assert isinstance(app, FastAPI)

    fake = FakeOIDC()

    class StubVerifier:
        def decode(self, token: str) -> dict[str, Any]:
            return CLAIMS

    # The routes resolve capabilities to put them in the session response, which
    # needs a database. These are unit tests with no engine, so the lookup is
    # answered from the pure-domain defaults instead — which is also what the service
    # itself falls back to when the table is empty, so the shape under test is real.
    async def _defaults(self: object, roles: frozenset[str]) -> frozenset[Capability]:
        return capabilities_for(roles)

    monkeypatch.setattr(AccessService, "capabilities_for_roles", _defaults)

    async def _no_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_session] = _no_session

    # The redirect flow is disabled by default now. These tests enable it, which
    # keeps them honest about what the flag controls and keeps the path covered for
    # when it is turned back on.
    monkeypatch.setattr(settings.auth, "sso_enabled", True)
    # The redirect flow is switched off by default now, so the tests that exercise
    # it turn it on explicitly. That also keeps them honest about what the flag
    # controls, and keeps the path covered for when it is turned back on.
    monkeypatch.setattr(settings.auth, "sso_enabled", True)
    monkeypatch.setattr(auth_routes, "verifier", StubVerifier())
    app.dependency_overrides[auth_routes._client] = lambda: fake
    app.dependency_overrides[auth_routes._store] = lambda: RefreshGrantStore()

    app.state.fake_oidc = fake
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def http(auth_app: Any) -> Any:
    async with (
        AsyncClient(
            transport=ASGITransport(app=auth_app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client,
        auth_app.router.lifespan_context(auth_app),
    ):
        yield client


async def _sign_in(http: AsyncClient) -> str:
    """Walk the handshake and return the refresh cookie value."""
    started = await http.get("/api/v1/auth/sso")
    transaction = started.cookies.get(settings.auth.transaction_cookie_name)
    assert transaction is not None
    state = transaction.split(".")[0]

    finished = await http.get(
        "/api/v1/auth/callback",
        params={"code": "code-1", "state": state},
        cookies={settings.auth.transaction_cookie_name: transaction},
    )
    assert finished.status_code == 303
    cookie = finished.cookies.get(settings.auth.cookie_name)
    assert cookie is not None
    return cookie


async def test_login_redirects_to_the_realm_with_pkce(http: AsyncClient) -> None:
    response = await http.get("/api/v1/auth/sso")

    assert response.status_code == 307
    location = response.headers["location"]
    assert "code_challenge_method=S256" in location
    assert "response_type=code" in location
    # The verifier stays with us; only the challenge goes to the realm.
    transaction = response.cookies.get(settings.auth.transaction_cookie_name)
    assert transaction is not None
    assert transaction.split(".")[2] not in location


async def test_the_callback_sets_an_httponly_cookie_and_no_token(http: AsyncClient) -> None:
    started = await http.get("/api/v1/auth/sso")
    transaction = started.cookies[settings.auth.transaction_cookie_name]
    state = transaction.split(".")[0]

    response = await http.get(
        "/api/v1/auth/callback",
        params={"code": "code-1", "state": state},
        cookies={settings.auth.transaction_cookie_name: transaction},
    )

    assert response.status_code == 303
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Path=/api/v1/auth" in header
    # The access token must never travel in a redirect the browser can read.
    assert "access-1" not in header
    assert "access-1" not in response.text


async def test_a_mismatched_state_is_refused(http: AsyncClient) -> None:
    started = await http.get("/api/v1/auth/sso")
    transaction = started.cookies[settings.auth.transaction_cookie_name]

    response = await http.get(
        "/api/v1/auth/callback",
        params={"code": "code-1", "state": "not-the-state"},
        cookies={settings.auth.transaction_cookie_name: transaction},
    )

    assert response.status_code == 401


async def test_a_callback_without_a_transaction_cookie_is_refused(http: AsyncClient) -> None:
    response = await http.get("/api/v1/auth/callback", params={"code": "c", "state": "s"})

    assert response.status_code == 401


async def test_token_mints_an_access_token_from_the_cookie(http: AsyncClient) -> None:
    cookie = await _sign_in(http)

    response = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"].startswith("access-")
    assert body["subject"] == "sub-1"
    assert body["roles"] == ["claims-handler"]
    # Reduced by the configured margin, so the client renews before expiry.
    assert body["expires_in"] < 300
    # The refresh token is the one thing that must never be in a response body.
    assert "refresh" not in response.text


async def test_each_refresh_rotates_the_cookie(http: AsyncClient) -> None:
    first = await _sign_in(http)

    response = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: first},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    second = response.cookies[settings.auth.cookie_name]

    assert second != first


async def test_replaying_the_previous_cookie_is_refused_and_kills_the_session(
    http: AsyncClient,
) -> None:
    first = await _sign_in(http)
    await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: first},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    replayed = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: first},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert replayed.status_code == 401


async def test_token_without_the_custom_header_is_refused(http: AsyncClient) -> None:
    """The CSRF control on the only routes that read a cookie.

    A cross-site form or image tag cannot set a custom header, and both these
    routes are POST-only, so this plus the origin check is what stands in for a
    CSRF token.
    """
    cookie = await _sign_in(http)

    response = await http.post("/api/v1/auth/token", cookies={settings.auth.cookie_name: cookie})

    assert response.status_code == 403


async def test_token_from_a_foreign_origin_is_refused(http: AsyncClient) -> None:
    cookie = await _sign_in(http)

    response = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: cookie},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://phishing.example",
        },
    )

    assert response.status_code == 403


async def test_token_without_a_cookie_is_unauthenticated(http: AsyncClient) -> None:
    response = await http.post("/api/v1/auth/token", headers={"X-Requested-With": "XMLHttpRequest"})

    assert response.status_code == 401


async def test_a_realm_that_refuses_the_refresh_ends_the_session(
    http: AsyncClient, auth_app: Any
) -> None:
    """A password change or an admin logout ends it server-side; we must follow."""
    cookie = await _sign_in(http)
    auth_app.state.fake_oidc.refresh_fails = True

    response = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 401
    assert 'cwb_refresh=""' in response.headers.get("set-cookie", "")


async def test_logout_destroys_the_grant_and_names_the_end_session_url(
    http: AsyncClient, auth_app: Any
) -> None:
    cookie = await _sign_in(http)

    response = await http.post(
        "/api/v1/auth/logout",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert "openid-connect/logout" in response.json()["end_session_url"]
    # Told the realm too: clearing our cookie alone would leave the SSO session
    # live and the next sign-in would sail through without a prompt.
    assert auth_app.state.fake_oidc.revoked == ["refresh-1"]

    replayed = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert replayed.status_code == 401


async def test_a_password_sign_out_sends_the_browser_nowhere(http: AsyncClient) -> None:
    """The regression: a password sign-out used to be routed via Keycloak.

    The browser never visited the realm to obtain this session — the grant was
    exchanged server to server — so no cookie was set on the realm's origin and there
    is nothing there to clear. Returning a URL anyway cost two full document loads,
    out and back, to reach a screen the SPA already had.

    The tokens are still revoked at the realm; that part is server to server too.
    """
    signed_in = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": CORRECT_PASSWORD},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    cookie = signed_in.cookies[settings.auth.cookie_name]

    response = await http.post(
        "/api/v1/auth/logout",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert response.json()["end_session_url"] is None


async def test_a_password_sign_out_still_revokes_at_the_realm(
    http: AsyncClient, auth_app: Any
) -> None:
    """No browser round trip is not the same as no revocation."""
    signed_in = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": CORRECT_PASSWORD},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    cookie = signed_in.cookies[settings.auth.cookie_name]

    await http.post(
        "/api/v1/auth/logout",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert auth_app.state.fake_oidc.revoked == ["refresh-1"]


async def test_an_sso_sign_out_does_get_an_end_session_url(http: AsyncClient) -> None:
    """The case the round trip exists for.

    An SSO sign-in leaves a cookie on the realm's own origin, and only a browser
    navigation can clear it — so this one keeps its URL.
    """
    cookie = await _sign_in(http)

    response = await http.post(
        "/api/v1/auth/logout",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert "openid-connect/logout" in response.json()["end_session_url"]


async def test_logout_without_a_session_still_succeeds(http: AsyncClient) -> None:
    """Never fails. Someone told they are still signed in on a shared machine is
    the one outcome worth designing against."""
    response = await http.post(
        "/api/v1/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"}
    )

    assert response.status_code == 200


# --- What is no longer accepted ----------------------------------------------


async def test_an_unsigned_development_token_is_refused(client: AsyncClient) -> None:
    """The bypass this phase removed.

    The frontend used to mint `dev.<base64url(json)>` in the browser, choosing its
    own roles, and the API accepted it whenever a flag was set. There is now no
    configuration in which this works.
    """
    forged = "dev.eyJzdWIiOiAiYXR0YWNrZXIiLCAicm9sZXMiOiBbImNsYWltcy1hZG1pbiJdfQ"

    response = await client.get(
        "/api/v1/meta/whoami", headers={"Authorization": f"Bearer {forged}"}
    )

    assert response.status_code == 401


async def test_dev_auth_is_no_longer_a_setting() -> None:
    assert not hasattr(settings, "dev_auth")
    assert not hasattr(settings, "dev_auth_enabled")


async def test_security_headers_are_present(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    # XSS is the attack the in-memory access token is defended against; a CSP is
    # what reduces the chance of it landing at all.
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


# --- Signing in with a password ----------------------------------------------


async def test_a_correct_password_opens_a_session(http: AsyncClient) -> None:
    response = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": CORRECT_PASSWORD},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "access-1"
    assert body["subject"] == "sub-1"
    assert body["roles"] == ["claims-handler"]

    # The same cookie the redirect flow sets, so everything downstream is unaware
    # of which door the person came through.
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Path=/api/v1/auth" in header
    # The refresh token must never be in a response the browser can read.
    assert "refresh-1" not in response.text
    assert "refresh-1" not in header


async def test_the_session_from_a_password_is_usable_for_refresh(http: AsyncClient) -> None:
    """The two doors have to leave the session in the same state.

    If a password sign-in produced a grant the refresh path could not rotate, the
    session would die at the first token renewal rather than at its idle timeout.
    """
    signed_in = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": CORRECT_PASSWORD},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    cookie = signed_in.cookies[settings.auth.cookie_name]

    refreshed = await http.post(
        "/api/v1/auth/token",
        cookies={settings.auth.cookie_name: cookie},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert refreshed.status_code == 200
    assert refreshed.cookies[settings.auth.cookie_name] != cookie


async def test_a_wrong_password_says_nothing_about_which_half_was_wrong(
    http: AsyncClient,
) -> None:
    """No username enumeration.

    Keycloak answers `invalid_grant` for a wrong password, an unknown user and a
    disabled account alike. Passing its wording through would tell an attacker
    which addresses name real accounts, so exactly one sentence comes back.
    """
    response = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": "wrong"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 401
    message = response.json()["error"]["message"]
    assert message == "That email and password do not match."
    # The realm's own wording must not leak through.
    assert "Invalid user credentials" not in response.text


async def test_a_locked_account_is_told_so(http: AsyncClient, auth_app: Any) -> None:
    """The one refusal worth naming.

    Somebody whose correct password has stopped working needs to know why, or they
    keep trying and extend the lockout. An attacker learns nothing they could not
    infer by trying.
    """
    auth_app.state.fake_oidc.refusal = "Account is temporarily disabled, contact your admin"

    response = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": "wrong"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 401
    assert "temporarily locked" in response.json()["error"]["message"]


async def test_repeated_failures_are_throttled_before_reaching_the_realm(
    http: AsyncClient, auth_app: Any
) -> None:
    """The counter our own front door needs.

    Keycloak locks a single account after its own threshold, but is blind to one
    source trying many accounts once each. Over the limit is refused *before* the
    password is forwarded, which is what `password_attempts` proves.
    """
    limit = settings.auth.login_max_attempts
    for _ in range(limit):
        await http.post(
            "/api/v1/auth/login",
            json={"email": "demo@carrier.example", "password": "wrong"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    attempts_before = len(auth_app.state.fake_oidc.password_attempts)
    response = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": "wrong"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.json()["error"]["details"]["retry_after_seconds"] > 0
    assert len(auth_app.state.fake_oidc.password_attempts) == attempts_before


async def test_a_success_clears_the_failure_budget(http: AsyncClient) -> None:
    """Four typos then the right password must not leave a spent budget behind."""
    for _ in range(4):
        await http.post(
            "/api/v1/auth/login",
            json={"email": "demo@carrier.example", "password": "wrong"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    ok = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": CORRECT_PASSWORD},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert ok.status_code == 200

    # Budget reset: another wrong attempt is refused on its merits, not throttled.
    again = await http.post(
        "/api/v1/auth/login",
        json={"email": "demo@carrier.example", "password": "wrong"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert again.status_code == 401


async def test_a_malformed_login_body_does_not_echo_the_password(http: AsyncClient) -> None:
    """The leak this phase fixed.

    Pydantic puts the offending value in `input`, and the validation handler used to
    pass every key but `ctx` and `url` straight through. With a password in the body
    that returned the password in the 422 — and from there into any client log.
    """
    response = await http.post(
        "/api/v1/auth/login",
        json={"email": 12345, "password": "hunter2hunter2"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 422
    assert "hunter2hunter2" not in response.text


async def test_the_sso_entry_point_moved_but_still_redirects(http: AsyncClient) -> None:
    """`GET /auth/login` became `GET /auth/sso`.

    The password grant took the `/login` name, and one path answering two verbs with
    two entirely different flows is the kind of thing that reads fine until someone
    calls the wrong one.
    """
    moved = await http.get("/api/v1/auth/login")
    assert moved.status_code == 405

    response = await http.get("/api/v1/auth/sso")
    assert response.status_code == 307
    assert "code_challenge_method=S256" in response.headers["location"]


async def test_the_end_session_url_returns_to_sign_in_not_the_landing_page() -> None:
    """Where a signed-out browser lands.

    The marketing root would make somebody who has just pressed "sign out" go
    looking for the way back in — and they are at a keyboard, about to sign in again
    or to hand the machine over. The URI has to be covered by the client's registered
    post-logout redirects, which the bootstrap script sets to the origin plus a
    wildcard.
    """
    from urllib.parse import parse_qs, urlparse

    from app.core.oidc import OIDCClient

    url = OIDCClient(settings).end_session_url()
    target = parse_qs(urlparse(url).query)["post_logout_redirect_uri"][0]

    assert target == settings.sign_in_url
    assert target.endswith("/sign-in")


async def test_the_sso_route_is_refused_while_the_flag_is_off(client: AsyncClient) -> None:
    """Disabled means disabled, not merely unlinked.

    The button is hidden by `/meta/config`, but a control whose URL still works is
    not an absent feature. Uses the plain `client` fixture, which does not enable the
    flag the way `auth_app` does.
    """
    response = await client.get("/api/v1/auth/sso")

    assert response.status_code == 404


async def test_meta_config_reports_sso_off(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/config")

    assert response.status_code == 200
    assert response.json()["auth"]["sso_enabled"] is False
