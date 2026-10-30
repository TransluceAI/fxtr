"""Tests for tsv namespace pin key parsing and validation."""

import pytest

from fxtr2.data.tsv_namespace.key_utils import (
    FxtrKeyError,
    make_full_key,
    parse_pin_key,
    validate_pin_key,
)


class TestValidatePinKey:
    """Tests for validate_pin_key function."""

    def test_valid_simple_key(self):
        """Simple key without colon should be valid."""
        validate_pin_key("mykey")

    def test_valid_key_with_colon(self):
        """Key with one colon should be valid."""
        validate_pin_key("data/models:encoder")

    def test_valid_nested_path(self):
        """Nested path with colon should be valid."""
        validate_pin_key("a/b/c/d:key")

    def test_empty_key_raises(self):
        """Empty key should raise."""
        with pytest.raises(FxtrKeyError, match="cannot be empty"):
            validate_pin_key("")

    def test_key_with_tab_raises(self):
        """Key with tab should raise."""
        with pytest.raises(FxtrKeyError, match="cannot contain tabs"):
            validate_pin_key("key\twith\ttab")

    def test_key_with_newline_raises(self):
        """Key with newline should raise."""
        with pytest.raises(FxtrKeyError, match="cannot contain newlines"):
            validate_pin_key("key\nwith\nnewline")

    def test_key_with_carriage_return_raises(self):
        """Key with carriage return should raise."""
        with pytest.raises(FxtrKeyError, match="cannot contain carriage returns"):
            validate_pin_key("key\rwith\rreturn")

    def test_multiple_colons_raises(self):
        """Key with more than one colon should raise."""
        with pytest.raises(FxtrKeyError, match="more than one colon"):
            validate_pin_key("a:b:c")

    def test_reserved_prefix_raises(self):
        """Using reserved prefix as path prefix should raise."""
        with pytest.raises(FxtrKeyError, match="reserved"):
            validate_pin_key("index:mykey", reserved_prefix="index")

    def test_reserved_prefix_custom(self):
        """Custom reserved prefix should be checked."""
        with pytest.raises(FxtrKeyError, match="reserved"):
            validate_pin_key("custom:mykey", reserved_prefix="custom")

    def test_reserved_prefix_different_prefix_ok(self):
        """Different path prefix should be valid."""
        validate_pin_key("data:mykey", reserved_prefix="index")

    def test_empty_reserved_prefix_allows_simple_keys(self):
        """Empty reserved prefix should work (simple keys become disallowed)."""
        # With empty reserved prefix, simple keys are not allowed
        # But colon keys are fine
        validate_pin_key("data:mykey", reserved_prefix="")

    def test_empty_path_prefix_raises(self):
        """Empty path prefix (key starting with colon) should raise."""
        with pytest.raises(FxtrKeyError, match="Path prefix cannot be empty"):
            validate_pin_key(":mykey")

    def test_empty_tsv_key_raises(self):
        """Empty key after colon should raise."""
        with pytest.raises(FxtrKeyError, match="Key after colon cannot be empty"):
            validate_pin_key("path:")


class TestParsePinKey:
    """Tests for parse_pin_key function."""

    def test_simple_key_uses_reserved_prefix(self):
        """Simple key should use reserved prefix TSV."""
        tsv_path, tsv_key = parse_pin_key("mykey", reserved_prefix="index")
        assert tsv_path == "index.fxtr.tsv"
        assert tsv_key == "mykey"

    def test_simple_key_custom_reserved_prefix(self):
        """Custom reserved prefix should be used."""
        tsv_path, tsv_key = parse_pin_key("mykey", reserved_prefix="default")
        assert tsv_path == "default.fxtr.tsv"
        assert tsv_key == "mykey"

    def test_path_prefix_single_level(self):
        """Path prefix with single level."""
        tsv_path, tsv_key = parse_pin_key("data:encoder")
        assert tsv_path == "data.fxtr.tsv"
        assert tsv_key == "encoder"

    def test_path_prefix_nested(self):
        """Path prefix with nested levels."""
        tsv_path, tsv_key = parse_pin_key("data/models:encoder")
        assert tsv_path == "data/models.fxtr.tsv"
        assert tsv_key == "encoder"

    def test_path_prefix_deeply_nested(self):
        """Path prefix with many levels."""
        tsv_path, tsv_key = parse_pin_key("a/b/c/d:key")
        assert tsv_path == "a/b/c/d.fxtr.tsv"
        assert tsv_key == "key"

    def test_simple_key_no_reserved_prefix_raises(self):
        """Simple key with empty reserved prefix should raise."""
        with pytest.raises(FxtrKeyError, match="not allowed when reserved_prefix is empty"):
            parse_pin_key("mykey", reserved_prefix="")

    def test_parse_validates_key(self):
        """parse_pin_key should validate the key."""
        with pytest.raises(FxtrKeyError, match="more than one colon"):
            parse_pin_key("a:b:c")


