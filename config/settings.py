"""Django settings for the e-commerce platform.

Configuration is loaded from environment variables with a ``.env`` file via
python-decouple (see ``.env.example``). ``DEBUG`` distinguishes development from
production. Test mode is auto-detected from ``manage.py test`` and by default
swaps in in-memory SQLite, a local-memory cache, and eager Celery so tests need
no external services; setting ``TEST_USE_POSTGRES`` to a truthy value keeps the
configured PostgreSQL database instead, which is required for the concurrency
tests that exercise ``select_for_update`` row-locking behaviour. Error
tracking initializes whenever ``SENTRY_DSN`` is configured.
"""

import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

TESTING = "test" in sys.argv


def _database_from_url(url, conn_max_age, conn_health_checks):
    """Convert a ``DATABASE_URL`` into a Django ``DATABASES`` entry.

    Handles the ``postgres://`` / ``postgresql://`` URLs issued by managed
    providers (e.g. Neon). Query-string options such as ``sslmode`` are passed
    through as connection ``OPTIONS`` so ``?sslmode=require`` keeps working.

    Args:
        url: database URL string.
        conn_max_age: seconds to keep a database connection open for reuse
            across requests instead of reconnecting every time.
        conn_health_checks: whether to check a reused connection before use.

    Returns:
        dict: database configuration suitable for ``DATABASES["default"]``.
    """
    parsed = urlparse(url)
    options = dict(parse_qsl(parsed.query))
    settings_dict = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port) if parsed.port else "",
        "CONN_MAX_AGE": conn_max_age,
        "CONN_HEALTH_CHECKS": conn_health_checks,
    }
    sslmode = options.pop("sslmode", "")
    if sslmode:
        settings_dict.setdefault("OPTIONS", {})["sslmode"] = sslmode
    if options:
        settings_dict.setdefault("OPTIONS", {}).update(options)
    settings_dict.setdefault("OPTIONS", {}).setdefault("connect_timeout", 10)
    settings_dict["OPTIONS"].setdefault("keepalives", 1)
    settings_dict["OPTIONS"].setdefault("keepalives_idle", 30)
    settings_dict["OPTIONS"].setdefault("keepalives_interval", 10)
    settings_dict["OPTIONS"].setdefault("keepalives_count", 5)
    return settings_dict


SECRET_KEY = config("SECRET_KEY", default="")
DEBUG = config("DEBUG", default=True, cast=bool)

if not DEBUG and not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is False")

ALLOWED_HOSTS = ["*"] if DEBUG else config("ALLOWED_HOSTS", default="", cast=Csv())

# Managed hosts (e.g. Render) expose the public hostname in a dedicated env
# var. Trust it alongside ALLOWED_HOSTS so the app serves traffic without a
# settings change on every redeploy.
RENDER_EXTERNAL_HOSTNAME = config("RENDER_EXTERNAL_HOSTNAME", default="")
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, RENDER_EXTERNAL_HOSTNAME]

DJANGO_CORE_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.postgres",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
]


LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.notifications",
    "apps.catalog",
    "apps.shipping",
    "apps.collections",
    "apps.bundles",
    "apps.cart",
    "apps.promotions",
    "apps.orders",
    "apps.returns",
    "apps.social_proof",
    "apps.reviews",
    "apps.content",
    "apps.support",
    "apps.analytics",
    "apps.dashboard",
    "apps.audit",
    "apps.inquiries",
]

INSTALLED_APPS = DJANGO_CORE_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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

