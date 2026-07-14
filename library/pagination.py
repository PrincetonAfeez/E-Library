"""Signed opaque cursor encoding for catalog pagination."""
from django.core import signing

CURSOR_SALT = "elibrary.catalog.cursor"


class CursorError(ValueError):
    pass


def encode_cursor(payload: dict) -> str:
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def decode_cursor(value: str | None, *, max_age: int = 60 * 60 * 24) -> dict:
    if not value:
        return {}
    try:
        payload = signing.loads(value, salt=CURSOR_SALT, max_age=max_age)
    except signing.BadSignature as exc:
        raise CursorError("Cursor is malformed or expired.") from exc
    if not isinstance(payload, dict):
        raise CursorError("Cursor payload is invalid.")
    return payload
