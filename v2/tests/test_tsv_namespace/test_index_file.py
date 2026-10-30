"""Tests for index file I/O (fxtr_namespace_indexes.tsv)."""

import json
from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.tsv_namespace.index_file import INDEX_FILE_NAME, read_index_file, write_index_file


def _make_id(tag: str = "d", payload: bytes = b"test1234567890ab") -> FxtrID:
    """Create a FxtrID for testing."""
    return FxtrID.from_tag_and_payload(tag, payload)


class TestReadIndexFile:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        result = read_index_file(path)
        assert result == frozenset()

    def test_empty_file_returns_empty(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text("")
        result = read_index_file(path)
        assert result == frozenset()

    def test_whitespace_only_file_returns_empty(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text("  \n\n  \n")
        result = read_index_file(path)
        assert result == frozenset()

    def test_single_registration(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text(f'{fid.id_string()}\t""\n')
        result = read_index_file(path)
        assert result == frozenset({(fid, "")})

    def test_multiple_registrations(self, tmp_path: Path):
        fid1 = _make_id(payload=b"aaaa1234567890ab")
        fid2 = _make_id(payload=b"bbbb1234567890ab")
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text(f'{fid1.id_string()}\t""\n' f'{fid2.id_string()}\t"data/models:"\n')
        result = read_index_file(path)
        assert result == frozenset({(fid1, ""), (fid2, "data/models:")})

    def test_same_indexer_multiple_prefixes(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text(f'{fid.id_string()}\t""\n' f'{fid.id_string()}\t"data/models:"\n')
        result = read_index_file(path)
        assert result == frozenset({(fid, ""), (fid, "data/models:")})

    def test_json_encoded_prefix_with_special_chars(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        # JSON encoding preserves special characters
        prefix = 'path/with "quotes"'
        Path(path).write_text(f"{fid.id_string()}\t{json.dumps(prefix)}\n")
        result = read_index_file(path)
        assert result == frozenset({(fid, prefix)})

    def test_invalid_format_no_tab(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text("notabseparated\n")
        with pytest.raises(ValueError, match="expected"):
            read_index_file(path)

    def test_invalid_fxtr_id(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        # "X" is not a valid tag for FxtrID
        Path(path).write_text('Xbaddata\t""\n')
        with pytest.raises(ValueError, match="Invalid FxtrID"):
            read_index_file(path)

    def test_invalid_json_prefix(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text(f"{fid.id_string()}\tnot_json\n")
        with pytest.raises(ValueError, match="Invalid JSON"):
            read_index_file(path)

    def test_non_string_json_prefix(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        Path(path).write_text(f"{fid.id_string()}\t42\n")
        with pytest.raises(ValueError, match="must be a JSON string"):
            read_index_file(path)


class TestWriteIndexFile:
    def test_empty_set_deletes_file(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        # Create the file first
        Path(path).write_text("dummy\n")
        write_index_file(path, frozenset())
        assert not Path(path).exists()

    def test_empty_set_no_file_is_noop(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        write_index_file(path, frozenset())
        assert not Path(path).exists()

    def test_write_single_registration(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        write_index_file(path, frozenset({(fid, "")}))
        content = Path(path).read_text()
        assert content == f'{fid.id_string()}\t""\n'

    def test_write_multiple_registrations_sorted(self, tmp_path: Path):
        fid1 = _make_id(payload=b"aaaa1234567890ab")
        fid2 = _make_id(payload=b"bbbb1234567890ab")
        path = str(tmp_path / INDEX_FILE_NAME)
        # Write in reverse order — output should be sorted
        write_index_file(path, frozenset({(fid2, "data/models:"), (fid1, "")}))
        content = Path(path).read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2
        # fid1 < fid2 lexicographically (aaaa < bbbb)
        assert lines[0].startswith(fid1.id_string())
        assert lines[1].startswith(fid2.id_string())

    def test_same_indexer_multiple_prefixes_sorted(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        write_index_file(path, frozenset({(fid, "z_prefix:"), (fid, "")}))
        content = Path(path).read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2
        # Empty prefix "" sorts before "z_prefix:"
        assert '""\n' in lines[0] + "\n"
        assert '"z_prefix:"' in lines[1]

    def test_json_encodes_prefix(self, tmp_path: Path):
        fid = _make_id()
        path = str(tmp_path / INDEX_FILE_NAME)
        write_index_file(path, frozenset({(fid, "data/models:")}))
        content = Path(path).read_text()
        assert '"data/models:"' in content


class TestRoundtrip:
    def test_roundtrip_empty(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        original = frozenset()
        write_index_file(path, original)
        result = read_index_file(path)
        assert result == original

    def test_roundtrip_single(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        fid = _make_id()
        original = frozenset({(fid, "")})
        write_index_file(path, original)
        result = read_index_file(path)
        assert result == original

    def test_roundtrip_multiple(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        fid1 = _make_id(payload=b"aaaa1234567890ab")
        fid2 = _make_id(payload=b"bbbb1234567890ab")
        original = frozenset(
            {
                (fid1, ""),
                (fid1, "data/models:"),
                (fid2, "turns/"),
            }
        )
        write_index_file(path, original)
        result = read_index_file(path)
        assert result == original

    def test_roundtrip_special_chars_in_prefix(self, tmp_path: Path):
        path = str(tmp_path / INDEX_FILE_NAME)
        fid = _make_id()
        original = frozenset({(fid, 'path/with "quotes"')})
        write_index_file(path, original)
        result = read_index_file(path)
        assert result == original
