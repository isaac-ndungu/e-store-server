"""Django settings for the e-commerce platform.

Configuration is loaded from environment variables with a ``.env`` file via
python-decouple (see ``.env.example``). ``DEBUG`` distinguishes development from
production. Test mode is auto-detected from ``manage.py test`` and swaps in
in-memory SQLite, a local-memory cache, and eager Celery so tests need no
external services.
"""

import sys
from datetime import timedelta
from pathlib import Path

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

TESTING = "test" in sys.argv

SECRET_KEY = config("SECRET_KEY", default="")
DEBUG = config("DEBUG", default=True, cast=bool)

# Production (DEBUG=False) must never run with a placeholder or empty key.
if not DEBUG and not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is False")

ALLOWED_HOSTS = ["*"] if DEBUG else config("ALLOWED_HOSTS", default=[], cast=Csv())

DJANGO_CORE_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "corsheaders",
]

# Local apps live under apps/ and are referenced as 'apps.<name>' (e.g.
# 'apps.core', 'apps.accounts'). Apps are added to this list as they are built —
# never before the app exists.
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_CORE_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# The platform's own User model (email + password login, see apps/accounts).
# Set before any migration runs against a real database — swapping this after
# migrate is not possible without a fresh database.
AUTH_USER_MODEL = "accounts.User"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# PostgreSQL is the sole database for dev/prod. Empty DB_HOST/DB_PORT use the
# local Unix socket / peer auth. Tests run in-memory SQLite for speed and
# isolation (the Postgres role lacks CREATEDB for a test database).
DATABASES = {
    "default": {
        "ENGINE": config("DB_ENGINE", default="django.db.backends.postgresql"),
        "NAME": config("DB_NAME"),
        "USER": config("DB_USER"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default=""),
        "PORT": config("DB_PORT", default=""),
    }
}
if TESTING:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # JWT authenticates the API via the email + password credential from the
        # accounts app; SessionAuthentication supports the browsable API and the
        # admin during development.
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Every view must declare an explicit permission class; fail-closed by
    # default so a view that forgets one is not accidentally public.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # Endpoints opt into throttling by declaring a throttle_scope; the shared
    # scopes below carry concrete limits. Auth/OTP/STK endpoints add stricter
    # scopes when those endpoints are implemented.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "public": "100/min",
        # Login and registration carry concrete brute-force/abuse limits, not
        # the framework default. STK Push and OTP scopes get stricter scopes
        # once those endpoints are implemented.
        "auth_login": "3/min",
        "auth_reauth": "10/min",
        "auth_write": "10/min",
        # The staff-only test-SMS endpoint costs real money (SMS charges), so
        # it gets a concrete limit rather than the framework default.
        "notification_send": "5/min",
    },
}

# JWT access/refresh token pair issued on email + password login. Access tokens
# are short-lived. Refresh tokens are shorter-lived than before to bound the
# window a stolen one stays valid, and are rotated on every use — each refresh
# mints a new refresh and blacklists the old one, so a replayed token is
# rejected. Logout blacklists the presented refresh token.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

# Redis backs the hot-data cache (effective prices, smart-collection snapshots,
# warehouse-selection lookups, view counters). Tests use a local-memory cache
# to stay offline.
CACHES = {
    "default": {
        "BACKEND": (
            "django.core.cache.backends.redis.RedisCache"
            if not TESTING
            else "django.core.cache.backends.locmem.LocMemCache"
        ),
        "LOCATION": (
            config("CACHE_REDIS_URL", default="redis://localhost:6379/0")
            if not TESTING
            else ""
        ),
    }
}

# Background tasks (eTIMS retry, M-Pesa callbacks, reservation/OTP sweep,
# smart-collection refresh, overdue-invoice sweep) run via Celery. Tests
# execute tasks eagerly so they never hit a broker.
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config(
    "CELERY_RESULT_BACKEND", default="redis://localhost:6379/0"
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = TESTING
CELERY_TASK_EAGER_PROPAGATES = TESTING

# Email used for the password-reset flow. Tests and local development use
# offline backends (in-memory / console); real SMTP is configured via env vars
# for production. ``RESET_LINK_BASE`` is the frontend URL the reset links point
# to, since the storefront renders the reset form, not this API.
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.locmem.EmailBackend"
        if TESTING
        else "django.core.mail.backends.console.EmailBackend"
    ),
)
EMAIL_HOST = config("EMAIL_HOST", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="no-reply@estore.local")
RESET_LINK_BASE = config("RESET_LINK_BASE", default="http://localhost:3000/reset/")

# STORAGES placeholders for object storage (Cloudflare R2 / AWS S3). Until the
# AWS_* credentials are set in the environment the local filesystem is used for
# both static and media.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# CDN domain placeholder (e.g. a Cloudflare distribution fronting S3/R2).
CDN_DOMAIN = config("CDN_DOMAIN", default="")

# Allow origins for the separate storefront (Next.js) and admin dashboard
# (React) front-ends. Never wildcard in production.
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default=[], cast=Csv())

# Object storage placeholders, activated once credentials are provisioned.
AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="")
AWS_S3_ENDPOINT_URL = config("AWS_S3_ENDPOINT_URL", default="")

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    STATIC_URL = f"{CDN_DOMAIN}/static/"
    MEDIA_URL = f"{CDN_DOMAIN}/media/"

if TESTING:
    PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]
