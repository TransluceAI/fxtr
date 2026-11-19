"""The shared SSE frame parser and JSON helper the dialect codecs decode streams with."""

import pytest

from behaviors.implementations.dialects.codec_utils import SseFrameParser, decode_json_object
from behaviors.interfaces.dialect_codecs import (
    CodecError,
)


def test_frames_split_on_blank_lines_and_join_data_lines() -> None:
    parser = SseFrameParser()
    assert parser.feed(b"event: a\ndata: 1\ndata: 2\n\n: comment\nid: 7\ndata: x\n\n") == [
        ("a", "1\n2"),
        (None, "x"),
    ]


def test_frames_span_chunks_and_accept_crlf_and_cr_line_endings() -> None:
    parser = SseFrameParser()
    assert parser.feed(b'event: a\r\ndata: {"k":') == []
    assert parser.feed(b" 1}\r\n\r\ndata: b\rdata: c\n\n") == [("a", '{"k": 1}'), (None, "b\nc")]


def test_frame_boundaries_in_every_line_ending_style() -> None:
    # A blank line is any line ending directly followed by another, so CR-only streams and
    # mixed endings delimit frames too.
    assert SseFrameParser().feed(b"data: a\r\rdata: b\r\r") == [(None, "a"), (None, "b")]
    assert SseFrameParser().feed(b"data: a\n\r\ndata: b\r\n\n") == [(None, "a"), (None, "b")]
    # A CR-only boundary split across chunks.
    parser = SseFrameParser()
    assert parser.feed(b"data: a\r") == []
    assert parser.feed(b"\rdata: b\r\r") == [(None, "a"), (None, "b")]
    # A CRLF boundary split between its CR and LF: the stray LF is an empty line, not data.
    parser = SseFrameParser()
    assert parser.feed(b"data: a\r\n\r") == [(None, "a")]
    assert parser.feed(b"\ndata: b\n\n") == [(None, "b")]


def test_unicode_line_separators_inside_data_are_kept() -> None:
    # str.splitlines would break on U+2028 (and U+0085, U+000C, ...); the wire only ends lines
    # at CR/LF, so the character must survive as data.
    parser = SseFrameParser()
    payload = '{"text": "a\u2028b\u0085c"}'
    assert parser.feed(("data: " + payload).encode() + b"\n\n") == [(None, payload)]


def test_end_of_stream_terminates_a_final_frame() -> None:
    parser = SseFrameParser()
    assert parser.feed(b"data: [DONE]\n") == []
    assert parser.finish() == [(None, "[DONE]")]
    assert parser.finish() == []
    assert SseFrameParser().finish() == []
    trailing_whitespace = SseFrameParser()
    trailing_whitespace.feed(b"\n")
    assert trailing_whitespace.finish() == []


def test_decode_json_object_rejects_what_is_not_an_object() -> None:
    assert decode_json_object('{"a": 1}', "frame") == {"a": 1}
    assert decode_json_object(b'{"a": 1}', "frame") == {"a": 1}
    with pytest.raises(CodecError, match="frame is not valid JSON"):
        decode_json_object('{"a": ', "frame")
    with pytest.raises(CodecError, match="frame is not a JSON object"):
        decode_json_object("[1]", "frame")
