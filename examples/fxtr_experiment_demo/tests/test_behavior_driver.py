"""Headless coverage for the behavior scenario's runner + driver.

Three tiers (the chunk-2 plan's test section):

- **structural** (mode ``off``): every stage button builds the
  documented grid tree without executing anything — the rule wirings
  (vmapped config grants, kept provider axes, the replica coord view,
  the four-axes-kept analysis grant) all canonicalize and persist.
- **mocked end-to-end** (mode ``mock``): the whole scenario over the
  deterministic fake services — stages 1-3, a foreground demand, the
  full stage-4 augment → stale → clear → re-run arc, and the
  background ``run_analysis`` button. CI-able, no API key.
- **mode-switch staleness**: rebuilding structure under a different
  mode overwrites the handle cells, fanning staleness through the
  evidence (the design's mock-results-are-never-real guarantee).
- **live smoke** (mode ``real``): skipped without ``ANTHROPIC_API_KEY``;
  a 1x1x1x1-turn sweep against the real API.
"""

from __future__ import annotations

import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import pytest
import scenario_content as content
from behavior_demo_helper import BehaviorDemoHelperProto
from behavior_demo_helper_service import mount_behavior_demo_helper
from behavior_research_types import (
    AnalysisTable,
    JudgeResult,
    RolloutConfig,
    Transcript,
    UserSimConfig,
    UserSimImprovementResult,
)
from behavior_runner import BehaviorRunner, behavior_mount_factory
from experiment_runner import RunnerSupervisor
from fxtr2.data.entity_ref_scope import EntityRef

pytestmark = pytest.mark.anyio

_ROLLOUT0 = {"subj_id": "baseline", "sim_id": "cautious_buyer", "replica": 0}


@asynccontextmanager
async def _mounted(
    data_dir: Path, *, llm_mode: str = "off", api_key: str | None = None
) -> AsyncIterator[tuple[RunnerSupervisor, Any]]:
    """A live behavior supervisor (no workstation registrar) + a
    resolved driver proxy."""
    async with RunnerSupervisor(
        data_dir,
        mount=behavior_mount_factory(llm_mode=llm_mode, anthropic_api_key=api_key),  # type: ignore[arg-type]
        external_name="behavior-workflow",
    ) as sup:
        await sup.revive()
        async with mount_behavior_demo_helper(sup) as mounted:
            ref = mounted.service_ref.replace_service_type(BehaviorDemoHelperProto)
            yield sup, await ref.resolve(mounted.scope)


def _wf(sup: RunnerSupervisor) -> tuple[BehaviorRunner, Any]:
    runner = sup.current
    assert isinstance(runner, BehaviorRunner)
    return runner, runner.workflow


async def _attach_path(wf, *keys: str):
    view = wf.root
    for key in keys:
        view = await wf.attach(view, key)
    return view


async def test_structural_stages_off_mode(tmp_path: Path) -> None:
    """Every stage builds structure without executing anything."""
    async with _mounted(tmp_path / "data") as (sup, helper):
        out = await helper.stage_structure()
        assert out["llm_mode"] == "off"
        runner, wf = _wf(sup)

        root_keys = {k.key: k.kind for k in await wf.list_keys(wf.root)}
        assert root_keys == {
            "tasks": "subtree",
            "services": "subtree",
            "configs": "subtree",
            "domain": "subtree",
            "evidence": "subtree",
            "analyses": "subtree",
            "transformations": "subtree",
        }
        tasks = await _attach_path(wf, "tasks")
        assert {k.key: k.kind for k in await wf.list_keys(tasks)} == {
            "rollout": "handle",
            "judge": "handle",
            "analyze": "handle",
            "improve_sim": "handle",
        }
        improve = await _attach_path(wf, "transformations", "user_sim_improve")
        assert {k.key: k.kind for k in await wf.list_keys(improve)} == {
            "request": "data",
            "flow": "task",
        }
        services = await _attach_path(wf, "services")
        assert {k.key: k.kind for k in await wf.list_keys(services)} == {
            "subject_model_services": "handle",
            "user_simulator_service": "handle",
            "judge_services": "handle",
        }
        evidence = await _attach_path(wf, "evidence")
        assert {k.key: k.kind for k in await wf.list_keys(evidence)} == {
            "rollouts": "task",
            "judgements": "task",
        }
        # The domain cells are zero-axis dataframe grids (the id-list
        # frames the analysis's gather drives from); sweep stays data.
        domain = await _attach_path(wf, "domain")
        assert {k.key: k.kind for k in await wf.list_keys(domain)} == {
            "active_subject_models": "dataframe",
            "active_user_sims": "dataframe",
            "active_judges": "dataframe",
            "sweep_config": "data",
        }
        # The analyses subtree exists and is empty until stage 3.
        analyses = await _attach_path(wf, "analyses")
        assert await wf.list_keys(analyses) == ()
        # Handle cells hold the off mode's (anthropic) keys.
        rollout_h = await wf.attach(tasks, "rollout")
        assert (await wf.inspect(rollout_h)).value == runner.fn_keys["rollout"]
        subject_svcs = await wf.attach(services, "subject_model_services")
        assert (await wf.inspect(subject_svcs, provider="anthropic")).value == runner.svc_keys[
            "subject_model"
        ]

        out = await helper.status()
        assert "unpopulated" in out["note"]

        out = await helper.stage_inputs()
        assert set(out["subjects"]) == {"baseline", "pushy"}
        out = await helper.status()
        assert out["domain"]["replicas"] == 1
        assert out["rollouts"] == {"missing": 2 * 2 * 1}
        assert out["judgements"] == {"missing": 2 * 2 * 1 * 2}
        assert "not created" in out["analysis"]

        out = await helper.stage_analysis()
        assert "sales_report" in out["grid"]
        assert await wf.list_keys(analyses) != ()
        await helper.target_analysis()
        assert len(await wf.targets()) == 1

        out = await helper.stage_augment()
        assert out["replicas"] == 2
        out = await helper.status()
        assert list(out["domain"]["subjects"]) == ["baseline", "principled", "pushy"]
        assert out["rollouts"] == {"missing": 3 * 3 * 2}
        assert out["judgements"] == {"missing": 3 * 3 * 2 * 3}


