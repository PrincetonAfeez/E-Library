"""Tests for discovery-quality search (Increment 11): autocomplete, did-you-mean, semantic."""

import pytest
from rest_framework.test import APIClient

from library import search
from library.models import (
    Author,
    Branch,
    Copy,
    Edition,
    Organization,
    Work,
)
from library.selectors import search_catalog
from library.services import rebuild_work_search_document

pytestmark = pytest.mark.django_db(transaction=True)


def make_org():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    return org, branch


def add_work(org, branch, *, title, slug, summary, author, isbn):
    work = Work.objects.create(canonical_title=title, slug=slug, summary=summary)
    author_obj, _ = Author.objects.get_or_create(name=author)
    work.authors.add(author_obj)
    edition = Edition.objects.create(work=work, isbn_13=isbn)
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=f"B{slug}")
    rebuild_work_search_document(work.pk)
    return work


def build_catalog():
    org, branch = make_org()
    a = add_work(
        org, branch, title="Neuromancer", slug="neuromancer",
        summary="A cyberpunk hacker jacks into cyberspace.", author="William Gibson",
        isbn="9780000000001",
    )
    b = add_work(
        org, branch, title="The Vegetable Garden", slug="veg-garden",
        summary="How to grow vegetables and flowers in your garden.", author="Jane Green",
        isbn="9780000000002",
    )
    c = add_work(
        org, branch, title="Snow Crash", slug="snow-crash",
        summary="A cyberpunk hacker adventure in the metaverse.", author="Neal Stephenson",
        isbn="9780000000003",
    )
    return org, branch, a, b, c


def _api():
    client = APIClient()
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Embedding
# --------------------------------------------------------------------------- #
def test_embed_text_is_deterministic_and_normalized():
    v1 = search.embed_text("Café Brontë")
    v2 = search.embed_text("cafe bronte")  # accent + case folded to the same tokens
    assert v1 == v2
    assert len(v1) == search.EMBED_DIM
    # L2-normalized.
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-9


def test_cosine_ranks_overlap_higher():
    q = search.embed_text("cyberpunk hacker")
    close = search.embed_text("a cyberpunk hacker story")
    far = search.embed_text("grow vegetables in the garden")
    assert search.cosine(q, close) > search.cosine(q, far)


# --------------------------------------------------------------------------- #
# Autocomplete
# --------------------------------------------------------------------------- #
def test_autocomplete_titles_and_authors():
    org, branch, a, b, c = build_catalog()
    titles = search.autocomplete(org, "neu")
    assert any(s["type"] == "title" and s["value"] == "Neuromancer" for s in titles)

    authors = search.autocomplete(org, "gib")
    assert any(s["type"] == "author" and "Gibson" in s["value"] for s in authors)

    # Too-short prefixes return nothing.
    assert search.autocomplete(org, "n") == []


# --------------------------------------------------------------------------- #
# Did-you-mean
# --------------------------------------------------------------------------- #
def test_did_you_mean_corrects_typo():
    org, branch, a, b, c = build_catalog()
    assert search.did_you_mean(org, "Neuromacer") == "Neuromancer"
    # A correctly spelled title is not "corrected".
    assert search.did_you_mean(org, "Neuromancer") is None


def test_search_catalog_surfaces_suggestion():
    org, branch, a, b, c = build_catalog()
    page = search_catalog(organization=org, query="Neuromacer", log=False)
    assert page.did_you_mean == "Neuromancer"


# --------------------------------------------------------------------------- #
# Semantic search
# --------------------------------------------------------------------------- #
def test_semantic_search_ranks_by_meaning():
    org, branch, a, b, c = build_catalog()
    results = search.semantic_search(org, "cyberpunk hacker in cyberspace", limit=3)
    assert results, "expected semantic matches"
    top_titles = [w.canonical_title for w in results[:2]]
    # The two cyberpunk titles outrank the gardening book.
    assert "Neuromancer" in top_titles or "Snow Crash" in top_titles
    assert results[0].canonical_title != "The Vegetable Garden"


def test_similar_works_more_like_this():
    org, branch, a, b, c = build_catalog()
    similar = search.similar_works(a, limit=3)
    titles = [w.canonical_title for w in similar]
    # Snow Crash (also cyberpunk/hacker) is the nearest neighbour to Neuromancer.
    assert "Snow Crash" in titles
    assert "Neuromancer" not in titles  # never returns itself


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def test_suggest_and_semantic_api():
    org, branch, a, b, c = build_catalog()
    client = _api()

    resp = client.get("/api/v1/catalog/suggest/?q=snow&org=lib", secure=True)
    assert resp.status_code == 200
    assert any(item["value"] == "Snow Crash" for item in resp.json()["data"])

    resp = client.get("/api/v1/catalog/semantic/?q=cyberpunk+hacker&org=lib", secure=True)
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1

    resp = client.get("/api/v1/catalog/search/?q=Neuromacer&org=lib", secure=True)
    assert resp.status_code == 200
    assert resp.json()["did_you_mean"] == "Neuromancer"
