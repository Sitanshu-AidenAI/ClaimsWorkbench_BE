"""Sign-in, sign-out, and the silent refresh in between.

Four routes, and the split between them is the security model:

* `/auth/login` and `/auth/callback` are the OIDC handshake. The browser is
  redirected to Keycloak and comes back with a code; we redeem it and keep the
  refresh token. Keycloak owns the password, the lockout policy and any MFA, so
  none of that is implemented here and none of it can be got wrong here.
* `/auth/token` is what the SPA calls on load and shortly before expiry. It reads
  the `HttpOnly` cookie, rotates the grant, and answers with a short-lived access
  token that the SPA holds **in memory only**.
* `/auth/logout` destroys the grant on our side and hands back the realm's
  end-session URL so the SSO session dies too. Clearing a cookie is not signing
  out if the next login sails straight through Keycloak without a prompt.

Every *other* authenticated route in this API is plain bearer and never sees a
cookie, which is why `app/api/deps/auth.py` needs no cookie branch and why CSRF
is a concern for exactly the two POSTs below.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlencode, urlparse

import jwt
from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.db import SessionDep
from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    error_response,
)
from app.core.logging import get_logger
from app.core.oidc import OIDCClient, PkcePair, TokenResponse, get_oidc_client
from app.core.security import Principal, principal_from_claims, verifier
from app.repositories.handler import HandlerRepository
from app.services.access.service import AccessService
from app.services.auth.store import Grant, GrantReuseError, RefreshGrantStore
from app.services.auth.throttle import LoginThrottle
from app.services.handlers import HandlerDirectoryService

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: The handshake cookie is three values that must survive one redirect and then
#: never again. Joined rather than given three cookies, because they are one fact
#: and expire together.
_TX_SEPARATOR = "."


class Credentials(BaseModel):
    """The sign-in body.

    `EmailStr` is deliberately not used: the realm is the only authority on whether
    an address names an account, and a stricter pattern here would reject a real
    unusual address while telling an attacker which addresses are well-formed.
    Length bounds only, so an oversized body is refused before it reaches Keycloak.

    A validation failure on this model must never echo what was submitted — see the
    `input` note in `app.core.errors._serialisable_errors`, which is what stops the
    password appearing in a 422.
    """

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    """What the SPA runs on. Deliberately not a token *and* a durable credential."""

    access_token: str
    #: Seconds. The SPA schedules its next refresh from this rather than decoding
    #: the token, so nothing in the browser has to parse a JWT to work.
    expires_in: int
    subject: str
    username: str | None = None
    email: str | None = None
    full_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    #: What these roles may reach, resolved from the access matrix at mint time.
    #:
    #: Sent with the session rather than fetched separately so the rail can render on
    #: first paint without a second round trip. It means a matrix change reaches an
    #: open tab on its next token refresh — up to the access-token lifetime — while
    #: the API enforces the new answer immediately. The window fails closed: a
    #: revoked item stays visible briefly and 403s if clicked.
    capabilities: list[str] = Field(default_factory=list)


class LogoutResponse(BaseModel):
    #: Where to send the browser to end the *realm's* session, or `null` when there
    #: is none to end.
    #:
    #: Returned rather than answered with a 302, because the caller is `fetch` and a
    #: redirect there would be followed invisibly instead of navigating the page.
    #:
    #: `null` for a password sign-in, and that is the point: the browser never visited
    #: Keycloak, so no cookie was set on its origin and there is nothing there to
    #: clear. Sending it anyway cost two full document loads — out to the realm and
    #: back — which is the page reload somebody noticed on the way to the sign-in
    #: screen. With this null the SPA routes there itself and nothing reloads.
    end_session_url: str | None = None


def _store() -> RefreshGrantStore:
    """Overridable in tests via `app.dependency_overrides`."""
    return RefreshGrantStore()


def _client() -> OIDCClient:
    return get_oidc_client()


def _throttle() -> LoginThrottle:
    """Overridable in tests via `app.dependency_overrides`."""
    return LoginThrottle()


StoreDep = Annotated[RefreshGrantStore, Depends(_store)]
ThrottleDep = Annotated[LoginThrottle, Depends(_throttle)]
ClientDep = Annotated[OIDCClient, Depends(_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --- The handshake -----------------------------------------------------------


@router.get("/sso", summary="Begin single sign-on against Keycloak")
async def sso(
    request: Request,
    client: ClientDep,
    config: SettingsDep,
    return_to: Annotated[str | None, Query(description="Path to land on afterwards")] = None,
) -> RedirectResponse:
    """Redirect to Keycloak, remembering enough to finish when it comes back."""
    if not config.auth.sso_enabled:
        # Refused rather than merely unlinked. Hiding the button while leaving the
        # URL live would mean the feature is off on the screen and on in the API,
        # which is the sort of gap nobody finds until it matters.
        raise NotFoundError("Single sign-on is not enabled on this deployment.")

    if not client.configured:
        # Said plainly and early. The alternative is a redirect that works, a
        # callback that fails at the token endpoint, and an operator reading
        # Keycloak logs to discover a missing environment variable.
        raise AuthenticationError(
            "Sign-in is not configured: CWB_KEYCLOAK_CLIENT_SECRET is not set."
        )

    pkce = PkcePair.generate()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    response = RedirectResponse(
        client.authorization_url(state=state, nonce=nonce, challenge=pkce.challenge),
        status_code=307,
    )
    _set_transaction_cookie(
        response,
        config,
        state=state,
        nonce=nonce,
        verifier=pkce.verifier,
        return_to=_safe_return_to(return_to),
    )
    logger.info("auth_login_started", host=request.url.hostname)
    return response


@router.get("/callback", summary="Finish sign-in and store the refresh grant")
async def callback(
    response: Response,
    client: ClientDep,
    store: StoreDep,
    config: SettingsDep,
    session: SessionDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    cwb_oidc_tx: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Redeem the code, keep the refresh token, hand the browser back to the SPA."""
    if error:
        # The person cancelled, or the realm refused. Not our error to raise as a
        # 500 — send them back to the sign-in screen with the reason.
        logger.info("auth_callback_provider_error", error=error)
        return _to_frontend(config, config.auth.sign_in_path, {"error": error_description or error})

    transaction = _read_transaction_cookie(cwb_oidc_tx)
    if transaction is None:
        raise AuthenticationError("The sign-in attempt expired. Please start again.")
    if not code or not state or not secrets.compare_digest(state, transaction["state"]):
        # Constant-time, and a mismatch is the CSRF case this parameter exists for.
        raise AuthenticationError("The sign-in response did not match the request.")

    tokens = await client.exchange_code(code=code, verifier=transaction["verifier"])
    claims = verifier.decode(tokens.access_token)
    _check_nonce(tokens, expected=transaction["nonce"])

    if tokens.refresh_token is None:
        raise AuthenticationError("The identity provider issued no refresh token.")

    principal = principal_from_claims(claims, client_id=config.keycloak.client_id)
    grant = await store.create(
        refresh_token=tokens.refresh_token,
        subject=principal.subject,
        id_token=tokens.id_token,
        via="sso",
    )

    # The redirect flow hands the browser a cookie rather than a session payload, so
    # it never reaches `_session_response`. Without this an SSO user would sign in
    # for months and never appear in the directory a manager assigns from.
    await _register_handler(session, principal)

    redirect = _to_frontend(config, transaction["return_to"] or "/claims", None)
    _set_grant_cookie(redirect, config, grant.handle)
    _clear_transaction_cookie(redirect, config)
    logger.info("auth_signed_in", subject=principal.subject)
    return redirect


