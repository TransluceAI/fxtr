"""fxtr: experiment orchestration over typed, content-addressed arrays."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fxtr.array_sql import sql

__all__ = ["sql"]


def __getattr__(name: str) -> Any:
    # ``fxtr.sql`` is loaded on first use: importing any ``fxtr.*`` module runs this file, and
    # ``array_sql`` pulls in the array, query, and experiment modules.
    if name == "sql":
        from fxtr.array_sql import sql

        return sql
    raise AttributeError(f"module 'fxtr' has no attribute {name!r}")


def __dir__() -> list[str]:
    # Listed for tab completion, which asks dir() and would not otherwise see a lazy name.
    return [*globals(), "sql"]
