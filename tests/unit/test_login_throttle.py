"""Rate limiting on the sign-in endpoint.

`test_auth_session.py` covers the throttle through the route — that eleven attempts
answer 429, and that the eleventh never reaches the realm. This covers the mechanism
directly, because three of its properties are invisible from the outside and each is a
decision somebody could reasonably undo:

* the window is **fixed**, not sliding forward on every failure;
* the counters are keyed on a **hash**, so an email address is not a Redis key;
* a **success clears** the budget.

Why this exists at all: while sign-in was a redirect, Keycloak's login page absorbed
credential stuffing and its brute-force detector counted the failures. Moving the form
into this application made `POST /auth/login` the front door, and Keycloak still cannot
see the attack that never repeats a username — ten thousand accounts tried once each
trips no per-user counter anywhere.
"""

from __future__ import annotations

import pytest

from app.core.config import AuthSettings
from app.services.auth.throttle import LoginThrottle
from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.anyio

EMAIL = "r.marsh@carrier.com"
IP = "203.0.113.7"


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.services.auth.throttle.get_redis", lambda: fake)
    return fake


@pytest.fixture
def throttle() -> LoginThrottle:
    return LoginThrottle(AuthSettings(login_max_attempts=3, login_window_seconds=300))


async def test_a_first_attempt_is_allowed(redis: FakeRedis, throttle: LoginThrottle) -> None:
    assert (await throttle.check(ip=IP, email=EMAIL)).allowed is True


async def test_checking_does_not_count(redis: FakeRedis, throttle: LoginThrottle) -> None:
    """Only failures count, so signing in and out repeatedly is never throttled.

    If `check` incremented, a person opening the application ten times would lock
    themselves out without ever getting a password wrong.
    """
    for _ in range(10):
        await throttle.check(ip=IP, email=EMAIL)

    assert (await throttle.check(ip=IP, email=EMAIL)).allowed is True


async def test_the_limit_is_reached_by_failures(redis: FakeRedis, throttle: LoginThrottle) -> None:
    for _ in range(3):
        await throttle.record_failure(ip=IP, email=EMAIL)

    verdict = await throttle.check(ip=IP, email=EMAIL)

    assert verdict.allowed is False
    # Sent as Retry-After, so a client that wants to behave is told how long.
    assert verdict.retry_after > 0


async def test_one_under_the_limit_is_still_allowed(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """An off-by-one here either locks someone out early or gives a free attempt."""
    for _ in range(2):
        await throttle.record_failure(ip=IP, email=EMAIL)

    assert (await throttle.check(ip=IP, email=EMAIL)).allowed is True


async def test_a_success_clears_the_budget(redis: FakeRedis, throttle: LoginThrottle) -> None:
    """Four typos then the right password must not leave a nearly-spent budget.

    They have proved the session is theirs; carrying the failures forward would punish
    them for the rest of the window.
    """
    for _ in range(3):
        await throttle.record_failure(ip=IP, email=EMAIL)
    assert (await throttle.check(ip=IP, email=EMAIL)).allowed is False

    await throttle.clear(ip=IP, email=EMAIL)

    assert (await throttle.check(ip=IP, email=EMAIL)).allowed is True


async def test_the_email_counter_follows_the_account_across_addresses(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """Many sources hammering one account.

    Keycloak would eventually lock that account, which is the attacker's goal if it
    belongs to somebody who needs to work today. Refusing here — without locking —
    is the kinder failure.
    """
    for index in range(3):
        await throttle.record_failure(ip=f"198.51.100.{index}", email=EMAIL)

    assert (await throttle.check(ip="198.51.100.99", email=EMAIL)).allowed is False


async def test_the_ip_counter_follows_the_source_across_accounts(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """One source spraying many accounts — the attack Keycloak cannot see.

    No per-user counter anywhere trips, because no username repeats.
    """
    for index in range(3):
        await throttle.record_failure(ip=IP, email=f"person{index}@carrier.com")

    assert (await throttle.check(ip=IP, email="someone-else@carrier.com")).allowed is False


async def test_another_source_is_unaffected_by_a_blocked_one(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """The counters must not become a way to lock the whole desk out.

    A shared office NAT would otherwise let one person's typos block their colleagues.
    """
    for index in range(3):
        await throttle.record_failure(ip=IP, email=f"person{index}@carrier.com")

    assert (await throttle.check(ip="203.0.113.250", email="fresh@carrier.com")).allowed is True


async def test_the_email_is_matched_case_and_space_insensitively(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """Otherwise the counter is bypassed by holding down shift."""
    for _ in range(3):
        await throttle.record_failure(ip=IP, email=EMAIL)

    assert (await throttle.check(ip=IP, email=f"  {EMAIL.upper()}  ")).allowed is False


async def test_the_window_is_fixed_rather_than_sliding(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """The TTL is set once, on the first failure, and not pushed forward after.

    A sliding window would let a steady trickle of attempts keep the key alive for
    ever, and the person would never be let back in.
    """
    await throttle.record_failure(ip=IP, email=EMAIL)
    keys = [key for key in redis.ttls if key.startswith("auth:login-attempts:")]
    assert keys, "the first failure should have set a TTL"
    first = {key: redis.ttls[key] for key in keys}

    # A later failure raises the count but must not re-arm the expiry.
    redis.ttls[keys[0]] = 42
    await throttle.record_failure(ip=IP, email=EMAIL)

    assert redis.ttls[keys[0]] == 42
    assert all(value == 300 for key, value in first.items() if key != keys[0])


async def test_no_address_still_counts_the_email(redis: FakeRedis, throttle: LoginThrottle) -> None:
    """A caller with no resolvable IP must not get an unlimited budget.

    `request.client` is `None` for some ASGI transports, and a missing address is not
    a reason to stop counting the thing that is present.
    """
    for _ in range(3):
        await throttle.record_failure(ip=None, email=EMAIL)

    assert (await throttle.check(ip=None, email=EMAIL)).allowed is False


async def test_neither_the_email_nor_the_address_appears_in_a_key(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """A key name ends up in MONITOR output, a KEYS scan and any screenshot.

    The counter only ever needs equality, so storing the plaintext buys nothing and
    puts personal data somewhere nobody thinks to look for it.
    """
    await throttle.record_failure(ip=IP, email=EMAIL)

    written = " ".join(redis.values)
    assert EMAIL not in written
    assert "r.marsh" not in written
    assert IP not in written


async def test_a_retry_after_of_zero_is_never_reported(
    redis: FakeRedis, throttle: LoginThrottle
) -> None:
    """Redis answers -2 for a key that has already expired.

    Passing that through as `Retry-After: -2` would be worse than useless, so the
    window length stands in.
    """
    for _ in range(3):
        await throttle.record_failure(ip=IP, email=EMAIL)
    # Drop the recorded TTLs, leaving the counters — which is the state a key rolling
    # between the increment and the read produces.
    redis.ttls.clear()

    verdict = await throttle.check(ip=IP, email=EMAIL)

    assert verdict.allowed is False
    assert verdict.retry_after == 300
