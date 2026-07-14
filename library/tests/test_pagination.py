"""Tests for signed catalog cursor encoding and validation."""
import pytest

from library.pagination import CursorError, decode_cursor, encode_cursor


def test_cursor_round_trip():
    payload = {"query": "archive", "filters": {"branch": "downtown"}, "page": 2}
    assert decode_cursor(encode_cursor(payload)) == payload


def test_cursor_rejects_tampering():
    cursor = encode_cursor({"page": 1}) + "x"
    with pytest.raises(CursorError):
        decode_cursor(cursor)
