"""get_db teardown invalidates a poisoned connection instead of leaking it (tr-3sj).

If session.close() races an in-flight operation on the asyncpg connection it
raises "another operation is in progress" mid-rollback; the original `async with`
let that connection leak checked-out with an open transaction holding row locks,
which wedged every later request that needed those rows (the tr-dus incident).
_close_db_session must instead invalidate (discard) the connection on any close
failure, while a clean close must NOT invalidate.
"""
import asyncpg
import pytest

from app.dependencies import _close_db_session


class _FakeSession:
    def __init__(self, close_error: BaseException | None = None) -> None:
        self._close_error = close_error
        self.closed = False
        self.invalidated = False

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    async def invalidate(self) -> None:
        self.invalidated = True


async def test_invalidates_when_close_hits_operation_in_progress() -> None:
    session = _FakeSession(
        close_error=asyncpg.InterfaceError(
            "cannot perform operation: another operation is in progress"
        )
    )
    with pytest.raises(asyncpg.InterfaceError):
        await _close_db_session(session)  # type: ignore[arg-type]
    # Poisoned connection must be DISCARDED, not returned to the pool.
    assert session.invalidated is True


async def test_invalidates_when_close_is_cancelled() -> None:
    session = _FakeSession(close_error=asyncio_cancelled())
    with pytest.raises(BaseException):
        await _close_db_session(session)  # type: ignore[arg-type]
    assert session.invalidated is True


async def test_clean_close_does_not_invalidate() -> None:
    session = _FakeSession()
    await _close_db_session(session)  # type: ignore[arg-type]
    assert session.closed is True
    assert session.invalidated is False


def asyncio_cancelled() -> BaseException:
    import asyncio

    return asyncio.CancelledError()