def _rows_by_pair(table: AnalysisTable) -> dict[tuple[Any, Any], tuple[Any, Any]]:
    assert table.columns == ("subject_model", "judge", "mean_score", "n")
    return {(r[0], r[1]): (r[2], r[3]) for r in table.rows}


async def test_mocked_end_to_end(tmp_path: Path) -> None:
    """The whole scenario over the deterministic mock services."""
    # Re-arm the conftest deadlock guard: the full-size stage-4 rerun
    # (18 rollouts + 54 judgements through real service boundaries) runs
    # ~25s and would flake against the default 30s alarm.
    signal.alarm(120)
    async with _mounted(tmp_path / "data", llm_mode="mock") as (sup, helper):
        await helper.stage_structure()
        await helper.stage_inputs()
        await helper.stage_analysis()
        await helper.target_analysis()
        runner, wf = _wf(sup)
        analysis = await _attach_path(wf, "analyses", "sales_report")

        # Foreground demand: pulls 4 rollouts + 8 judgements + the table.
        table_id = await analysis.output_entity("report").read()
        # Membership: served by the capability → loadable in the runner scope.
        table = await EntityRef(table_id, AnalysisTable).load(runner.scope)
        rows = _rows_by_pair(table)
        assert set(rows) == {
            (s, j) for s in ("baseline", "pushy") for j in ("effectiveness", "pushiness")
        }
        for mean, n in rows.values():
            assert 0.0 <= mean <= 1.0
            assert n == 2  # 2 sims x 1 replica

        # A rollout's transcript is complete, fresh, and visibly mock.
        rollouts = await _attach_path(wf, "evidence", "rollouts")
        report = await wf.inspect(rollouts, **_ROLLOUT0)
        assert (report.status, report.currency) == ("complete", "fresh")
        await wf.add_target(rollouts, **_ROLLOUT0)
        tid = await rollouts.output_entity().read(**_ROLLOUT0)
        transcript = await EntityRef(tid, Transcript).load(runner.scope)
        assert [m.role for m in transcript.messages] == [
            "user",
            "assistant",
        ] * content.ROLLOUT.max_turns
        assert all("[MOCK" in m.text for m in transcript.messages)
        # In-data provenance: the transcript's refs chase back to the
        # exact configs it was produced from (refs are in the served
        # entity's closure, so loadable in the runner scope).
        subj_cfg = await transcript.subject_config.load(runner.scope)
        assert subj_cfg == content.STAGE2_SUBJECTS["baseline"]
        sim_cfg = await transcript.sim_config.load(runner.scope)
        assert sim_cfg == content.STAGE2_SIMS["cautious_buyer"]

        # ...and the analysis rows ref every judgement they aggregate,
        # each of which refs its transcript + rubric.
        assert len(table.row_sources) == len(table.rows)
        for row, sources in zip(table.rows, table.row_sources):
            assert len(sources) == row[3]  # the row's n column
        source = await table.row_sources[0][0].load(runner.scope)
        assert 0.0 <= source.score <= 1.0
        judged = await source.transcript.load(runner.scope)
        assert judged.messages

        # The analysis's second output: the gathered evidence frame
        # (key columns + score), consistent with the entity's rows.
        evidence = await wf.demand_table(analysis.output_frame("evidence"))
        assert evidence.height == 2 * 2 * 1 * 2  # subjects x sims x replicas x judges
        assert set(evidence.columns) == {"subj_id", "sim_id", "replica", "judge_id", "score"}
        by_pair: dict[tuple[Any, Any], list[float]] = {}
        for r in evidence.iter_rows(named=True):
            assert 0.0 <= r["score"] <= 1.0
            by_pair.setdefault((r["subj_id"], r["judge_id"]), []).append(r["score"])
        for pair, scores in by_pair.items():
            mean, n = rows[pair]
            assert n == len(scores)
            assert abs(mean - sum(scores) / len(scores)) < 1e-9

        # A judgement cell's `data` frame output: one row, the score.
        judgements_v = await _attach_path(wf, "evidence", "judgements")
        jdf = await wf.demand_table(
            judgements_v.output_frame("data"), **_ROLLOUT0, judge_id="pushiness"
        )
        assert jdf.height == 1
        # The cell's score is one of the values the evidence frame
        # aggregated for its (subject, judge) pair.
        assert jdf["score"][0] in by_pair[("baseline", "pushiness")]

        # Stage 4: the domain edit lands as analysis staleness ONLY —
        # existing evidence stays fresh (rollouts never read the domain).
        await helper.stage_augment()
        assert (await wf.inspect(analysis)).currency == "stale"
        assert (await wf.inspect(rollouts, **_ROLLOUT0)).currency == "fresh"

        out = await helper.clear_stale()
        assert out["count"] == 1  # exactly the analysis cell

        # Re-run via the background button; poll the status snapshot.
        out = await helper.run_analysis()
        assert "background" in out["note"]
        # Generous: the full-size rerun is ~20s alone under load.
        with anyio.fail_after(60):
            while True:
                s = await helper.status()
                if s.get("background", {}).get("analysis") == "completed":
                    break
                await anyio.sleep(0.1)
        assert s["analysis"]["currency"] == "fresh"
        assert s["rollouts"] == {"fresh": 3 * 3 * 2}
        assert s["judgements"] == {"fresh": 3 * 3 * 2 * 3}

        # The cell is fresh, so this read is served, not re-run.
        table2_id = await analysis.output_entity("report").read()
        table2 = await EntityRef(table2_id, AnalysisTable).load(runner.scope)
        rows2 = _rows_by_pair(table2)
        assert len(rows2) == 3 * 3
        for mean, n in rows2.values():
            assert 0.0 <= mean <= 1.0
            assert n == 3 * 2  # 3 sims x 2 replicas