class TestMakeFullKey:
    """Tests for make_full_key function."""

    def test_reserved_prefix_tsv_returns_simple_key(self):
        """TSV matching reserved prefix should return simple key."""
        result = make_full_key("index.fxtr.tsv", "mykey", reserved_prefix="index")
        assert result == "mykey"

    def test_non_reserved_prefix_tsv_returns_colon_key(self):
        """TSV not matching reserved prefix should return key with colon."""
        result = make_full_key("data.fxtr.tsv", "encoder", reserved_prefix="index")
        assert result == "data:encoder"

    def test_nested_path_returns_colon_key(self):
        """Nested TSV path should return key with path prefix."""
        result = make_full_key("data/models.fxtr.tsv", "encoder", reserved_prefix="index")
        assert result == "data/models:encoder"

    def test_deeply_nested_path(self):
        """Deeply nested TSV path should work."""
        result = make_full_key("a/b/c/d.fxtr.tsv", "key", reserved_prefix="index")
        assert result == "a/b/c/d:key"

    def test_invalid_tsv_suffix_raises(self):
        """TSV path without .fxtr.tsv suffix should raise."""
        with pytest.raises(FxtrKeyError, match="must end with"):
            make_full_key("data.tsv", "key")

    def test_tsv_key_with_colon_raises(self):
        """TSV key with colon should raise."""
        with pytest.raises(FxtrKeyError, match="cannot contain colons"):
            make_full_key("data.fxtr.tsv", "key:with:colon")

    def test_roundtrip_simple_key(self):
        """parse then make should return original simple key."""
        original = "mykey"
        tsv_path, tsv_key = parse_pin_key(original, reserved_prefix="index")
        result = make_full_key(tsv_path, tsv_key, reserved_prefix="index")
        assert result == original

    def test_roundtrip_colon_key(self):
        """parse then make should return original colon key."""
        original = "data/models:encoder"
        tsv_path, tsv_key = parse_pin_key(original, reserved_prefix="index")
        result = make_full_key(tsv_path, tsv_key, reserved_prefix="index")
        assert result == original

    def test_custom_reserved_prefix(self):
        """Custom reserved prefix should work for make."""
        result = make_full_key("custom.fxtr.tsv", "mykey", reserved_prefix="custom")
        assert result == "mykey"

    def test_empty_reserved_prefix(self):
        """Empty reserved prefix should always use colon format."""
        result = make_full_key("data.fxtr.tsv", "key", reserved_prefix="")
        assert result == "data:key"


class TestKeyIntegration:
    """Integration tests for key parsing and construction."""

    def test_various_keys_roundtrip(self):
        """Various key formats should roundtrip correctly."""
        test_cases = [
            "simple",
            "with_underscore",
            "with-dash",
            "with.dot",
            "data:key",
            "models:encoder",
            "path/to/file:key",
            "a/b/c:d",
        ]

        for original in test_cases:
            tsv_path, tsv_key = parse_pin_key(original, reserved_prefix="index")
            result = make_full_key(tsv_path, tsv_key, reserved_prefix="index")
            assert result == original, f"Roundtrip failed for {original!r}"

    def test_path_normalization(self):
        """TSV paths should be relative paths."""
        tsv_path, _ = parse_pin_key("data/models:encoder")

        # Should be a relative path string, not absolute
        assert not tsv_path.startswith("/")

        # Should be normalized (no . or ..)
        assert "/.." not in tsv_path
        assert "/./" not in tsv_path