# --- The session -------------------------------------------------------------


@router.post("/token", response_model=SessionResponse, summary="Mint an access token")
async def token(
    request: Request,
    response: Response,
    store: StoreDep,
    client: ClientDep,
    config: SettingsDep,
    session: SessionDep,
    cwb_refresh: Annotated[str | None, Cookie()] = None,
) -> SessionResponse | JSONResponse:
    """Exchange the refresh cookie for a short-lived access token.

    Called on page load and shortly before expiry, which is what lets the SPA hold
    its access token in memory: a reload does not restore a persisted credential,
    it asks for a new one.
    """
    _require_same_origin(request, config)

    if not cwb_refresh:
        raise AuthenticationError("No session. Sign in again.")

    try:
        grant = await store.get(cwb_refresh)
    except GrantReuseError as exc:
        # The family is already destroyed by the store. The cookie goes too, so the
        # browser stops presenting a credential that now revokes sessions.
        #
        # Returned rather than raised, here and below: a raised error is rendered by
        # the registered handler, which builds its own response and discards every
        # header this route set — including the one clearing the cookie.
        return _refused(config, str(exc))

    if grant is None:
        return _refused(config, "Your session has expired. Sign in again.")

    try:
        tokens = await client.refresh(grant.refresh_token)
    except AuthenticationError as exc:
        # The realm has ended the session — a password change, an admin logout, or
        # the refresh token simply aged out. Our grant is worth nothing now.
        await store.destroy_family(grant.family)
        return _refused(config, str(exc))

    rotated = await store.rotate(grant, refresh_token=tokens.refresh_token or grant.refresh_token)
    _set_grant_cookie(response, config, rotated.handle)

    return await _session_response(tokens, config, session)


