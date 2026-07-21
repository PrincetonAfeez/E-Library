"""ISBN metadata enrichment from an external bibliographic source.
 
Fetches title/author/publisher/subject/cover metadata for an ISBN (default:
OpenLibrary) and fills in blanks on an Edition/Work. The HTTP fetch is injectable
(``fetch=``) so the mapping logic is unit-testable without network access.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.db import transaction

from .models import Author, Edition, Subject
from .services import audit_action

logger = logging.getLogger("library")

OPENLIBRARY_URL = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"


def _default_fetch(url: str) -> bytes:
    from urllib.parse import urlparse

    from . import circuit
    from .net import validate_outbound_url

    validate_outbound_url(url)
    with circuit.guard(urlparse(url).netloc):
        with urllib.request.urlopen(url, timeout=8) as response:  # nosec B310 - scheme validated  # noqa: S310
            return response.read()


def fetch_isbn_metadata(isbn: str, *, fetch=None) -> dict | None:
    """Return normalized metadata for an ISBN, or None on miss/error."""
    if not isbn:
        return None
    fetch = fetch or _default_fetch
    try:
        raw = fetch(OPENLIBRARY_URL.format(isbn=isbn))
        payload = json.loads(raw)
    except Exception as exc:  # network / parse errors must not crash enrichment
        logger.info("ISBN enrichment fetch failed for %s: %s", isbn, exc)
        return None
    entry = payload.get(f"ISBN:{isbn}") if isinstance(payload, dict) else None
    if not entry:
        return None
    year = ""
    for ch in str(entry.get("publish_date", "")):
        if ch.isdigit():
            year += ch
    return {
        "title": (entry.get("title") or "").strip(),
        "authors": [a.get("name", "").strip() for a in entry.get("authors", []) if a.get("name")],
        "publisher": (entry.get("publishers") or [{}])[0].get("name", "").strip(),
        "publication_year": int(year[:4]) if len(year) >= 4 else None,
        "subjects": [s.get("name", "").strip() for s in entry.get("subjects", []) if s.get("name")],
        "cover_image": (entry.get("cover") or {}).get("large", ""),
        "summary": (entry.get("notes") or entry.get("description") or "").strip()
        if isinstance(entry.get("notes") or entry.get("description"), str)
        else "",
    }


@transaction.atomic
def enrich_edition(*, edition: Edition, fetch=None, actor=None) -> bool:
    """Fill blank fields on an edition/work from external metadata. Returns True
    if anything changed."""
    meta = fetch_isbn_metadata(edition.isbn_13, fetch=fetch)
    if not meta:
        return False
    work = edition.work
    changed_fields: list[str] = []

    if not edition.publisher and meta["publisher"]:
        edition.publisher = meta["publisher"][:200]
        changed_fields.append("publisher")
    if edition.publication_year is None and meta["publication_year"]:
        edition.publication_year = meta["publication_year"]
        changed_fields.append("publication_year")
    if not edition.cover_image and meta["cover_image"]:
        edition.cover_image = meta["cover_image"]
        changed_fields.append("cover_image")
    if changed_fields:
        edition.save(update_fields=[*changed_fields, "updated_at"])

    work_changed = False
    if not work.summary and meta["summary"]:
        work.summary = meta["summary"]
        work_changed = True
    if work_changed:
        work.save(update_fields=["summary", "updated_at"])

    if not work.authors.exists() and meta["authors"]:
        for name in meta["authors"][:5]:
            author = Author.objects.filter(normalized_name=_norm(name)).first() or Author.objects.create(
                name=name
            )
            work.authors.add(author)
    if not work.subjects.exists() and meta["subjects"]:
        from django.utils.text import slugify

        for name in meta["subjects"][:8]:
            subject, _ = Subject.objects.get_or_create(slug=slugify(name)[:50], defaults={"name": name})
            work.subjects.add(subject)

    changed = bool(changed_fields) or work_changed
    if changed or meta["authors"] or meta["subjects"]:
        audit_action(action="edition.enrich", entity=edition, actor=actor, source="enrichment")
        return True
    return False


def _norm(name: str) -> str:
    return " ".join((name or "").strip().casefold().split())