# PostgreSQL is the sole database for dev/prod. A single DATABASE_URL (as
# issued by managed providers such as Neon) takes precedence when set; the
# discrete DB_* variables remain for local dev and Compose. When the pooler
# URL is set it takes precedence over DATABASE_URL because the pooler reuses
# backend connections, so each new client connection skips the full Postgres
# startup/auth handshake. Tests run
# in-memory SQLite for speed and
#
# Connections are kept open for reuse across requests (CONN_MAX_AGE). With the
# default of zero Django opens a fresh TCP+TLS+auth connection on every
# request, which dominates response time when the database is in another
# region (several seconds per connect versus a few hundred milliseconds per
# query). Health checks guard reused connections against stale pooler/server
# closes.
DB_CONN_MAX_AGE = config("DB_CONN_MAX_AGE", default=600, cast=int)
DB_CONN_HEALTH_CHECKS = config("DB_CONN_HEALTH_CHECKS", default=True, cast=bool)
DATABASE_URL = config("DATABASE_URL", default="")
DATABASE_URL_POOLED = config("DATABASE_URL_POOLED", default="")
EFFECTIVE_DATABASE_URL = DATABASE_URL_POOLED or DATABASE_URL
if EFFECTIVE_DATABASE_URL:
    DATABASES = {
        "default": _database_from_url(
            EFFECTIVE_DATABASE_URL, DB_CONN_MAX_AGE, DB_CONN_HEALTH_CHECKS
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": config("DB_ENGINE", default="django.db.backends.postgresql"),
            "NAME": config("DB_NAME", default=""),
            "USER": config("DB_USER", default=""),
            "PASSWORD": config("DB_PASSWORD", default=""),
            "HOST": config("DB_HOST", default=""),
            "PORT": config("DB_PORT", default=""),
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": DB_CONN_HEALTH_CHECKS,
            "OPTIONS": {
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        }
    }
if TESTING and not config("TEST_USE_POSTGRES", default=False, cast=bool):
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
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
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
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
    "DEFAULT_THROTTLE_RATES": {
        "public": "100/min",
        "auth_login": "3/min",
        "auth_reauth": "10/min",
        "auth_write": "10/min",
        "auth_read": "60/min",
        "notification_send": "5/min",
        "public_catalog": "100/min",
        "coupon_validate": "5/min",
        "admin": "300/min",
        "order_write": "5/min",
        "order_read": "30/min",
        "notifications_callback": "500/min",
        "social_proof_view": "30/min",
        "review_read": "60/min",
        "review_write": "10/min",
        "content_read": "60/min",
        "content_write": "10/min",
        "support_read": "60/min",
        "support_write": "20/min",
        "analytics_read": "60/min",
        "dashboard_read": "60/min",
        "inquiry_write": "20/min",
        "inquiry_read": "60/min",
        "cart_read": "100/min",
        "cart_write": "60/min",
        "order_intake": "30/min",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "E-Store API",
    "DESCRIPTION": "Kenyan e-commerce platform for home appliances",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v1/",
    "TAGS": [
        {"name": "core", "description": "Site configuration and health"},
        {
            "name": "accounts",
            "description": "User registration, authentication, and addresses",
        },
        {"name": "notifications", "description": "SMS and notification logs"},
        {
            "name": "catalog",
            "description": "Brands, categories, products, and variants",
        },
        {"name": "shipping", "description": "Delivery areas"},
        {
            "name": "collections",
            "description": "Product collections and smart collections",
        },
        {"name": "bundles", "description": "Product bundles"},
        {"name": "cart", "description": "Anonymous guest cart"},
        {"name": "promotions", "description": "Discounts and coupons"},
        {"name": "orders", "description": "Staff order intake and status"},
        {
            "name": "returns",
            "description": "Post-delivery returns and pre-shipment cancellations",
        },
        {
            "name": "social_proof",
            "description": "Product view tracking and live-viewer counts",
        },
        {
            "name": "reviews",
            "description": "Product reviews, Q&A, and verified-purchase badges",
        },
        {
            "name": "content",
            "description": "Static content pages and promotional banners",
        },
        {
            "name": "support",
            "description": "Staff-only support tickets",
        },
        {
            "name": "analytics",
            "description": "Staff-only dashboard summaries and reports",
        },
        {
            "name": "dashboard",
            "description": "Staff-only dashboard widgets and live alerts",
        },
        {
            "name": "inquiries",
            "description": "Anonymous WhatsApp/email hand-off capture and staff follow-up",
        },
        {
            "name": "order_intake",
            "description": "Staff-only order creation from assisted sales",
        },
    ],
}


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}


REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")
# An explicitly empty CACHE_/CELERY_ value means "fall back to REDIS_URL" so a
# deployment only needs to set one variable (and empty dashboard fields do not
# produce a broken empty connection string).
CACHE_REDIS_URL = config("CACHE_REDIS_URL", default="") or REDIS_URL
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="") or REDIS_URL
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="") or REDIS_URL

CACHES = {
    "default": {
        "BACKEND": (
            "django.core.cache.backends.redis.RedisCache"
            if not TESTING
            else "django.core.cache.backends.locmem.LocMemCache"
        ),
        "LOCATION": (CACHE_REDIS_URL if not TESTING else ""),
    }
}

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = TESTING
CELERY_TASK_EAGER_PROPAGATES = TESTING

# Celery Beat runs only idempotent maintenance tasks. Stock moves
# synchronously inside the staff order-intake transaction, so no sweep is
# needed to release held stock.
CELERY_BEAT_SCHEDULE = {
    "refresh-smart-collections": {
        "task": "apps.collections.tasks.refresh_smart_collections",
        "schedule": 900.0,
    },
    "purge-old-view-events": {
        "task": "apps.social_proof.tasks.purge_old_view_events_task",
        "schedule": 86400.0,
    },
    "cleanup-orphan-review-photos": {
        "task": "apps.reviews.tasks.cleanup_orphan_review_photos",
        "schedule": 86400.0,
    },
}

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
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:3000")
RESET_LINK_BASE = config("RESET_LINK_BASE", default="http://localhost:3000/reset/")
WHATSAPP_BUSINESS_NUMBER = config("WHATSAPP_BUSINESS_NUMBER", default="")
ORDER_INTAKE_EMAIL = config("ORDER_INTAKE_EMAIL", default="")


STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# CDN domain placeholder (e.g. a Cloudflare distribution fronting S3/R2).
CDN_DOMAIN = config("CDN_DOMAIN", default="")

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = config("CORS_ALLOW_CREDENTIALS", default=False, cast=bool)
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="")
AWS_S3_ENDPOINT_URL = config("AWS_S3_ENDPOINT_URL", default="")

# Error tracking is active only when a DSN is configured; it stays inert in
# local development so a missing secret never crashes startup or floods the
# local console. The sample rate is environment-tunable so staging can keep
# full traces while production samples to control cost.
SENTRY_DSN = config("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
        ],
        environment="production" if not DEBUG else "development",
        traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=1.0, cast=float),
    )

if not DEBUG and not TESTING:
    # Behind Render (or any TLS-terminating proxy) Django only sees plain HTTP,
    # so trust the forwarded proto header to avoid redirect loops.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    if RENDER_EXTERNAL_HOSTNAME:
        render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
        if render_origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, render_origin]
    STATIC_URL = f"{CDN_DOMAIN}/static/"
    MEDIA_URL = f"{CDN_DOMAIN}/media/"

if TESTING:
    PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]
    MEDIA_ROOT = BASE_DIR / "media_test"
