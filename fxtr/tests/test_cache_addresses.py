"""Tests for cache keys read back into parts: parsing, patterns, summaries, and the prefix test."""

from __future__ import annotations

import pytest

from fxtr.experiment.workflows import append_mapped_key, join_path
from fxtr.runner.cache_addresses import (
    AddressPattern,
    PatternError,
    Slot,
    parse_address,
    parse_pattern,
    summarize,
    under_prefix,
)


def test_a_key_parses_into_literal_text_and_groups_and_renders_back() -> None:
    key = join_path(append_mapped_key("root/screen", {"applicant": "ada", "n": 3}), "judge")
    parsed = parse_address(key)
    assert parsed.parts == (
        "root/screen",
        (Slot("applicant", '"ada"'), Slot("n", "3")),
        "/judge",
    )
    assert str(parsed) == key
    assert parsed.shape == ("root/screen", ("applicant", "n"), "/judge")
    assert parsed.literal_prefix == "root/screen"


def test_values_may_hold_the_characters_that_delimit_keys() -> None:
    value = 'a/b[c:"d"],e]'
    key = append_mapped_key("root/s", {"text": value, "k": 1})
    parsed = parse_address(key)
    assert parsed.parts[1] == (Slot("text", '"a/b[c:\\"d\\"],e]"'), Slot("k", "1"))
    assert str(parsed) == key


def test_a_bracket_that_starts_no_group_is_literal() -> None:
    for text in ["root/a[b", "root/[x]/y", "root/a[k:nope]", "root/a[k:1"]:
        parsed = parse_address(text)
        assert parsed.parts == (text,) and str(parsed) == text


def test_a_mapped_key_and_a_replica_key_are_two_groups() -> None:
    key = append_mapped_key(append_mapped_key("root/sample", {"prompt": 3}), {"rep": 0})
    assert parse_address(key).shape == ("root/sample", ("prompt",), ("rep",))


def test_a_pattern_matches_keys_of_its_shape_at_its_values() -> None:
    pattern = parse_pattern('root/screen[applicant:*,judge:"v2"]/pangram')
    assert pattern.parts[1] == (Slot("applicant", None), Slot("judge", '"v2"'))
    assert pattern.literal_prefix == "root/screen"
    assert pattern.matches('root/screen[applicant:"ada",judge:"v2"]/pangram')
    assert pattern.matches('root/screen[applicant:7,judge:"v2"]/pangram')
    assert not pattern.matches('root/screen[applicant:"ada",judge:"v1"]/pangram')
    assert not pattern.matches('root/screen[applicant:"ada",judge:"v2"]/pangram/more')
    assert not pattern.matches('root/screen[applicant:"ada"]/pangram')
    # A key is a pattern without wildcards, and a "*" is a wildcard only in a pattern.
    assert parse_address('root/a[k:"*"]').matches('root/a[k:"*"]')
    assert parse_address("root/a[k:*]").parts == ("root/a[k:*]",)


def test_summarize_keeps_shared_values_and_stars_the_rest() -> None:
    keys = [
        'root/screen[applicant:"ada",judge:"v2"]/pangram',
        'root/screen[applicant:"bo",judge:"v2"]/pangram',
        "root/rank",
        'root/screen[applicant:"cy",judge:"v2"]/pangram',
    ]
    summary = summarize(keys)
    assert [(str(pattern), members) for pattern, members in summary] == [
        ('root/screen[applicant:*,judge:"v2"]/pangram', [keys[0], keys[1], keys[3]]),
        ("root/rank", ["root/rank"]),
    ]
    assert all(pattern.matches(key) for pattern, members in summary for key in members)
    # A group of one key is that key.
    ((single, _),) = summarize(keys[:1])
    assert single == parse_address(keys[0]) and isinstance(single, AddressPattern)


def test_a_pattern_selects_keys_at_or_under_its_matches() -> None:
    pattern = parse_pattern("root/sample[prompt:*]")
    for value in ["a", 3, -1.5, True, None, "x/y[z]"]:
        key = append_mapped_key("root/sample", {"prompt": value})
        assert pattern.selects(key)
        assert pattern.selects(key + "/judge")
        assert pattern.selects(append_mapped_key(key, {"rep": 0}))  # a replica group follows
    assert not pattern.selects("root/sample")
    assert not pattern.selects('root/samples[prompt:"a"]')
    # The dims are given in full, in the order of the key.
    assert not pattern.selects('root/sample[prompt:"a",n:1]')
    assert not parse_pattern("root/sample[n:*,prompt:*]").selects('root/sample[prompt:"a",n:1]')
    # After a group, literal text matches at a path boundary, like a prefix.
    after = parse_pattern("root/screen[applicant:*]/pang")
    assert not after.selects('root/screen[applicant:"ada"]/pangram')
    assert parse_pattern("root/screen[applicant:*]/pangram").selects(
        'root/screen[applicant:"ada"]/pangram/score'
    )
    # A pattern without wildcards selects as its text does as a prefix.
    exact = parse_pattern('root/screen[applicant:"ada"]')
    assert exact.selects('root/screen[applicant:"ada"]/pangram')
    assert not exact.selects('root/screen[applicant:"bo"]/pangram')


def test_a_star_outside_a_value_is_refused() -> None:
    for text in ["root/*", "root/s*[k:1]", "root/s[k:*", "root/s[*:1]"]:
        with pytest.raises(PatternError, match="a \\* stands only for a whole mapped value"):
            parse_pattern(text)
    # A star inside a JSON string is part of the value.
    assert parse_pattern('root/s[k:"*"]').selects('root/s[k:"*"]/t')


def test_under_prefix_breaks_at_path_and_mapped_key_boundaries() -> None:
    assert under_prefix('root/refine[bio:"ada"]/rewrite', "root/refine")
    assert under_prefix("root/refine/rewrite", "root/refine")
    assert under_prefix("root/refine", "root/refine")
    assert under_prefix("anything", "")
    assert not under_prefix("root/refined", "root/refine")
