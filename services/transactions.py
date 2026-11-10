"""Small helper for running repository work in a retried transaction."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


async def run_db_operation(
    db_manager: Any,
    operation: Callable[[Any], Awaitable[T]],
    *,
    retry_on_integrity_error: bool = False,
    isolation_level: str | None = None,
) -> T:
    """Run ``operation(session)`` in its own transaction.

    Uses ``DatabaseManager.run_in_transaction`` (CockroachDB 40001 retries, and
    unique-violation retries for read-then-insert upserts) when available, so
    lightweight test doubles that only expose ``session()`` keep working.
    ``isolation_level`` (for example ``"SERIALIZABLE"``) is forwarded only when set.
    """
    runner = getattr(db_manager, "run_in_transaction", None)
    if callable(runner):
        options: dict[str, Any] = {"retry_on_integrity_error": retry_on_integrity_error}
        if isolation_level is not None:
            options["isolation_level"] = isolation_level
        return await runner(operation, **options)
    async with db_manager.session() as session:
        return await operation(session)
