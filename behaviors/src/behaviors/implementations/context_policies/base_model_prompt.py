# ruff: noqa: E501 -- BASE_MODEL_PREFACE's lines are deliberately not hard-wrapped.
"""Prompt rendering for the base-model user simulator.

The simulated user is a base (non-chat) model continuing a document: an
optional preface framing what follows as supplemental data for a report on
user simulation; for each of a few reference users, a ``<user_bio>`` vignette
and one example ``<transcript>`` in ChatML; then the target user's vignette and
the opening of their transcript, continued by the conversation so far. Sampled
up to the next ChatML turn boundary, the model's completion is the user's next
message, and a completion of ``</transcript>`` means the user stops chatting.

The ``user_id`` / ``transcript_id`` tag attributes are caller-supplied
identifiers rather than generated here, so a caller can reproduce a prompt
byte-for-byte from stored configuration.
"""

import dataclasses
from collections.abc import Iterable
from typing import Literal

from behaviors.implementations.context_policies._shared import END_TRANSCRIPT_MARKER, message_text
from behaviors.types.message import ChatMessage
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass

CHATML_USER_START = "<|im_start|>user\n"
CHATML_ASSISTANT_START = "<|im_start|>assistant\n"
CHATML_END = "<|im_end|>"

__all__ = [
    "BASE_MODEL_PREFACE",
    "CHATML_ASSISTANT_START",
    "CHATML_END",
    "CHATML_USER_START",
    "END_TRANSCRIPT_MARKER",
    "ExampleTranscript",
    "ExampleTranscriptMessage",
    "ReferenceUser",
    "SimulatedUserProfile",
    "default_chatml_stop_sequences",
    "render_base_model_prompt",
    "render_chatml_transcript",
]

# A hand-written preamble, prepended to the base-model prompt when ``use_preface`` is set, that
# frames the subsequent bios+transcripts as supplemental data for a (fictitious) report on user
# simulation: real production users, each with an auto-generated biographical vignette. The intent
# is to answer "what is this text?" up front -- establishing the human-authored genre and explaining
# the ChatML / <user_bio> / <transcript> scaffolding -- so the base model continues as a real person
# rather than extending an ambiguous, faintly assistant-flavored document. Adapted from the Mental
# Health pilot's preface, generalized away from that domain. Lines are intentionally not
# hard-wrapped (the base model sees free-flowing text elsewhere); the backslashes only keep this
# source readable.
BASE_MODEL_PREFACE = """\
# Supplemental Data for "Privacy-Preserving Simulation of Real User Conversations from Synthetic Biographical Vignettes"

<supplement name="supplement_A">
<preface>
## Supplement A: Biographical vignettes and source transcripts for selected users

The following collection contains transcripts of real conversations between users of a chat application and its AI assistant, drawn from production traffic across a range of everyday topics.

For each user represented in the data, we include a biographical vignette (see main text) generated from that user's conversation using the methodology presented in the main text. These model-written vignettes support, among other uses:

- Synthetic generation of new user messages in the voice of the original user (see Supplement B)
- Unsupervised discovery of user traits and behaviors (Appendix D)

This supplement demonstrates only the **first** step of our pipeline: generating a biographical vignette for a real user from one of their transcripts. The second step -- simulating a user from a vignette -- is demonstrated in Supplement B.

## Transcript format

Transcripts are presented in ChatML. To recap, the format is:

```
<|im_start|>user
[the user's message]<|im_end|>
<|im_start|>assistant
[the assistant's reply]<|im_end|>
```

...and so on, alternating. Each transcript is wrapped in <transcript>...</transcript>. We include an empty user message at the end of each transcript, which is how the end of a conversation is represented in the production database the transcripts were pulled from. These empty messages are presented **without** the <|im_end|> separator that is ordinarily used at the end of each message, so that the final lines of each transcript read exactly as follows:

```
<|im_start|>user
</transcript>
```

## Vignette format

Each vignette is wrapped in <user_bio>...</user_bio>. We include one vignette and one source transcript per user, with the vignette placed immediately **before** the transcript of the user it describes. Each vignette and transcript carries a unique ID identifying the user involved:

```
<user_bio user_id="unique_id_for_user_1">
[ ... vignette describing user 1 ... ]
</user_bio>

<transcript transcript_id="unique_id_for_transcript_1" user_id="unique_id_for_user_1">
[ ... conversation between user 1 and the assistant ... ]
</transcript>
```

...and so on.

</preface>
<supplement_data>
"""


