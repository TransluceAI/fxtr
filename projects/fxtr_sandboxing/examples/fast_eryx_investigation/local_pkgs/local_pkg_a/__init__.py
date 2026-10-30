"""Tiny mutable local package — used to verify per-spawn freshness."""

VERSION_TAG = "v1"


def hello() -> str:
    return f"local_pkg_a:{VERSION_TAG}"
