"""Constant lists + small helpers for inspecting judges in the
Docent collection ``2c4ecb4e-6f13-4bfb-9969-00f2043c2f15``
(mental-health relevant WildChat eval).

Constants:

- :data:`CHAINED_JUDGE_MODELS` — the 4 models used in the
  ``chained-judges`` transcript-group.
- :data:`CHAINED_JUDGES` — all 46 distinct judge "type" names
  seen under that group (transcript name with the model suffix
  stripped).
- :data:`UNCHAINED_CHAINED_JUDGES` — the subset of
  :data:`CHAINED_JUDGES` whose name is a **single component**
  (no chain separator). These are the easy-to-extract judges:
  the transcript name, the metadata score key, and the
  reasoning location are all 1-to-1 with a single
  ``(judge_name, model)`` pair (see "Extracting (score,
  reasoning) pairs" below).
- :data:`CHAINED_JUDGE_PAIRS` — a dict mapping each two-component
  chained-judges name to its ``(gate, behavior)`` leaves. For
  these, the transcript covers the whole chain in one reasoning
  message, but the agent_run metadata stores **separate scores
  per leaf component**.
- :data:`PRIMARY_ADDITIONAL_TRANSCRIPT_NAMES` — every
  ``transcripts.name`` value (``{judge_type}_{judge_model}``)
  matching the primary + additional taxonomies. Coverage isn't
  uniform — model coverage is included only where the row exists.

Naming convention in this collection (under group
``chained-judges``):

- Two-component judges: ``{user_applicability}_{assistant_behavior}_{model}``
- Single-component judges: ``{name}_{model}``
- Date suffixes on names use ``__YYYY-MM-DD`` (double underscore)
  to keep the single-``_`` chain separator unambiguous.

Date suffixes on judge names differ from the canonical taxonomy
strings (e.g. ``safety-monitoring__2026-02-12`` here vs.
``safety-monitoring__2026-04-27`` in the taxonomy reference);
the names below are the strings as they appear in this
collection.

Two taxonomy entries from the reference lists are **not present**
in this collection:

- ``harmful-compliance-with-creative-writing-about-users-own-death-or-suicide``
- ``creative-writing-about-death-or-suicide-with-evidence-of-risk__2026-04-27``

----

Extracting (score, reasoning) pairs for an "unchained" judge
============================================================

Given an agent_run and an unchained judge name (e.g.
``"safety-monitoring__2026-02-12"``), you can get the matched
``(score, reasoning)`` pair for each model in
:data:`CHAINED_JUDGE_MODELS`.

**Where the data lives**

- **Score** — in ``agent_runs.metadata_json`` under the key
  ``chained_{judge_name}_{model}`` (as a stringified float — DQL
  returns JSON values as strings via ``->>``; cast to float
  yourself). Use :func:`score_metadata_key` to build the key.
  Coverage isn't uniform: a missing key means that ``(judge,
  model)`` wasn't scored for that run.
- **Reasoning** — in the ``transcripts`` table, in the group
  ``chained-judges``, on the row whose name is
  ``{judge_name}_{model}``. The reasoning is the **second
  message** (index 1, role ``assistant``). The ``content`` may
  be either a string OR a list of parts (each part a dict with a
  ``text`` field) — :func:`parse_judge_response` normalizes
  both.

**Response shape inside the second message**::

    <reasoning>step-by-step reasoning here…</reasoning>
    <score>N</score>
    <was_tough_call>true|false</was_tough_call>

The inline ``<score>`` and the metadata float almost always
agree; prefer the metadata float (it's already typed).

**DQL query — fetch all per-model judge transcripts for one
agent_run in a single call**. Filter by ``transcript_group.name``
AND ``transcript.agent_run_id`` AND the full ``IN (...)`` list of
expected transcript names — keeps the result tight and avoids the
DB statement timeout that ``ORDER BY`` on the full table tends to
trigger. See :func:`build_judge_transcripts_query`.

**End-to-end Python sketch**::

    import json
    from judge_constants import (
        CHAINED_JUDGE_MODELS, parse_judge_response,
        score_metadata_key, transcript_name_for,
        build_judge_transcripts_query,
    )
    import docent_fxtr_proxy as docent
    from _fxtr_mcp_builtin import retrieve_result
    from fxtr2.data.entity_ref_scope import EntityRef

    judge_name = "safety-monitoring__2026-02-12"
    agent_run_id = "..."
    collection_id = "..."

    # 1. Fetch the agent_run row's metadata (one query).
    await docent.schedule_execute_dql(
        "ar",
        collection_id=collection_id,
        query=f'''
            SELECT metadata_json
            FROM agent_runs
            WHERE id = '{agent_run_id}'
        ''',
    )

    # 2. Fetch all per-model judge transcripts in one query.
    await docent.schedule_execute_dql(
        "judges",
        collection_id=collection_id,
        query=build_judge_transcripts_query(
            agent_run_id=agent_run_id,
            judge_name=judge_name,
        ),
    )

    # (wait for both slots to land, then in a follow-up script)

    ar_meta = json.loads(
        (await EntityRef(
            await retrieve_result("ar"), docent.DqlResult
        ).load()).rows[0][0]
    )
    rows = docent.dql_result_to_dicts(
        await EntityRef(
            await retrieve_result("judges"), docent.DqlResult
        ).load()
    )

    pairs = {}  # model -> (score, reasoning)
    for row in rows:
        # row['name'] is transcript_name_for(judge_name, model)
        model = row["name"][len(judge_name) + 1:]
        messages = json.loads(row["messages"])
        parsed = parse_judge_response(messages[1]["content"])
        score = ar_meta.get(score_metadata_key(judge_name, model))
        pairs[model] = (
            float(score) if score is not None else None,
            parsed["reasoning"],
        )

Chained-pair judges (not "unchained")
=====================================

A name like
``"consequential-ambiguity-applicability_following-up-on-consequential-ambiguity"``
is a chained-judge pair: the user-applicability gate
(``consequential-ambiguity-applicability``) precedes the
assistant-behavior (``following-up-on-consequential-ambiguity``).

For these:

- The transcript in ``chained-judges`` carries the **chained
  reasoning** — both halves discussed in one assistant message.
- The agent_run metadata stores **two separate score keys**
  (one per leaf component, e.g.
  ``chained_consequential-ambiguity-applicability_<model>`` and
  ``chained_following-up-on-consequential-ambiguity_<model>``).

So a chained-pair judge isn't 1-to-1 with a single score key.
Decide per use-case whether you want the gate-side score, the
behavior-side score, or both — the leaves are accessible via
:data:`CHAINED_JUDGE_PAIRS`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Judge models used in the ``chained-judges`` transcript group.
CHAINED_JUDGE_MODELS: list[str] = [
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5-20250929-extended-thinking",
    "gpt-5.4",
    "gpt-5.4-medium",
]

#: All transcript names (``{judge_type}_{model}``) under the
#: ``chained-judges`` group that map to the primary + additional
#: taxonomy. Ordered: PRIMARY assistant behaviors, then PRIMARY
#: user-applicability standalones, then ADDITIONAL assistant
#: behaviors. Within each judge, models are sorted alphabetically.
PRIMARY_ADDITIONAL_TRANSCRIPT_NAMES: list[str] = [
    # PRIMARY assistant behaviors.
    "user-expresses-suicidal-ideation_explicit-encouragement-or-validation-of-suicide_claude-sonnet-4-5-20250929",
    "user-expresses-suicidal-ideation_explicit-encouragement-or-validation-of-suicide_claude-sonnet-4-5-20250929-extended-thinking",
    "user-expresses-suicidal-ideation_explicit-encouragement-or-validation-of-suicide_gpt-5.4",
    "user-expresses-suicidal-ideation_explicit-encouragement-or-validation-of-suicide_gpt-5.4-medium",
    "user-expresses-suicidal-ideation_provision-of-instrumental-support-for-suicide-planning__2026-03-16_claude-sonnet-4-5-20250929-extended-thinking",
    "user-expresses-suicidal-ideation_provision-of-instrumental-support-for-suicide-planning__2026-03-16_gpt-5.4",
    "user-expresses-suicidal-ideation_provision-of-instrumental-support-for-suicide-planning__2026-03-16_gpt-5.4-medium",
    "fostering-unhealthy-user-dependency__2026-03-16_claude-sonnet-4-5-20250929-extended-thinking",
    "fostering-unhealthy-user-dependency__2026-03-16_gpt-5.4",
    "fostering-unhealthy-user-dependency__2026-03-16_gpt-5.4-medium",
    "user-expresses-suicidal-ideation_extended-co-rumination-on-death-or-suicide-themes_claude-sonnet-4-5-20250929",
    "user-expresses-suicidal-ideation_extended-co-rumination-on-death-or-suicide-themes_claude-sonnet-4-5-20250929-extended-thinking",
    "user-expresses-suicidal-ideation_extended-co-rumination-on-death-or-suicide-themes_gpt-5.4",
    "user-expresses-suicidal-ideation_extended-co-rumination-on-death-or-suicide-themes_gpt-5.4-medium",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_endorsement-or-reinforcement-of-beliefs-reflecting-impaired-reality-testing_claude-sonnet-4-5-20250929",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_endorsement-or-reinforcement-of-beliefs-reflecting-impaired-reality-testing_claude-sonnet-4-5-20250929-extended-thinking",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_endorsement-or-reinforcement-of-beliefs-reflecting-impaired-reality-testing_gpt-5.4",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_endorsement-or-reinforcement-of-beliefs-reflecting-impaired-reality-testing_gpt-5.4-medium",
    "safety-monitoring__2026-02-12_claude-sonnet-4-5-20250929",
    "safety-monitoring__2026-02-12_claude-sonnet-4-5-20250929-extended-thinking",
    "safety-monitoring__2026-02-12_gpt-5.4",
    "safety-monitoring__2026-02-12_gpt-5.4-medium",
    "facilitating-connection-to-human-support__2026-03-16_claude-sonnet-4-5-20250929-extended-thinking",
    "facilitating-connection-to-human-support__2026-03-16_gpt-5.4",
    "facilitating-connection-to-human-support__2026-03-16_gpt-5.4-medium",
    # PRIMARY user-applicability (standalone).
    "user-discusses-suicide-planning-or-intent_claude-sonnet-4-5-20250929",
    "user-discusses-suicide-planning-or-intent_claude-sonnet-4-5-20250929-extended-thinking",
    "user-discusses-suicide-planning-or-intent_gpt-5.4",
    "user-discusses-suicide-planning-or-intent_gpt-5.4-medium",
    "user-expresses-dependency-on-assistant_claude-sonnet-4-5-20250929-extended-thinking",
    "user-expresses-dependency-on-assistant_gpt-5.4",
    "user-expresses-dependency-on-assistant_gpt-5.4-medium",
    # ADDITIONAL assistant behaviors.
    "poetic-escalation-applicability_poetic-escalation_claude-sonnet-4-5-20250929",
    "poetic-escalation-applicability_poetic-escalation_claude-sonnet-4-5-20250929-extended-thinking",
    "poetic-escalation-applicability_poetic-escalation_gpt-5.4",
    "poetic-escalation-applicability_poetic-escalation_gpt-5.4-medium",
    "consequential-ambiguity-applicability_following-up-on-consequential-ambiguity_claude-sonnet-4-5-20250929",
    "consequential-ambiguity-applicability_following-up-on-consequential-ambiguity_claude-sonnet-4-5-20250929-extended-thinking",
    "consequential-ambiguity-applicability_following-up-on-consequential-ambiguity_gpt-5.4",
    "consequential-ambiguity-applicability_following-up-on-consequential-ambiguity_gpt-5.4-medium",
    "situations-warranting-goal-directed-support_beneficent-goal-directed-tenacity_claude-sonnet-4-5-20250929",
    "situations-warranting-goal-directed-support_beneficent-goal-directed-tenacity_claude-sonnet-4-5-20250929-extended-thinking",
    "situations-warranting-goal-directed-support_beneficent-goal-directed-tenacity_gpt-5.4",
    "situations-warranting-goal-directed-support_beneficent-goal-directed-tenacity_gpt-5.4-medium",
    "verbose-complex-applicability_verbose-complex_claude-sonnet-4-5-20250929",
    "verbose-complex-applicability_verbose-complex_claude-sonnet-4-5-20250929-extended-thinking",
    "verbose-complex-applicability_verbose-complex_gpt-5.4",
    "verbose-complex-applicability_verbose-complex_gpt-5.4-medium",
    "user-presents-problematic-framing_lock-in-to-problematic-framing_claude-sonnet-4-5-20250929",
    "user-presents-problematic-framing_lock-in-to-problematic-framing_claude-sonnet-4-5-20250929-extended-thinking",
    "user-presents-problematic-framing_lock-in-to-problematic-framing_gpt-5.4",
    "user-presents-problematic-framing_lock-in-to-problematic-framing_gpt-5.4-medium",
    "user-provides-feedback-about-assistants-approach_adaptive-responsiveness-to-user-feedback_claude-sonnet-4-5-20250929",
    "user-provides-feedback-about-assistants-approach_adaptive-responsiveness-to-user-feedback_claude-sonnet-4-5-20250929-extended-thinking",
    "user-provides-feedback-about-assistants-approach_adaptive-responsiveness-to-user-feedback_gpt-5.4",
    "user-provides-feedback-about-assistants-approach_adaptive-responsiveness-to-user-feedback_gpt-5.4-medium",
]

#: All 46 distinct judge "type" names in the ``chained-judges``
#: transcript group (model suffix stripped). Sorted alphabetically.
CHAINED_JUDGES: list[str] = [
    "consequential-ambiguity-applicability_following-up-on-consequential-ambiguity",
    "consequential-ambiguity-applicability_user-makes-concerning-interpretation-salient",
    "facilitating-connection-to-human-support__2026-03-16",
    "fostering-unhealthy-user-dependency__2026-03-16",
    "poetic-escalation-applicability_poetic-escalation",
    "safety-monitoring__2026-02-12",
    "situations-warranting-goal-directed-support_beneficent-goal-directed-tenacity",
    "user-communicates-with-high-intellectual-sophistication",
    "user-context-warrants-human-support__2026-03-16",
    "user-context-warrants-human-support_facilitating-connection-to-human-support",
    "user-context-warrants-human-support_fostering-unhealthy-user-dependency__2026-01-28",
    "user-discusses-suicide-planning-or-intent",
    "user-draws-unfavorable-comparison-to-specific-concerned-people",
    "user-emotionally-composed-despite-severity",
    "user-employs-metaphorical-language__2026-03-05",
    "user-escalates-emotional-intensity-at-reality-testing",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_endorsement-or-reinforcement-of-beliefs-reflecting-impaired-reality-testing",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_user-expresses-uncertainty-about-reality",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_user-frames-concerning-content-as-neutral-practical-task",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_user-presentation-interleaves-plausibly-legitimate-elements",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_user-requests-engagement-with-framework",
    "user-exhibits-signs-of-delusions-grandiosity-or-impaired-reality-testing_user-wraps-beliefs-in-non-pathological-framework",
    "user-expresses-dependency-on-assistant",
    "user-expresses-suicidal-ideation_explicit-encouragement-or-validation-of-suicide",
    "user-expresses-suicidal-ideation_extended-co-rumination-on-death-or-suicide-themes",
    "user-expresses-suicidal-ideation_provision-of-instrumental-support-for-suicide-planning__2026-01-28",
    "user-expresses-suicidal-ideation_provision-of-instrumental-support-for-suicide-planning__2026-03-16",
    "user-expresses-suicidal-ideation_user-discloses-access-to-means",
    "user-expresses-suicidal-ideation_user-frames-si-as-rational-analysis",
    "user-frames-as-companionship-or-presence",
    "user-frames-interaction-as-practical-task",
    "user-gradually-reveals-risk-relevant-information",
    "user-has-concrete-request",
    "user-has-identified-human-contact",
    "user-initiates-topic-shift-from-concern",
    "user-offers-benign-explanation-for-concerning-indicators",
    "user-preemptively-constrains-response-approach",
    "user-presents-problematic-framing_lock-in-to-problematic-framing",
    "user-presents-problematic-framing_user-presents-concerning-behaviors-as-positive-growth__2026-03-05",
    "user-provides-feedback-about-assistants-approach_adaptive-responsiveness-to-user-feedback",
    "user-provides-substantive-domain-content",
    "user-pushes-back-against-reality-testing",
    "user-requests-witness-rather-than-help",
    "user-sets-preemptive-boundaries-against-support",
    "user-states-complete-social-isolation",
    "verbose-complex-applicability_verbose-complex",
]

#: Regex that matches the chain separator — a single ``_`` not
#: adjacent to another ``_``. Date suffixes (``__YYYY-MM-DD``) use
#: double underscores so this won't split them.
_CHAIN_SEPARATOR_RE = re.compile(r"(?<!_)_(?!_)")


def split_chained_judge_name(name: str) -> tuple[str, ...]:
    """Split a chained-judges type name into its 1-or-2 leaf
    components. Single-component names round-trip unchanged."""
    return tuple(_CHAIN_SEPARATOR_RE.split(name))


#: The subset of :data:`CHAINED_JUDGES` whose names are a single
#: component (no chain separator). For each entry, the transcript
#: name and the metadata score key both have a 1-to-1 mapping with
#: a single ``(judge_name, model)`` pair — the easy case for
#: extracting matched ``(score, reasoning)`` pairs.
UNCHAINED_CHAINED_JUDGES: list[str] = [
    n for n in CHAINED_JUDGES if len(split_chained_judge_name(n)) == 1
]

#: Two-component chained judges, keyed by the full chained name,
#: mapped to the ``(gate, behavior)`` leaves. For these, the
#: agent_run metadata stores one score per leaf — there is no
#: single key for the chained pair as a unit.
CHAINED_JUDGE_PAIRS: dict[str, tuple[str, str]] = {
    n: (parts[0], parts[1])
    for n in CHAINED_JUDGES
    if len(parts := split_chained_judge_name(n)) == 2
}


def transcript_name_for(judge_name: str, model: str) -> str:
    """Build the ``transcripts.name`` value for a given
    ``(judge_name, model)`` pair in the ``chained-judges`` group:
    ``f"{judge_name}_{model}"``."""
    return f"{judge_name}_{model}"


def score_metadata_key(judge_name: str, model: str) -> str:
    """Build the key under which ``(judge_name, model)``'s score
    lives in ``agent_runs.metadata_json``:
    ``f"chained_{judge_name}_{model}"``."""
    return f"chained_{judge_name}_{model}"


#: Regex matching the ``<reasoning>…</reasoning><score>…</score>
#: <was_tough_call>…</was_tough_call>`` shape every judge's
#: assistant message follows. The tags can carry surrounding
#: whitespace / newlines.
_JUDGE_RESPONSE_RE = re.compile(
    r"<reasoning>\s*(?P<reasoning>.*?)\s*</reasoning>\s*"
    r"<score>\s*(?P<score>[^<]*?)\s*</score>"
    r"(?:\s*<was_tough_call>\s*(?P<tough>true|false)\s*</was_tough_call>)?",
    re.DOTALL,
)


def parse_judge_response(content: Any) -> dict[str, Any]:
    """Parse the assistant-message content of a judge transcript.

    Returns a dict with:

    - ``reasoning`` (str) — the text inside ``<reasoning>...</reasoning>``.
    - ``score`` (str | None) — the raw text inside ``<score>...</score>``;
      cast to ``float`` if you want a number. ``None`` if the tag is missing.
    - ``was_tough_call`` (bool | None) — ``True`` / ``False`` if present,
      ``None`` if the tag is missing.

    ``content`` may be a ``str`` or a list of "parts" dicts (each
    with a ``text`` field); both shapes are normalized.

    On total parse failure (no ``<reasoning>`` tag at all), returns
    ``{"reasoning": <raw text>, "score": None, "was_tough_call": None}``
    so callers can still see what came back.
    """
    if isinstance(content, list):
        text = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    else:
        text = str(content)
    m = _JUDGE_RESPONSE_RE.search(text)
    if m is None:
        return {"reasoning": text, "score": None, "was_tough_call": None}
    tough = m.group("tough")
    return {
        "reasoning": m.group("reasoning"),
        "score": m.group("score") or None,
        "was_tough_call": None if tough is None else (tough == "true"),
    }


def build_judge_transcripts_query(
    *,
    agent_run_id: str,
    judge_name: str,
    models: Iterable[str] | None = None,
) -> str:
    """Build a DQL query that fetches the per-model judge
    transcripts under ``chained-judges`` for a single
    ``(agent_run_id, judge_name)``.

    ``models`` defaults to :data:`CHAINED_JUDGE_MODELS`. Returns the
    SQL string; pass it as the ``query=`` arg to
    ``docent.schedule_execute_dql(...)``.

    Selects ``t.name`` (so the caller can recover the model by
    stripping the ``{judge_name}_`` prefix) and ``t.messages``
    (the JSON-stringified messages list — ``json.loads`` it,
    then look at index 1 for the assistant reasoning).

    No ``ORDER BY`` — the row count is small (one per model) so
    sorting isn't needed, and ``ORDER BY`` on the ``transcripts``
    table tends to trigger the DB statement timeout.
    """
    selected_models = list(models) if models is not None else list(CHAINED_JUDGE_MODELS)
    in_list = ", ".join(f"'{transcript_name_for(judge_name, m)}'" for m in selected_models)
    return f"""
        SELECT t.name, t.messages
        FROM transcripts t
        JOIN transcript_groups tg ON tg.id = t.transcript_group_id
        WHERE tg.name = 'chained-judges'
          AND t.agent_run_id = '{agent_run_id}'
          AND t.name IN ({in_list})
    """


__all__ = [
    "CHAINED_JUDGE_MODELS",
    "CHAINED_JUDGES",
    "CHAINED_JUDGE_PAIRS",
    "PRIMARY_ADDITIONAL_TRANSCRIPT_NAMES",
    "UNCHAINED_CHAINED_JUDGES",
    "build_judge_transcripts_query",
    "parse_judge_response",
    "score_metadata_key",
    "split_chained_judge_name",
    "transcript_name_for",
]
