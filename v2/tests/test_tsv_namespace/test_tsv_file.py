"""Tests for tsv namespace TSV file reading and writing."""

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.tsv_namespace.tsv_file import FxtrTSVError, read_tsv, validate_tsv_key, write_tsv


class TestValidateTsvKey:
    """Tests for validate_tsv_key function."""

    def test_valid_simple_key(self):
        """Valid simple key should not raise."""
        validate_tsv_key("mykey")

    def test_valid_key_with_slashes(self):
        """Keys can contain slashes."""
        validate_tsv_key("path/to/key")

    def test_valid_key_with_dots(self):
        """Keys can contain dots."""
        validate_tsv_key("my.key.name")

    def test_valid_key_with_hyphens_underscores(self):
        """Keys can contain hyphens and underscores."""
        validate_tsv_key("my-key_name")

    def test_empty_key_raises(self):
        """Empty key should raise."""
        with pytest.raises(FxtrTSVError, match="cannot be empty"):
            validate_tsv_key("")

    def test_key_with_tab_raises(self):
        """Key with tab should raise."""
        with pytest.raises(FxtrTSVError, match="cannot contain tabs"):
            validate_tsv_key("key\twith\ttabs")

    def test_key_with_newline_raises(self):
        """Key with newline should raise."""
        with pytest.raises(FxtrTSVError, match="cannot contain newlines"):
            validate_tsv_key("key\nwith\nnewlines")

    def test_key_with_carriage_return_raises(self):
        """Key with carriage return should raise."""
        with pytest.raises(FxtrTSVError, match="cannot contain carriage returns"):
            validate_tsv_key("key\rwith\rreturns")

    def test_key_with_colon_raises(self):
        """Key with colon should raise (colons are for full pin keys, not TSV keys)."""
        with pytest.raises(FxtrTSVError, match="cannot contain colons"):
            validate_tsv_key("key:with:colons")