@router.post("/logout", response_model=LogoutResponse, summary="End the session")
async def logout(
    request: Request,
    response: Response,
    store: StoreDep,
    client: ClientDep,
    config: SettingsDep,
    cwb_refresh: Annotated[str | None, Cookie()] = None,
) -> LogoutResponse:
    """Destroy the grant here, and say whether the realm needs a visit too.

    Never fails. A sign-out that could 500 would leave someone believing they are
    still signed in on a shared machine, which is the one outcome worth designing
    against.
    """
    _require_same_origin(request, config)

    end_session_url: str | None = None
    if cwb_refresh:
        grant = await _grant_for_logout(store, cwb_refresh)
        if grant is not None:
            await store.destroy_family(grant.family)
            # Revoked server to server either way. For a password session this is the
            # whole of ending it at the realm; for an SSO session it ends the tokens
            # but not the browser cookie, which is why that one also gets a URL.
            await client.revoke(grant.refresh_token)
            if grant.via == "sso":
                end_session_url = client.end_session_url(id_token=grant.id_token)
            logger.info("auth_signed_out", subject=grant.subject, via=grant.via)

    _clear_grant_cookie(response, config)
    return LogoutResponse(end_session_url=end_session_url)


async def _grant_for_logout(store: RefreshGrantStore, handle: str) -> Grant | None:
    """The grant, or nothing — a reused handle on the way out is not interesting.

    Reuse detection has already destroyed the family by the time the exception
    reaches here, which is exactly what logout wanted to do anyway.
    """
    try:
        return await store.get(handle)
    except GrantReuseError:
        return None


async def _session_response(
    tokens: TokenResponse,
    config: Settings,
    session: AsyncSession,
    *,
    register: bool = False,
) -> SessionResponse:
    """The access token, who it belongs to, and what they may reach.

    Shared by `/auth/login` and `/auth/token` deliberately: the browser must not be
    able to tell which grant produced its session, or the two paths would drift and
    only one of them would stay tested.

    The capability list is resolved here rather than by the client, so the rail draws
    from the same answer `require_capability` enforces on.

    `register` is off by default, and that default is the point: this function serves
    `/auth/token` as well as `/auth/login`, and a browser renews its token every few
    minutes. Writing to the handler directory on every renewal would be a lookup and
    a commit per user per few minutes to record something that changes when a person
    is renamed. Sign-in is the moment; refreshing a token is not a sign-in.
    """
    claims = verifier.decode(tokens.access_token)
    principal = principal_from_claims(claims, client_id=config.keycloak.client_id)
    held = await AccessService(session).capabilities_for_roles(principal.roles)

    if register:
        await _register_handler(session, principal)

    return SessionResponse(
        access_token=tokens.access_token,
        # Shortened by the margin so the SPA renews before the token is actually
        # dead, rather than discovering it on a 401 mid-action.
        expires_in=max(tokens.expires_in - config.auth.refresh_margin_seconds, 1),
        subject=principal.subject,
        username=principal.username,
        email=principal.email,
        full_name=principal.full_name,
        roles=sorted(principal.roles),
        capabilities=sorted(c.value for c in held),
    )


async def _register_handler(session: AsyncSession, principal: Principal) -> None:
    """Make sure a signed-in handler exists in the directory a manager assigns from.

    Called from the two paths that establish a session — the password grant and the
    SSO callback — and from neither refresh.

    Here rather than from a nightly synchronisation because the token in hand is the
    best source of this person's subject, name and address that exists: it is what
    the identity provider has just asserted about them. Reading the realm's user
    list instead would need an administrative grant on Keycloak that this service
    account does not hold and should not need.

    **A failure here must not cost somebody their sign-in.** Being absent from the
    assignment dialog is an inconvenience a manager can work around; not being able
    to log in is not. So the write is committed on its own and a failure is logged
    and swallowed, which is the one place in this codebase where that is the right
    trade.
    """
    try:
        await HandlerDirectoryService(HandlerRepository(session)).register(principal)
        await session.commit()
    except Exception:  # Deliberately broad — see the docstring: sign-in outranks this.
        logger.warning("handler_registration_failed", subject=principal.subject, exc_info=True)
        # Guarded in turn. A block whose whole purpose is that nothing here can cost
        # somebody their sign-in must not raise out of its own recovery.
        try:
            await session.rollback()
        except Exception:  # Deliberately broad, for the same reason.
            logger.warning("handler_registration_rollback_failed", exc_info=True)


