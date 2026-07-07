"""Discovery-quality search: autocomplete, did-you-mean, and semantic ranking.

Semantic search normally needs a vector database (pgvector) and an embedding
model. To keep the product fully offline and deterministic in tests, this module
computes a **local hashed embedding**: an accent-folded bag-of-words hashed into
a fixed-dimensional, L2-normalized vector. It captures term overlap and supports
"more like this" / semantic re-ranking without any external service. In
production the same interface can be backed by pgvector + a real model — callers
only use ``embed_text`` and the ranking helpers.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from django.contrib.postgres.search import TrigramSimilarity
from django.db import connection

from .models import Author, Work, normalize_text

EMBED_DIM = 64
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- #
# Local embedding
# --------------------------------------------------------------------------- #
def _fold(text: str) -> str:
    """Lowercase and strip diacritics so 'Bronte' matches 'Brontë'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_fold(text))


def _stable_hash(token: str) -> int:
    # Non-cryptographic feature hash (bucketing tokens into embedding dimensions).
    # usedforsecurity=False documents intent and satisfies security scanners.
    digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big")


def embed_text(text: str, *, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic, offline sentence embedding (hashed bag-of-words + bigrams)."""
    vec = [0.0] * dim
    tokens = _tokens(text)
    for token in tokens:
        vec[_stable_hash(token) % dim] += 1.0
    # Adjacent bigrams add a little word-order signal.
    for a, b in zip(tokens, tokens[1:], strict=False):
        vec[_stable_hash(f"{a}_{b}") % dim] += 0.5
    norm = math.sqrt(sum(v * v for v in vec))
    if norm:
        vec = [v / norm for v in vec]
    return vec


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


# --------------------------------------------------------------------------- #
# Autocomplete / typeahead
# --------------------------------------------------------------------------- #
def autocomplete(organization, prefix: str, *, limit: int = 8) -> list[dict]:
    """Fast typeahead suggestions from visible titles, authors, and subjects."""
    from .selectors import base_visible_works

    normalized = normalize_text(prefix)
    if len(normalized) < 2:
        return []
    visible = base_visible_works(organization)

    suggestions: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, value: str):
        key = (kind, (value or "").casefold())
        if not value or key in seen:
            return
        seen.add(key)
        suggestions.append({"type": kind, "value": value})

    titles = (
        visible.filter(normalized_title__icontains=normalized)
        .order_by("normalized_title")
        .values_list("canonical_title", flat=True)[: limit * 2]
    )
    for title in titles:
        add("title", title)

    authors = (
        Author.objects.filter(works__in=visible.values("id"), name__icontains=prefix)
        .distinct()
        .order_by("name")
        .values_list("name", flat=True)[:limit]
    )
    for name in authors:
        add("author", name)

    return suggestions[:limit]


# --------------------------------------------------------------------------- #
# Did-you-mean
# --------------------------------------------------------------------------- #
def did_you_mean(organization, query: str, *, threshold: float = 0.3) -> str | None:
    """Suggest a spelling correction for a sparse/misspelled query via trigrams."""
    query = (query or "").strip()
    if not query or connection.vendor != "postgresql":
        return None
    from .selectors import base_visible_works

    visible = base_visible_works(organization)
    best_title = (
        visible.annotate(sim=TrigramSimilarity("normalized_title", query))
        .filter(sim__gt=threshold)
        .order_by("-sim")
        .values_list("canonical_title", "sim")
        .first()
    )
    best_author = (
        Author.objects.filter(works__in=visible.values("id"))
        .annotate(sim=TrigramSimilarity("name", query))
        .filter(sim__gt=threshold)
        .order_by("-sim")
        .values_list("name", "sim")
        .first()
    )
    candidates = [c for c in (best_title, best_author) if c]
    if not candidates:
        return None
    suggestion = max(candidates, key=lambda c: c[1])[0]
    # Don't suggest what the user effectively already typed.
    if normalize_text(suggestion) == normalize_text(query):
        return None
    return suggestion


# --------------------------------------------------------------------------- #
# Semantic ranking
# --------------------------------------------------------------------------- #
def _embedding_candidates(organization, *, cap: int = 1000):
    from .selectors import base_visible_works

    return (
        base_visible_works(organization)
        .filter(search_row__embedding__isnull=False)
        .values_list("id", "search_row__embedding")[:cap]
    )


def semantic_search(organization, query: str, *, limit: int = 20) -> list[Work]:
    """Rank visible works by semantic closeness to the query embedding."""
    query = (query or "").strip()
    if not query:
        return []
    q_vec = embed_text(query)
    scored = [
        (work_id, cosine(q_vec, embedding))
        for work_id, embedding in _embedding_candidates(organization)
    ]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top_ids = [work_id for work_id, _ in scored[:limit]]
    if not top_ids:
        return []
    works = {
        w.pk: w
        for w in Work.objects.filter(pk__in=top_ids).prefetch_related(
            "authors", "subjects", "editions"
        )
    }
    return [works[wid] for wid in top_ids if wid in works]


def similar_works(work: Work, *, limit: int = 6) -> list[Work]:
    """'More like this' — nearest neighbours to a work by embedding."""
    row = getattr(work, "search_row", None)
    if row is None or not row.embedding:
        return []
    base = row.embedding
    scored = []
    for other_id, embedding in _embedding_candidates(work_organization_hint(work)):
        if other_id == work.pk:
            continue
        scored.append((other_id, cosine(base, embedding)))
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top_ids = [wid for wid, _ in scored[:limit]]
    works = {w.pk: w for w in Work.objects.filter(pk__in=top_ids).prefetch_related("authors")}
    return [works[wid] for wid in top_ids if wid in works]


def work_organization_hint(work: Work):
    """Resolve an organization to scope 'more like this' to a tenant's catalog."""
    edition = work.editions.first()
    copy = edition.copies.first() if edition else None
    return copy.organization if copy else None