class TestReadTsv:
    """Tests for read_tsv function."""

    def test_read_nonexistent_file_returns_empty(self, tmp_path: Path):
        """Reading a nonexistent file should return empty dict."""
        result = read_tsv(tmp_path / "nonexistent.tsv")
        assert result == {}

    def test_read_empty_file_returns_empty(self, tmp_path: Path):
        """Reading an empty file should return empty dict."""
        tsv_path = tmp_path / "empty.tsv"
        tsv_path.write_text("")
        result = read_tsv(tsv_path)
        assert result == {}

    def test_read_single_entry(self, tmp_path: Path):
        """Read a TSV with a single entry."""
        tsv_path = tmp_path / "single.tsv"
        # Create a valid d-node ID
        test_id = FxtrID.from_tag_and_payload("d", b"x" * 32)
        tsv_path.write_text(f"mykey\t{test_id.id_string()}\n")

        result = read_tsv(tsv_path)

        assert len(result) == 1
        assert "mykey" in result
        assert result["mykey"] == test_id

    def test_read_multiple_entries_sorted(self, tmp_path: Path):
        """Read a TSV with multiple sorted entries."""
        tsv_path = tmp_path / "multi.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)
        id_b = FxtrID.from_tag_and_payload("d", b"b" * 32)
        id_c = FxtrID.from_tag_and_payload("d", b"c" * 32)

        # Write sorted entries
        tsv_path.write_text(
            f"alpha\t{id_a.id_string()}\n"
            f"beta\t{id_b.id_string()}\n"
            f"gamma\t{id_c.id_string()}\n"
        )

        result = read_tsv(tsv_path)

        assert len(result) == 3
        assert result["alpha"] == id_a
        assert result["beta"] == id_b
        assert result["gamma"] == id_c

    def test_read_unsorted_raises_with_details(self, tmp_path: Path):
        """Reading an unsorted TSV should raise with details about out-of-order keys."""
        tsv_path = tmp_path / "unsorted.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)
        id_b = FxtrID.from_tag_and_payload("d", b"b" * 32)

        # Write unsorted entries
        tsv_path.write_text(
            f"beta\t{id_a.id_string()}\n"
            f"alpha\t{id_b.id_string()}\n"  # alpha < beta, so this is out of order
        )

        with pytest.raises(FxtrTSVError) as exc_info:
            read_tsv(tsv_path)

        error_msg = str(exc_info.value)
        assert "not sorted" in error_msg
        assert "alpha" in error_msg
        assert "beta" in error_msg

    def test_read_multiple_unsorted_reports_all(self, tmp_path: Path):
        """Multiple unsorted keys should all be reported."""
        tsv_path = tmp_path / "unsorted.tsv"
        id_x = FxtrID.from_tag_and_payload("d", b"x" * 32)

        # Write with multiple sort violations
        tsv_path.write_text(
            f"c\t{id_x.id_string()}\n"
            f"a\t{id_x.id_string()}\n"  # a < c, out of order
            f"d\t{id_x.id_string()}\n"
            f"b\t{id_x.id_string()}\n"  # b < d, out of order
        )

        with pytest.raises(FxtrTSVError) as exc_info:
            read_tsv(tsv_path)

        error_msg = str(exc_info.value)
        assert "a" in error_msg
        assert "b" in error_msg

    def test_read_duplicate_key_raises(self, tmp_path: Path):
        """Duplicate keys should raise an error."""
        tsv_path = tmp_path / "dupe.tsv"
        id_x = FxtrID.from_tag_and_payload("d", b"x" * 32)

        tsv_path.write_text(f"mykey\t{id_x.id_string()}\n" f"mykey\t{id_x.id_string()}\n")

        with pytest.raises(FxtrTSVError, match="Duplicate key"):
            read_tsv(tsv_path)

    def test_read_invalid_column_count_raises(self, tmp_path: Path):
        """Lines without exactly 2 columns should raise."""
        tsv_path = tmp_path / "bad.tsv"
        tsv_path.write_text("just_one_column\n")

        with pytest.raises(FxtrTSVError, match="expected 2 columns"):
            read_tsv(tsv_path)

    def test_read_three_columns_raises(self, tmp_path: Path):
        """Lines with 3 columns should raise."""
        tsv_path = tmp_path / "bad.tsv"
        tsv_path.write_text("one\ttwo\tthree\n")

        with pytest.raises(FxtrTSVError, match="expected 2 columns"):
            read_tsv(tsv_path)

    def test_read_invalid_fxtr_id_raises(self, tmp_path: Path):
        """Invalid FxtrID string should raise."""
        tsv_path = tmp_path / "bad.tsv"
        tsv_path.write_text("mykey\tnot_a_valid_id\n")

        with pytest.raises(FxtrTSVError, match="Invalid FxtrID"):
            read_tsv(tsv_path)

    def test_read_skips_empty_lines(self, tmp_path: Path):
        """Empty lines should be skipped."""
        tsv_path = tmp_path / "with_blanks.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)
        id_b = FxtrID.from_tag_and_payload("d", b"b" * 32)

        tsv_path.write_text(
            f"\n" f"alpha\t{id_a.id_string()}\n" f"\n" f"beta\t{id_b.id_string()}\n" f"\n"
        )

        result = read_tsv(tsv_path)
        assert len(result) == 2

    def test_read_handles_different_line_endings(self, tmp_path: Path):
        """Should handle both Unix and Windows line endings."""
        tsv_path = tmp_path / "crlf.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)

        # Write with CRLF
        tsv_path.write_bytes(f"mykey\t{id_a.id_string()}\r\n".encode())

        result = read_tsv(tsv_path)
        assert len(result) == 1
        assert "mykey" in result

    def test_read_various_node_types(self, tmp_path: Path):
        """Should handle various FxtrID node types."""
        tsv_path = tmp_path / "types.tsv"

        # Create different node types
        d_node = FxtrID.from_tag_and_payload("d", b"d" * 32)
        n_node = FxtrID.from_tag_and_payload("n", b"n" * 32)
        here_node = FxtrID.from_tag_and_payload(".", b"")

        tsv_path.write_text(
            f"aaa\t{here_node.id_string()}\n"
            f"bbb\t{d_node.id_string()}\n"
            f"ccc\t{n_node.id_string()}\n"
        )

        result = read_tsv(tsv_path)
        assert result["aaa"] == here_node
        assert result["bbb"] == d_node
        assert result["ccc"] == n_node


