import pytest


@pytest.fixture(autouse=True)
def _isolate_cache(settings):
    """Use a hermetic per-process cache and clear it around each test.

    Tests must not depend on a running Redis; locmem gives each run an isolated
    cache. Clearing around every test also stops rate-limiter counters from
    bleeding across tests (a later test tripping a limit an earlier one used).
    """
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "elibrary-tests",
        }
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    """Use non-manifest static storage in tests.

    Production uses WhiteNoise's CompressedManifestStaticFilesStorage, which
    requires a `collectstatic` manifest. Template-rendering tests have no
    manifest, so swap in the plain backend to resolve {% static %} URLs.
    """
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