# --- Sign in with a password -------------------------------------------------


@router.post("/login", response_model=SessionResponse, summary="Sign in with a password")
async def login(
    request: Request,
    response: Response,
    credentials: Credentials,
    store: StoreDep,
    client: ClientDep,
    config: SettingsDep,
    throttle: ThrottleDep,
    session: SessionDep,
) -> SessionResponse:
    """Exchange an email and password with the realm, and open a session.

    The credential is verified by Keycloak against its own database; nothing here
    reads a local user row, and there is no local user table to read. What comes
    back is the same access token the redirect flow produces, stored the same way —
    so every route above this one is unaware of which door the person came through.
    """
    if not client.configured:
        raise AuthenticationError(
            "Sign-in is not configured: CWB_KEYCLOAK_CLIENT_SECRET is not set."
        )

    email = credentials.email.strip().lower()
    ip = _client_ip(request)

    verdict = await throttle.check(ip=ip, email=email)
    if not verdict.allowed:
        # 429 rather than 401, and with the wait: a client that wants to behave is
        # told how long, and one that does not is refused before the password ever
        # reaches Keycloak.
        raise RateLimitedError(
            "Too many sign-in attempts. Wait a few minutes and try again.",
            details={"retry_after_seconds": verdict.retry_after},
        )

    try:
        tokens = await client.password_grant(username=email, password=credentials.password)
    except AuthenticationError as exc:
        await throttle.record_failure(ip=ip, email=email)
        # The reason goes to the log, not to the caller. Keycloak answers
        # `invalid_grant` for a wrong password, a disabled account and an expired
        # one alike, and telling them apart out loud turns this endpoint into a
        # username-enumeration oracle. `_signin_refusal` decides the one exception.
        logger.info("auth_password_grant_refused", email=email, detail=str(exc))
        raise _signin_refusal(exc) from exc

    if tokens.refresh_token is None:
        raise AuthenticationError("The identity provider issued no refresh token.")

    claims = verifier.decode(tokens.access_token)
    principal = principal_from_claims(claims, client_id=config.keycloak.client_id)
    grant = await store.create(
        refresh_token=tokens.refresh_token,
        subject=principal.subject,
        id_token=tokens.id_token,
        via="password",
    )
    _set_grant_cookie(response, config, grant.handle)
    # Cleared on success, so four typos followed by the right password does not
    # leave a nearly-spent budget on a session already proved to be theirs.
    await throttle.clear(ip=ip, email=email)

    logger.info("auth_signed_in", subject=principal.subject, method="password")
    return await _session_response(tokens, config, session, register=True)


def _signin_refusal(exc: AuthenticationError) -> AuthenticationError:
    """One sentence for the person, chosen from what the realm actually said.

    Lockout is surfaced and nothing else is. An attacker learns nothing from being
    told that repeated attempts have been throttled — they could infer it by
    trying — whereas somebody whose correct password suddenly stops working needs
    to know why, or they will keep trying and extend the lockout.
    """
    detail = str(exc).lower()
    if "temporarily disabled" in detail or "too many" in detail or "brute" in detail:
        return AuthenticationError(
            "This account is temporarily locked after too many failed attempts. "
            "Wait a few minutes, or ask an administrator to unlock it."
        )
    return AuthenticationError("That email and password do not match.")