@dataclasses.dataclass(frozen=True)
class ExampleTranscriptMessage(
    TaggedDataclass, fxtr_type=behaviors_type("ExampleTranscriptMessage.v1")
):
    """One message of a reference user's example transcript."""

    role: Literal["user", "assistant"]
    content: str


@dataclasses.dataclass(frozen=True)
class ExampleTranscript(TaggedDataclass, fxtr_type=behaviors_type("ExampleTranscript.v1")):
    """A reference user's example conversation, shown to the base model as a few-shot example.

    Attributes:
        transcript_id: Identifier rendered on the ``<transcript>`` tag.
        messages: The conversation, alternating user and assistant.
    """

    transcript_id: str
    messages: tuple[ExampleTranscriptMessage, ...]


@dataclasses.dataclass(frozen=True)
class SimulatedUserProfile(TaggedDataclass, fxtr_type=behaviors_type("SimulatedUserProfile.v1")):
    """The user the base model simulates.

    Attributes:
        user_id: Identifier rendered on the user's ``<user_bio>`` and ``<transcript>`` tags.
        transcript_id: Identifier rendered on the tag opening the simulated conversation.
        user_bio: Biographical vignette describing the user, in prose.
        initial_user_message: A fixed first message, sent instead of sampling one.
    """

    user_id: str
    transcript_id: str
    user_bio: str
    initial_user_message: str | None = None


@dataclasses.dataclass(frozen=True)
class ReferenceUser(TaggedDataclass, fxtr_type=behaviors_type("ReferenceUser.v1")):
    """A few-shot reference user: a vignette and, optionally, one example transcript.

    Attributes:
        user_id: Identifier rendered on the ``<user_bio>`` and example ``<transcript>`` tags.
        user_bio: Biographical vignette describing the user, in prose.
        example_transcript: The example conversation, if any.
    """

    user_id: str
    user_bio: str
    example_transcript: ExampleTranscript | None = None


def default_chatml_stop_sequences() -> tuple[str, ...]:
    """Stop strings for generating the next user turn from a ChatML transcript prompt."""
    return (CHATML_END, CHATML_ASSISTANT_START)


def render_base_model_prompt(
    *,
    profile: SimulatedUserProfile,
    transcript: tuple[ChatMessage, ...] = (),
    references: tuple[ReferenceUser, ...] = (),
    use_preface: bool = True,
) -> str:
    """Render the base-model prompt for the simulated user's next message.

    When ``use_preface`` is set, :data:`BASE_MODEL_PREFACE` is prepended ahead of the
    reference/target material to frame the document genre (see that constant).
    """
    sections: list[str] = []
    if use_preface:
        sections.append(BASE_MODEL_PREFACE)
    for reference in references:
        sections.append(_render_user_bio(reference.user_bio, user_id=reference.user_id))
        if reference.example_transcript is not None:
            sections.append(
                _render_example_transcript(reference.example_transcript, user_id=reference.user_id)
            )
    sections.append(_render_user_bio(profile.user_bio, user_id=profile.user_id))
    sections.append(
        f'<transcript transcript_id="{profile.transcript_id}" user_id="{profile.user_id}">'
    )
    sections.append(render_chatml_transcript(transcript))
    return "\n".join(sections).rstrip() + "\n"


def render_chatml_transcript(messages: tuple[ChatMessage, ...]) -> str:
    """Render messages in ChatML, ending with the opening of the next user turn.

    Each message contributes its text (see ``message_text``).
    """
    return _render_chatml((message.role, message_text(message)) for message in messages)


def _render_chatml(turns: Iterable[tuple[str, str]]) -> str:
    rendered = [f"<|im_start|>{role}\n{content}{CHATML_END}" for role, content in turns]
    rendered.append(CHATML_USER_START)
    return "\n".join(rendered)


def _render_user_bio(user_bio: str, *, user_id: str) -> str:
    return "\n".join((f'<user_bio user_id="{user_id}">', user_bio.strip(), "</user_bio>"))


def _render_example_transcript(example: ExampleTranscript, *, user_id: str) -> str:
    return "\n".join(
        (
            f'<transcript transcript_id="{example.transcript_id}" user_id="{user_id}">',
            _render_chatml((message.role, message.content) for message in example.messages),
            "</transcript>",
        )
    )
