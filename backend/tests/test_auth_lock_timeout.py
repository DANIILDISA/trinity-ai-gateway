"""refresh_access_token fails fast on a row-lock timeout (tr-wvs).

In the tr-dus incident a wedged transaction held the refresh_tokens / users row
locks, so every /auth/refresh blocked on FOR UPDATE until the 30s request timeout
turned it into a 503. With a per-transaction lock_timeout the refresh aborts in
a few seconds with a clean, retryable ServiceUnavailableError instead of hanging.

This unit test drives refresh_access_token with a fake session whose locking
SELECT raises a simulated Postgres lock_timeout error (asyncpg
LockNotAvailableError) and asserts it is converted to ServiceUnavailableError
(retryable 503), not surfaced as a raw DB error (500).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.core.security import create_refresh_token
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.auth_service import _token_hash, refresh_access_token


class _LockTimeoutSession:
    """AsyncSession stand-in: SET LOCAL succeeds, the FOR UPDATE select then
    raises a wrapped Postgres lock_timeout error."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.set_local_seen = False

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "SET LOCAL" in sql or "lock_timeout" in sql:
            self.set_local_seen = True
            return None
        # The FOR UPDATE select waited past lock_timeout → Postgres 55P03.
        orig = asyncpg.exceptions.LockNotAvailableError(
            "canceling statement due to lock timeout"
        )
        raise OperationalError("SELECT ... FOR UPDATE", {}, orig)

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_refresh_lock_timeout_becomes_service_unavailable() -> None:
    session = _LockTimeoutSession()
    with pytest.raises(ServiceUnavailableError):
        await refresh_access_token(session, "any-raw-token")  # type: ignore[arg-type]
    # The failed transaction was rolled back so the connection isn't left dirty.
    assert session.rolled_back is True


async def test_refresh_lock_timeout_fast_fails_against_real_postgres(
    db_session: AsyncSession,
    engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with the token row locked by another transaction, a refresh
    aborts via SET LOCAL lock_timeout in well under the 30s request timeout and
    surfaces as ServiceUnavailableError (proves the timeout actually fires in
    Postgres, not just the error-mapping)."""
    monkeypatch.setattr(settings, "refresh_lock_timeout_ms", 250)

    user = User(
        email="locktest@example.com",
        display_name="Лок",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    raw = create_refresh_token()
    now = datetime.now(timezone.utc)
    db_session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_token_hash(raw),
            expires_at=now + timedelta(days=7),
            absolute_expires_at=now + timedelta(days=14),
        )
    )
    await db_session.commit()

    # Hold the token row's lock in a separate connection so the refresh blocks.
    locker = async_sessionmaker(engine, expire_on_commit=False)()
    await locker.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _token_hash(raw))
        .with_for_update()
    )
    try:
        with pytest.raises(ServiceUnavailableError):
            # wait_for bounds the test: success comes in ~250ms; a regression
            # (no lock_timeout) would hang and trip this instead of passing.
            await asyncio.wait_for(refresh_access_token(db_session, raw), timeout=10)
    finally:
        await locker.rollback()
        await locker.close()
        await db_session.rollback()
