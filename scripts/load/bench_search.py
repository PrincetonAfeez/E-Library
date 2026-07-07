"""Core-path capacity benchmark for catalog search.

Measures the latency of the search query path (FTS + trigram + facets +
serialization) directly against a seeded PostgreSQL database — the work that
actually determines read capacity — without dev-server / throttle noise.

Usage (seed first, then run):
    DATABASE_URL=postgres://.../elibrary_load python manage.py migrate
    DATABASE_URL=... python manage.py seed_demo --works 200
    DATABASE_URL=... CACHE_URL=locmem:// python scripts/load/bench_search.py
"""

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "elibrary.settings")
django.setup()

from library.models import Organization, Work  # noqa: E402
from library.selectors import search_catalog  # noqa: E402

TERMS = ["", "history", "science", "the", "world", "art", "life", "space", "garden", "river"]
N = int(os.environ.get("BENCH_N", "500"))
CONCURRENCY = int(os.environ.get("BENCH_CONC", "8"))


def _percentile(values, pct):
    values = sorted(values)
    if not values:
        return 0.0
    k = (len(values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _one(_ignored):
    org = Organization.objects.filter(active=True).order_by("id").first()
    query = random.choice(TERMS)  # noqa: S311 - benchmark input, not security
    start = time.perf_counter()
    page = search_catalog(organization=org, query=query, per_page=20, log=False)
    list(page.results)  # force queryset evaluation
    return (time.perf_counter() - start) * 1000.0


def main():
    works = Work.objects.count()
    for _ in range(20):  # warm caches / connections
        _one(None)

    single = [_one(None) for _ in range(N)]

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        concurrent = list(pool.map(_one, range(N)))
    throughput = N / (time.perf_counter() - start)

    print(f"dataset: works={works}  requests={N}  concurrency={CONCURRENCY}")
    for label, data in (("single-thread", single), (f"concurrent x{CONCURRENCY}", concurrent)):
        print(
            f"  {label:<16} p50={_percentile(data,50):6.1f}ms "
            f"p95={_percentile(data,95):6.1f}ms p99={_percentile(data,99):6.1f}ms"
        )
    print(f"  throughput      ~{throughput:5.0f} searches/s @ {CONCURRENCY} workers")


if __name__ == "__main__":
    main()
