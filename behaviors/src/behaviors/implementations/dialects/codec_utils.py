"""Helpers for writing a dialect codec (`behaviors.interfaces.dialect_codecs`), behaviors' own or
another codebase's: splitting a Server-Sent Events stream into frames, parsing a JSON object body,
and reading a reasoning state as the codec's own payload type."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, cast

from behaviors.interfaces.dialect_codecs import CodecError
from fxtr.entity_defns.dataclass_bases import ConversionError, TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

logger = logging.getLogger(__name__)

_warned_undecodable: set[str] = set()


def own_reasoning_state[T: TaggedDataclass](state: DynamicData | None, cls: type[T]) -> T | None:
    """`state` as `cls`, the payload type of the codec reading it; None when it is another type,
    or carries `cls`'s tag but does not decode as it (logged once per type). Reasoning state
    crosses harnesses and storage as tagged JSON, so a codec treats a state it cannot decode as
    one that is not its own."""
    if state is None:
        return None
    try:
        return state.try_as_type(cls)
    except ConversionError as error:
        if state.type not in _warned_undecodable:
            _warned_undecodable.add(state.type)
            logger.warning("ignoring reasoning state that does not decode as its type: %s", error)
        return None


@dataclass
class SseFrameParser:
    """Incremental splitter of a Server-Sent Events byte stream into (event, data) frames.

    Frames are separated by a blank line; each `data:` line's payload is joined with newlines,
    `event:` names the frame, comment lines (`:`) and unknown fields are ignored. Bytes of a
    frame not yet terminated stay buffered across `feed` calls. Decoding the `data` text (JSON,
    a `[DONE]` sentinel) is the caller's.
    """

    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        self._buffer += chunk
        frames: list[tuple[str | None, str]] = []
        while True:
            end, skip = _frame_end(self._buffer)
            if end < 0:
                return frames
            raw = bytes(self._buffer[:end])
            del self._buffer[: end + skip]
            frame = _parse_frame(raw)
            if frame is not None:
                frames.append(frame)

    def finish(self) -> list[tuple[str | None, str]]:
        """The frame the stream ended in the middle of, if any: end of stream terminates it."""
        raw = bytes(self._buffer)
        self._buffer.clear()
        frame = _parse_frame(raw) if raw.strip() else None
        return [frame] if frame is not None else []


# A frame ends at a blank line: one line ending (CRLF, CR or LF) directly followed by another.
# The first ending is an atomic group so a CRLF is never read as CR then LF (two endings).
_FRAME_END = re.compile(rb"(?>\r\n|\r|\n)(?:\r\n|\r|\n)")


def _frame_end(buffer: bytearray) -> tuple[int, int]:
    """Index of the first blank-line frame terminator and its length, or (-1, 0).

    A CR that is really the first byte of a CRLF split across chunks can be taken as a whole
    line ending here; the LF then left at the start of the next frame is an empty line, which
    `_parse_frame` ignores, so frames still come out right.
    """
    match = _FRAME_END.search(buffer)
    if match is None:
        return -1, 0
    return match.start(), match.end() - match.start()


_LINE_END = re.compile(rb"\r\n|\r|\n")


def _parse_frame(raw: bytes) -> tuple[str | None, str] | None:
    event: str | None = None
    data_lines: list[str] = []
    # Lines end at CR, LF or CRLF on the wire and nowhere else: splitting after decoding would
    # also break on Unicode line separators (U+2028, U+0085, ...) that are legal inside the data.
    for raw_line in _LINE_END.split(raw):
        line = raw_line.decode("utf-8", errors="replace")
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
    if not data_lines:
        return None
    return event, "\n".join(data_lines)


def decode_json_object(text: str | bytes, what: str) -> dict[str, Any]:
    """`text` as a JSON object; `CodecError` (naming `what` was expected) when it is not one."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodecError(f"{what} is not valid JSON ({exc}): {text[:80]!r}") from None
    if not isinstance(parsed, dict):
        raise CodecError(f"{what} is not a JSON object: {text[:80]!r}")
    return cast("dict[str, Any]", parsed)