class TestWriteTsv:
    """Tests for write_tsv function."""

    def test_write_empty_dict_deletes_file(self, tmp_path: Path):
        """Writing empty dict should delete existing file."""
        tsv_path = tmp_path / "to_delete.tsv"
        tsv_path.write_text("some content")
        assert tsv_path.exists()

        write_tsv(tsv_path, {})

        assert not tsv_path.exists()

    def test_write_empty_dict_nonexistent_file_ok(self, tmp_path: Path):
        """Writing empty dict to nonexistent file should not raise."""
        tsv_path = tmp_path / "nonexistent.tsv"
        write_tsv(tsv_path, {})
        assert not tsv_path.exists()

    def test_write_single_entry(self, tmp_path: Path):
        """Write a single entry."""
        tsv_path = tmp_path / "single.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)

        write_tsv(tsv_path, {"mykey": id_a})

        content = tsv_path.read_text()
        assert f"mykey\t{id_a.id_string()}\n" == content

    def test_write_sorts_entries(self, tmp_path: Path):
        """Entries should be sorted by key."""
        tsv_path = tmp_path / "sorted.tsv"
        id_x = FxtrID.from_tag_and_payload("d", b"x" * 32)

        # Pass in unsorted order
        write_tsv(tsv_path, {"gamma": id_x, "alpha": id_x, "beta": id_x})

        content = tsv_path.read_text()
        lines = content.strip().split("\n")

        # Verify sorted order
        assert lines[0].startswith("alpha\t")
        assert lines[1].startswith("beta\t")
        assert lines[2].startswith("gamma\t")

    def test_write_creates_parent_directories(self, tmp_path: Path):
        """Parent directories should be created if they don't exist."""
        tsv_path = tmp_path / "nested" / "deeply" / "test.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)

        write_tsv(tsv_path, {"mykey": id_a})

        assert tsv_path.exists()
        assert tsv_path.read_text() == f"mykey\t{id_a.id_string()}\n"

    def test_write_invalid_key_raises(self, tmp_path: Path):
        """Writing invalid key should raise."""
        tsv_path = tmp_path / "bad.tsv"
        id_a = FxtrID.from_tag_and_payload("d", b"a" * 32)

        with pytest.raises(FxtrTSVError, match="cannot contain colons"):
            write_tsv(tsv_path, {"key:with:colon": id_a})

    def test_write_roundtrip(self, tmp_path: Path):
        """Write then read should return same data."""
        tsv_path = tmp_path / "roundtrip.tsv"
        original = {
            "alpha": FxtrID.from_tag_and_payload("d", b"a" * 32),
            "beta": FxtrID.from_tag_and_payload("n", b"b" * 32),
            "gamma": FxtrID.from_tag_and_payload("d", b"c" * 32),
        }

        write_tsv(tsv_path, original)
        result = read_tsv(tsv_path)

        assert result == original

    def test_write_overwrites_existing(self, tmp_path: Path):
        """Writing should overwrite existing file."""
        tsv_path = tmp_path / "overwrite.tsv"
        id_old = FxtrID.from_tag_and_payload("d", b"o" * 32)
        id_new = FxtrID.from_tag_and_payload("d", b"n" * 32)

        write_tsv(tsv_path, {"old_key": id_old})
        write_tsv(tsv_path, {"new_key": id_new})

        result = read_tsv(tsv_path)
        assert "new_key" in result
        assert "old_key" not in result

    def test_write_atomic_no_partial_on_error(self, tmp_path: Path):
        """If write fails, original file should be unchanged."""
        tsv_path = tmp_path / "atomic.tsv"
        id_orig = FxtrID.from_tag_and_payload("d", b"o" * 32)
        id_bad = FxtrID.from_tag_and_payload("d", b"b" * 32)

        # Write initial content
        write_tsv(tsv_path, {"original": id_orig})
        original_content = tsv_path.read_text()

        # Try to write with invalid key - should fail
        with pytest.raises(FxtrTSVError):
            write_tsv(tsv_path, {"bad\tkey": id_bad})

        # Original file should be unchanged
        assert tsv_path.read_text() == original_content
