"""Django settings for the E-Library project."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
)

env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DEBUG", default=False)
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "csp",
    "library",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "library.middleware.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "library.middleware.StaffMfaMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
]

ROOT_URLCONF = "elibrary.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "library.context_processors.current_organization",
            ],
        },
    }
]

WSGI_APPLICATION = "elibrary.wsgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://elibrary:elibrary@localhost:5432/elibrary",
    )
}
# Reuse DB connections across requests (persistent pooling) and verify liveness
# before reuse. Set CONN_MAX_AGE=0 behind an external pooler (PgBouncer).
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

CACHES = {
    "default": env.cache("CACHE_URL", default="redis://localhost:6379/0"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = env("LANGUAGE_CODE", default="en-us")
LANGUAGES = [
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Django >= 5.1 removed STATICFILES_STORAGE/DEFAULT_FILE_STORAGE in favour of the
# STORAGES setting; using the old name here would silently disable WhiteNoise's
# manifest/compression.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "catalog_search"
LOGOUT_REDIRECT_URL = "catalog_search"

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="library@example.test")

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "library.auth.ScopedTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    # Env-configurable so a staging/load-test environment can raise them (or a
    # high-traffic tenant can be tuned) without a code change.
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", default="120/minute"),
        "user": env("THROTTLE_USER", default="600/minute"),
    },
    # The browsable API is a convenience for development only; in production it
    # is an unnecessary surface, so ship JSON-only unless DEBUG is on.
    "DEFAULT_RENDERER_CLASSES": (
        [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
        if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "E-Library API",
    "DESCRIPTION": "Catalog discovery, circulation, and librarian operations.",
    "VERSION": "1.0.0",
}

# Number of trusted reverse proxies in front of the app. Controls how the rate
# limiter derives the client IP from X-Forwarded-For (0 = use REMOTE_ADDR only).
RATELIMIT_TRUSTED_PROXY_COUNT = env.int("RATELIMIT_TRUSTED_PROXY_COUNT", default=0)

# PostgreSQL full-text search configuration (language dictionary).
SEARCH_CONFIG = env("SEARCH_CONFIG", default="english")

# Static default feature flags. DB-backed FeatureFlag rows (global or per-org)
# override these at runtime; see library/flags.py.
FEATURE_FLAGS: dict = {}

# Billing (Stripe). Left blank -> the manual no-op gateway is used, so the app
# runs fully without a payment provider in dev/test.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# SMS (Twilio). Blank -> the manual SMS backend (records/logs) is used.
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", default="")
TWILIO_FROM_NUMBER = env("TWILIO_FROM_NUMBER", default="")

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ("'self'",),
        "script-src": ("'self'", "https://unpkg.com"),
        "style-src": ("'self'", "'unsafe-inline'"),
        "img-src": ("'self'", "data:", "https:"),
    }
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
# The CSRF token is delivered via the {% csrf_token %} hidden input / HTMX form
# submissions, so the cookie itself need not be readable by JavaScript.
CSRF_COOKIE_HTTPONLY = True

# HTTPS enforcement is independent of DEBUG so a hardened, non-debug deployment
# can still run behind plain HTTP locally (e.g. the demo compose stack) without
# an unreachable redirect loop.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=not DEBUG)
# Never redirect the health/readiness checks to HTTPS, or HTTP probes 301.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^readyz/$"]

if SECURE_SSL_REDIRECT:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

LOG_LEVEL = env("LOG_LEVEL", default="INFO")
# JSON in deployed envs (structured + queryable); plain text locally for readability.
LOG_FORMAT = env("LOG_FORMAT", default="text" if DEBUG else "json")
# Log SQL slower than this many milliseconds (0 disables). Surfaces slow queries
# without a separate APM. Effective only when the DB logger is at DEBUG.
SLOW_QUERY_MS = env.int("SLOW_QUERY_MS", default=0)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "library.logging_utils.RequestIDFilter"},
    },
    "formatters": {
        "text": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"},
        "json": {"()": "library.logging_utils.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "library": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

if SLOW_QUERY_MS > 0:
    # Only instrument SQL timing when explicitly enabled (adds per-query cost).
    LOGGING["filters"]["slow_query"] = {
        "()": "library.logging_utils.SlowQueryFilter",
        "threshold_ms": SLOW_QUERY_MS,
    }
    LOGGING["handlers"]["slow_query"] = {
        "class": "logging.StreamHandler",
        "formatter": LOG_FORMAT,
        "filters": ["request_id", "slow_query"],
    }
    LOGGING["loggers"]["django.db.backends"] = {
        "handlers": ["slow_query"],
        "level": "DEBUG",
        "propagate": False,
    }

SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    # Initialize error monitoring only when a DSN is configured and the SDK is
    # installed; otherwise this is a no-op rather than a hard dependency.
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
            send_default_pii=False,
            environment=env("SENTRY_ENVIRONMENT", default="production"),
        )
    except ImportError:
        pass
