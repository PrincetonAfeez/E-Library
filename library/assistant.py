"""AI-native layer: recommendations, cataloging assist, and NL query parsing.

Like the billing/notification gateways, the AI capability is behind a provider
abstraction. The default :class:`LocalProvider` is fully deterministic and
offline — recommendations reuse the local semantic embeddings (see
:mod:`library.search`), and cataloging/query helpers use transparent heuristics.
A real LLM-backed provider can be swapped in behind the same interface.
"""

from __future__ import annotations

import re
from collections import Counter

from django.conf import settings
from django.db.models import Count, Q

from . import search
from .models import (
    Loan,
    LoanStatus,
    Work,
    WorkSearchDocument,
)

_STOPWORDS = {
    "this", "that", "with", "from", "have", "will", "your", "about", "into", "them",
    "they", "then", "than", "when", "what", "which", "were", "been", "book", "books",
    "novel", "story", "author", "read", "reading", "available", "show", "find", "want",
    "looking", "please", "some", "more", "like", "only",
}


def _popular_works(organization, *, limit: int) -> list[Work]:
    ids = (
        Work.objects.filter(editions__copies__organization=organization)
        .annotate(
            n=Count(
                "editions__copies__loans",
                # Scope to this tenant's loans — Works are a shared spine, so an
                # unfiltered Count would leak other tenants' circulation.
                filter=Q(editions__copies__loans__organization=organization),
                distinct=True,
            )
        )
        .order_by("-n", "canonical_title")
        .values_list("pk", flat=True)[:limit]
    )
    by_id = {w.pk: w for w in Work.objects.filter(pk__in=list(ids)).prefetch_related("authors")}
    return [by_id[i] for i in ids if i in by_id]


class LocalProvider:
    """Deterministic, offline AI provider."""

    name = "local"

    # ----- Recommendations ----- #
    def recommend_for_patron(self, patron, *, limit: int = 6) -> list[Work]:
        organization = patron.organization
        seed_ids = set(
            Loan.objects.filter(patron=patron)
            .exclude(status=LoanStatus.LOST)
            .values_list("copy__edition__work_id", flat=True)
        )
        seed_embeddings = [
            emb
            for emb in WorkSearchDocument.objects.filter(
                work_id__in=seed_ids, embedding__isnull=False
            ).values_list("embedding", flat=True)
            if emb
        ]
        if not seed_embeddings:
            return _popular_works(organization, limit=limit)

        dim = len(seed_embeddings[0])
        centroid = [sum(vec[i] for vec in seed_embeddings) / len(seed_embeddings) for i in range(dim)]
        scored = [
            (work_id, search.cosine(centroid, emb))
            for work_id, emb in search._embedding_candidates(organization)
            if work_id not in seed_ids
        ]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top_ids = [work_id for work_id, _ in scored[:limit]]
        by_id = {
            w.pk: w
            for w in Work.objects.filter(pk__in=top_ids).prefetch_related("authors")
        }
        return [by_id[i] for i in top_ids if i in by_id]

    # ----- Cataloging assist ----- #
    def suggest_keywords(self, text: str, *, k: int = 6) -> list[str]:
        words = [w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOPWORDS]
        return [word for word, _ in Counter(words).most_common(k)]

    def summarize(self, text: str, *, sentences: int = 2) -> str:
        parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
        return " ".join(p for p in parts[:sentences] if p)

    def reading_level(self, text: str) -> float:
        words = re.findall(r"[A-Za-z]+", text or "")
        if not words:
            return 0.0
        return round(sum(len(w) for w in words) / len(words), 2)

    def catalog_assist(self, *, text: str) -> dict:
        return {
            "keywords": self.suggest_keywords(text),
            "summary": self.summarize(text),
            "reading_level": self.reading_level(text),
        }

    # ----- Natural-language query parsing ----- #
    def parse_query(self, text: str) -> dict:
        lowered = (text or "").lower()
        filters = {}
        if re.search(r"\bavailable\b|\bin stock\b|\bon shelf\b", lowered):
            filters["availability"] = "available"
        cleaned = re.sub(
            r"\b(available|in stock|on shelf|books?|novels?|show me|find|please|the|a|an|"
            r"with|that are|i want|looking for|about|for)\b",
            " ",
            lowered,
        )
        return {"q": " ".join(cleaned.split()).strip(), "filters": filters}


def get_provider():
    """Return the configured AI provider (local by default)."""
    configured = getattr(settings, "AI_PROVIDER", "") or "local"
    # Only the local provider ships; a hook point for a real LLM provider.
    if configured != "local":  # pragma: no cover - external provider not bundled
        pass
    return LocalProvider()


# Convenience module-level wrappers.
def recommend_for_patron(patron, *, limit: int = 6) -> list[Work]:
    return get_provider().recommend_for_patron(patron, limit=limit)


def catalog_assist(*, text: str) -> dict:
    return get_provider().catalog_assist(text=text)


def parse_query(text: str) -> dict:
    return get_provider().parse_query(text)