async def test_mocked_sim_improvement(tmp_path: Path) -> None:
    """The ABR-style transformation over the mock services: request
    built from selected evidence, the flow trials two candidates with
    nested rollout+judge runs in its own internals, and the result
    carries the whole trajectory as refs."""
    signal.alarm(120)  # nested rollouts per candidate — comfortably > 30s under load
    async with _mounted(tmp_path / "data", llm_mode="mock") as (sup, helper):
        await helper.stage_structure()
        await helper.stage_inputs()
        await helper.stage_analysis()
        await helper.target_analysis()
        runner, wf = _wf(sup)
        analysis = await _attach_path(wf, "analyses", "sales_report")
        await analysis.output_entity("report").read()  # populate the evidence

        out = await helper.improve_sim(
            "cautious_buyer", "baseline", "pushiness", "make the customer harder to close"
        )
        assert "error" not in out
        request_id = out["request_id"]
        assert request_id.startswith("cautious_buyer-")
        assert out["sources"] == 1  # 1 replica in stage 2

        # Await the background flow, then read the (now fresh) output.
        flows = await _attach_path(wf, "transformations", "user_sim_improve", "flow")
        with anyio.fail_after(90):
            while True:
                s = await helper.status()
                state = s.get("background", {}).get(f"improve:{request_id}")
                if state == "completed":
                    break
                assert state == "running", state
                await anyio.sleep(0.1)
        result_id = await flows.output_entity().read(request_id=request_id)
        result = await EntityRef(result_id, UserSimImprovementResult).load(runner.scope)

        # The chronological event log: request, two (proposal, trial
        # transcript, trial judgement) rounds, final choice.
        assert [e.kind for e in result.events] == [
            "request",
            "proposal",
            "trial_transcript",
            "trial_judgement",
            "proposal",
            "trial_transcript",
            "trial_judgement",
            "final_choice",
        ]
        final_bio = await result.final_bio.load(runner.scope)
        assert "[MOCK improved persona" in final_bio.persona
        assert "[MOCK improver notes" in result.final_notes
        assert result.events[-1].text == result.final_notes
        req = await result.request.load(runner.scope)
        assert req.instruction == "make the customer harder to close"
        assert result.events[0].text == req.instruction
        assert len(req.rollouts) == len(req.judgements) == 1
        for offset in (1, 4):  # each round's proposal event
            proposal, trial_t, trial_j = result.events[offset : offset + 3]
            assert proposal.subject is not None
            bio = await proposal.subject.replace_target_type(UserSimConfig).load(runner.scope)
            assert "[MOCK improved persona" in bio.persona
            assert "[MOCK improver notes" in proposal.text
            assert trial_j.subject is not None
            trial = await trial_j.subject.replace_target_type(JudgeResult).load(runner.scope)
            assert 0.0 <= trial.score <= 1.0
            assert trial_t.subject is not None
            trial_transcript = await trial_t.subject.replace_target_type(Transcript).load(
                runner.scope
            )
            # The nested rollout really ran the candidate persona.
            assert (await trial_transcript.sim_config.load(runner.scope)) == bio

        # The flow's internals hold the isolated trial pipeline.
        flow_internals = await wf.internals(flows, request_id=request_id)
        internal_keys = {k.key: k.kind for k in await wf.list_keys(flow_internals)}
        assert internal_keys == {
            "subject_cfg": "data",
            "rollout_cfg": "data",
            "judge_cfg": "data",
            "bios": "data",
            "rollouts": "task",
            "judgements": "task",
            "steps": "checkpoint",
        }