def _client_ip(request: Request) -> str | None:
    """The caller's address, honouring one proxy hop.

    `X-Forwarded-For` is trusted only for its first entry, and only because this
    service is expected to sit behind a reverse proxy that sets it. A client can
    forge the header, which is why it feeds a rate-limit counter and nothing that
    makes an authorisation decision.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


# --- CSRF, for the two routes that read a cookie -----------------------------


def _require_same_origin(request: Request, config: Settings) -> None:
    """Refuse a cross-site caller on the cookie-bearing endpoints.

    Two checks, and both are needed. The custom header cannot be set by a form or
    an image tag, which rules out the classic CSRF shapes; the origin check rules
    out a page that can set headers because it is scripted, but is not us. Neither
    route is GET-navigable, so there is nothing else to protect.

    A full CSRF token would be the answer if these were ordinary authenticated
    writes. They are not: they are the only two routes in the API that read a
    cookie at all, and every other write is bearer-authenticated and therefore not
    forgeable cross-site in the first place.
    """
    if not request.headers.get(config.auth.required_header):
        raise PermissionDeniedError(
            f"This endpoint requires the {config.auth.required_header} header."
        )

    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin is None:
        # No Origin at all is a same-origin non-browser caller (curl, a test).
        # Browsers always send one on a cross-origin request, which is the case
        # being excluded here.
        return

    if _origin_of(origin) not in {_origin_of(allowed) for allowed in config.cors_origins}:
        logger.warning("auth_cross_origin_refused", origin=_origin_of(origin))
        raise PermissionDeniedError("This origin may not use the session endpoints.")


def _origin_of(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else value.rstrip("/")


# --- Cookies -----------------------------------------------------------------


def _set_grant_cookie(response: Response, config: Settings, handle: str) -> None:
    response.set_cookie(
        config.auth.cookie_name,
        handle,
        max_age=config.auth.absolute_timeout_seconds,
        httponly=True,
        secure=config.cookies_secure,
        samesite=config.auth.cookie_samesite,
        # Scoped to the auth routes, so the credential is not attached to a single
        # other request in the API — including the ones that serve documents.
        path=config.auth.cookie_path,
        domain=config.auth.cookie_domain,
    )


def _refused(config: Settings, message: str) -> JSONResponse:
    """A 401 in the standard envelope, with the session cookie cleared."""
    refusal = error_response(401, "unauthenticated", message)
    _clear_grant_cookie(refusal, config)
    return refusal


def _clear_grant_cookie(response: Response, config: Settings) -> None:
    response.delete_cookie(
        config.auth.cookie_name,
        path=config.auth.cookie_path,
        domain=config.auth.cookie_domain,
        httponly=True,
        secure=config.cookies_secure,
        samesite=config.auth.cookie_samesite,
    )


def _set_transaction_cookie(
    response: Response,
    config: Settings,
    *,
    state: str,
    nonce: str,
    verifier: str,
    return_to: str,
) -> None:
    response.set_cookie(
        config.auth.transaction_cookie_name,
        _TX_SEPARATOR.join((state, nonce, verifier, return_to)),
        max_age=config.auth.transaction_ttl_seconds,
        httponly=True,
        secure=config.cookies_secure,
        samesite="lax",
        # Root path, unlike the grant cookie: the callback arrives at
        # /api/v1/auth/callback but the browser must carry this from wherever the
        # login link was clicked.
        path="/",
        domain=config.auth.cookie_domain,
    )


def _clear_transaction_cookie(response: Response, config: Settings) -> None:
    response.delete_cookie(
        config.auth.transaction_cookie_name,
        path="/",
        domain=config.auth.cookie_domain,
    )


def _read_transaction_cookie(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    parts = raw.split(_TX_SEPARATOR)
    if len(parts) != 4:
        return None
    state, nonce, verifier_value, return_to = parts
    if not state or not nonce or not verifier_value:
        return None
    return {"state": state, "nonce": nonce, "verifier": verifier_value, "return_to": return_to}


# --- Helpers -----------------------------------------------------------------


def _check_nonce(tokens: TokenResponse, *, expected: str) -> None:
    """Confirm the id token answers the request we actually made.

    Verified with signature checking off and for one claim only, because the
    signature has already been checked on the *access* token from the same
    response by the same realm key. What is being established here is replay: that
    this id token belongs to this handshake rather than an earlier one.
    """
    if tokens.id_token is None:
        return

    try:
        claims = jwt.decode(tokens.id_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise AuthenticationError("The identity token could not be read.") from None

    nonce = claims.get("nonce")
    if nonce is not None and not secrets.compare_digest(str(nonce), expected):
        raise AuthenticationError("The identity token did not match the sign-in request.")


def _safe_return_to(value: str | None) -> str:
    """Only ever a path on our own frontend.

    An open redirect on a sign-in route is how a phishing page borrows a real
    domain, so anything absolute, protocol-relative or otherwise not a plain path
    is discarded rather than sanitised.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return ""
    return value


def _to_frontend(config: Settings, path: str, query: dict[str, str] | None) -> RedirectResponse:
    base = config.auth.frontend_url.rstrip("/")
    target = f"{base}{path if path.startswith('/') else '/' + path}"
    if query:
        target = f"{target}?{urlencode(query)}"
    return RedirectResponse(target, status_code=303)


__all__ = ["router"]
