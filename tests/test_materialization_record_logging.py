"""Unit tests for materialization-record logging modes.

These exercise `_save_materialization_record` directly (no live session) to validate:
- "per_file" mode (default): one `{timestamp}.json` pointer file per request.
- "jsonl" mode: cache-only requests are skipped entirely; requests that schedule new work
  append a self-describing summary line to `materializations.jsonl`; failed requests also get a
  per-request traceback dump under `materialization_failures/`.
"""

import json
import tempfile
from pathlib import Path
from typing import Generator, Literal

import cbrrr
import pytest
from frozendict import frozendict

from fxtr.api import fxtrconfig
from fxtr.api import session_manager as sm
from fxtr.object_model import core


def _make_config(active: Path, mode: Literal["per_file", "jsonl"]) -> fxtrconfig.FxtrConfig:
    """Build a minimal FxtrConfig pointing at a temp `active` dir with the given log mode."""
    return fxtrconfig.FxtrConfig(
        author=fxtrconfig.AuthorConfig(name="Tester", email="tester@example.org"),
        blueprint=fxtrconfig.BlueprintConfig(sources={}),
        data=fxtrconfig.DataConfig(active=str(active), materialization_log_mode=mode),
        resolver=fxtrconfig.ResolverConfig(trust_impl_code=True, default_py_provider="uv"),
        provider=fxtrconfig.ProviderConfig(
            uv=fxtrconfig.UvProviderConfig(
                virtual_env_dir=str(active / "venvs"),
                dynamic_source_packages=[],
                local_source_packages={},
            )
        ),
        config_path=active / "fxtr.toml",
    )


def _cid(label: str) -> cbrrr.CID:
    """A deterministic, distinct CID for use as a test task/run key."""
    return cbrrr.CID.cidv1_raw_sha256_32_from(label.encode())


def _record(
    *,
    cached: dict | None = None,
    completed: dict | None = None,
    failed: dict | None = None,
    cancelled: dict | None = None,
    timestamp: str = "2026-06-23T12:00:00.123456",
    message: str | None = "test message",
) -> sm.MaterializationRecord:
    return sm.MaterializationRecord(
        message=message,
        author="Tester",
        author_email="tester@example.org",
        request_timestamp=timestamp,
        root="some-root-container",
        should_continue_after_failures=False,
        cached=frozendict(cached or {}),
        completed=frozendict(completed or {}),
        failed=frozendict(failed or {}),
        cancelled=frozendict(cancelled or {}),
    )


@pytest.fixture
def active_dir() -> Generator[Path, None, None]:
    d = Path(tempfile.mkdtemp(prefix="fxtr_mat_log_test_"))
    try:
        yield d
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


# -- per_file mode (default, unchanged behavior) --------------------------------------------------


def test_per_file_writes_pointer_for_cache_only(active_dir: Path) -> None:
    """per_file mode keeps logging every request, including cache-only ones."""
    config = _make_config(active_dir, "per_file")
    rec = _record(cached={_cid("task"): _cid("run")})
    sm._save_materialization_record(config, rec)

    mat_dir = fxtrconfig.get_active_state_store(config) / "materializations"
    files = list(mat_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["materialization_record_cid"] == core.entity_cid(rec).encode(base="base32")


# -- jsonl mode -----------------------------------------------------------------------------------


def test_jsonl_skips_cache_only(active_dir: Path) -> None:
    """A request that only hit the cache (no new work) is not logged at all."""
    config = _make_config(active_dir, "jsonl")
    rec = _record(cached={_cid("task"): _cid("run")})
    sm._save_materialization_record(config, rec)

    state = fxtrconfig.get_active_state_store(config)
    assert not (state / "materializations.jsonl").exists()
    assert not (state / "materialization_failures").exists()


def test_jsonl_logs_completed_work(active_dir: Path) -> None:
    """A request that scheduled new work appends one self-describing summary line."""
    config = _make_config(active_dir, "jsonl")
    tcid = _cid("task")
    rec = _record(
        cached={_cid("cached_task"): _cid("cached_run")},
        completed={tcid: _cid("run")},
    )
    sm._save_materialization_record(config, rec)

    jsonl = fxtrconfig.get_active_state_store(config) / "materializations.jsonl"
    lines = jsonl.read_text().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["cid"] == core.entity_cid(rec).encode(base="base32")
    assert summary["ts"] == rec.request_timestamp
    assert summary["n_cached"] == 1
    assert summary["n_completed"] == 1
    assert summary["n_failed"] == 0
    assert summary["failure_file"] is None


def test_jsonl_appends_multiple_lines(active_dir: Path) -> None:
    """Successive logged requests append, rather than overwrite."""
    config = _make_config(active_dir, "jsonl")
    sm._save_materialization_record(
        config, _record(completed={_cid("a"): _cid("ra")}, timestamp="2026-06-23T12:00:00")
    )
    sm._save_materialization_record(
        config, _record(completed={_cid("b"): _cid("rb")}, timestamp="2026-06-23T12:00:01")
    )

    jsonl = fxtrconfig.get_active_state_store(config) / "materializations.jsonl"
    lines = jsonl.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["ts"] for line in lines] == [
        "2026-06-23T12:00:00",
        "2026-06-23T12:00:01",
    ]


def test_jsonl_dumps_failure_tracebacks(active_dir: Path) -> None:
    """A failed request writes a per-request traceback dump and references it in the summary."""
    config = _make_config(active_dir, "jsonl")
    tcid = _cid("failing_task")
    rec = _record(failed={tcid: "Task error: boom"})
    tb = 'Traceback (most recent call last):\n  ...\nValueError: boom'
    sm._save_materialization_record(config, rec, {tcid: tb})

    state = fxtrconfig.get_active_state_store(config)

    # jsonl line references the failure file and inlines the error message.
    summary = json.loads((state / "materializations.jsonl").read_text().splitlines()[0])
    assert summary["n_failed"] == 1
    assert summary["failed"][tcid.encode(base="base32")] == "Task error: boom"
    assert summary["failure_file"] is not None

    # The dump file exists, is named after the request, and contains the full traceback.
    dump_dir = state / "materialization_failures"
    dumps = list(dump_dir.glob("*.txt"))
    assert len(dumps) == 1
    assert dumps[0].name == summary["failure_file"]
    contents = dumps[0].read_text()
    assert "ValueError: boom" in contents
    assert tcid.encode(base="base32") in contents