async def test_mode_switch_invalidates_evidence(tmp_path: Path) -> None:
    """Rebuilding structure under a different mode overwrites the handle
    cells — mock-computed evidence goes stale rather than passing as
    real (the distinct-keys design guarantee)."""
    data = tmp_path / "data"
    async with _mounted(data, llm_mode="mock") as (sup, helper):
        await helper.stage_structure()
        await helper.stage_inputs()
        await helper.stage_analysis()
        await helper.target_analysis()
        _, wf = _wf(sup)
        analysis = await _attach_path(wf, "analyses", "sales_report")
        await analysis.output_entity("report").read()

    async with _mounted(data, llm_mode="off") as (sup, helper):
        _, wf = _wf(sup)
        rollouts = await _attach_path(wf, "evidence", "rollouts")
        # Untouched, the mock evidence is still fresh...
        assert (await wf.inspect(rollouts, **_ROLLOUT0)).currency == "fresh"
        # ...until stage_structure writes this mode's keys over the
        # mock ones, fanning staleness through everything computed
        # against the mock services.
        await helper.stage_structure()
        assert (await wf.inspect(rollouts, **_ROLLOUT0)).currency == "stale"


@pytest.mark.skipif(
    "ANTHROPIC_API_KEY" not in os.environ,
    reason="live Anthropic smoke needs ANTHROPIC_API_KEY (never in CI)",
)
async def test_live_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1-subject x 1-sim x 1-judge x 1-replica x 1-turn sweep against
    the real API (the smallest end-to-end proof the real services work)."""
    signal.alarm(120)  # re-arm the conftest guard: real LLM calls are slow
    monkeypatch.setattr(
        content, "STAGE2_SUBJECTS", {"baseline": content.STAGE2_SUBJECTS["baseline"]}
    )
    monkeypatch.setattr(
        content, "STAGE2_SIMS", {"cautious_buyer": content.STAGE2_SIMS["cautious_buyer"]}
    )
    monkeypatch.setattr(content, "STAGE2_JUDGES", {"pushiness": content.STAGE2_JUDGES["pushiness"]})
    monkeypatch.setattr(content, "ROLLOUT", RolloutConfig(max_turns=1))
    async with _mounted(
        tmp_path / "data", llm_mode="real", api_key=os.environ["ANTHROPIC_API_KEY"]
    ) as (sup, helper):
        await helper.stage_structure()
        await helper.stage_inputs()
        await helper.stage_analysis()
        await helper.target_analysis()
        runner, wf = _wf(sup)
        analysis = await _attach_path(wf, "analyses", "sales_report")
        table_id = await analysis.output_entity("report").read()
        table = await EntityRef(table_id, AnalysisTable).load(runner.scope)
        rows = _rows_by_pair(table)
        assert set(rows) == {("baseline", "pushiness")}
        (mean, n) = rows[("baseline", "pushiness")]
        assert 0.0 <= mean <= 1.0
        assert n == 1
        rollouts = await _attach_path(wf, "evidence", "rollouts")
        await wf.add_target(rollouts, **_ROLLOUT0)
        tid = await rollouts.output_entity().read(**_ROLLOUT0)
        transcript = await EntityRef(tid, Transcript).load(runner.scope)
        assert [m.role for m in transcript.messages] == ["user", "assistant"]
        assert all(m.text.strip() for m in transcript.messages)
