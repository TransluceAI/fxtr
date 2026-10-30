"""Ready-made example prompt templates for the streaming prompt task.

These are plain :class:`simple_tasks.PromptTemplate` values — import one
and ``.store()`` it agent-side, then pass its ``entity_id`` as the
task's ``"prompt_template"`` config (no need to author a template from
scratch). They double as documentation of the output format the task
expects.

This module is intentionally lightweight (it imports only
``simple_tasks``, not the ``anthropic`` SDK), so it can be imported in
the run_script sandbox via ``from streaming_prompt_task_oci import
VIVID_DESCRIPTION_PROMPT`` without dragging in the container-only
service deps.

**Output-format convention** (shared with :mod:`prompt_task`): the
model's reply is split on the first newline — the **first line becomes
the output note's title**, the rest becomes its body. So a template
should instruct the model to put a short title on the first line, then
the body below.
"""

from __future__ import annotations

from simple_tasks import PromptTemplate

#: Vivid three-sentence scene description. The template asks for a short
#: title on the first line (-> note title) and three evocative sentences
#: below (-> note body), matching the first-line-title convention above.
VIVID_DESCRIPTION_PROMPT = PromptTemplate(
    label="vivid-description",
    template=(
        "Describe the scene in this note vividly.\n"
        "Format your reply as: a short title (at most 8 words) on the first "
        "line, then a blank line, then exactly three evocative sentences.\n\n"
        "Title: {title}\nBody: {body}"
    ),
)
