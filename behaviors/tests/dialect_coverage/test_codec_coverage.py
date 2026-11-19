import functools
import importlib.metadata
import os

import pytest

from behaviors.types.dialect import Dialect

from .audit import Row, audit
from .dialects import AUDITS
from .schema import locked_version
from .table import describe_changes, load, render, table_path


@functools.cache
def _rows(dialect: Dialect) -> tuple[Row, ...]:
    return tuple(audit(AUDITS[dialect]()))


def _skip_unless_locked(sdk: str | None) -> None:
    if sdk is None:
        return  # a checked-in API document: the table pins its revision itself
    # The tables follow the lockfile's SDKs. An install that resolves others (CI's lowest-direct
    # job) reads a different schema, which says nothing about the tables; a locked install whose
    # table names another SDK means the table was not regenerated after a bump, and fails.
    installed = importlib.metadata.version(sdk)
    locked = locked_version(sdk)
    if locked is not None and installed != locked:
        pytest.skip(f"{sdk} {installed} installed; the lockfile, and the tables, pin {locked}")


@pytest.mark.parametrize("dialect", sorted(AUDITS))
def test_codec_coverage_has_no_unhandled_rows(dialect: Dialect) -> None:
    # Every installed SDK's schema is audited, including non-locked versions: every path is
    # modeled, kept as an extra, or a recorded loss. Only the snapshot comparison below depends
    # on the lockfile, and regenerating a table cannot accept an unhandled row.
    unhandled = [row for row in _rows(dialect) if row.verdict == "unhandled"]
    assert not unhandled, f"{len(unhandled)} unhandled {dialect} path(s):\n" + "\n".join(
        f"  {row.path}: {'; '.join(row.reasons)}" for row in unhandled[:20]
    )


@pytest.mark.parametrize("dialect", sorted(AUDITS))
def test_codec_coverage_matches_the_checked_in_table(dialect: Dialect) -> None:
    spec = AUDITS[dialect]()
    # Before regenerating too: a table is only ever written from the locked SDKs' schemas.
    _skip_unless_locked(spec.sdk)
    rows = list(_rows(dialect))
    if os.environ.get("BEHAVIORS_UPDATE_CODEC_COVERAGE"):
        table_path(spec.dialect).parent.mkdir(parents=True, exist_ok=True)
        table_path(spec.dialect).write_text(render(spec, rows))
        return
    expected = load(spec.dialect)
    assert expected is not None, f"no table for {dialect}; generate it (see its header)"
    changes = describe_changes(expected, spec, rows)
    assert not changes, (
        f"the {dialect} codec's coverage differs from {table_path(spec.dialect).name}; review "
        f"the change and regenerate the table:\n{changes}"
    )
