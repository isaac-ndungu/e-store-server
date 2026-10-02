"""Response hardening headers for every HTTP response.

Django's own middleware already sets ``X-Content-Type-Options``,
``Referrer-Policy`` (in production settings), and ``X-Frame-Options``. This
middleware adds the remaining baseline headers scanners and browsers expect:

- ``Content-Security-Policy`` scoped to self plus the documentation page's
  CDNs (Swagger UI / Redoc assets) and inline styles/scripts (admin and API
  docs rely on them).
- ``Permissions-Policy`` disabling sensor and payment features the site never
  uses.
- ``Cross-Origin-Opener-Policy`` isolating browsing contexts, without
  overriding a view that already set its own value.
- ``Cache-Control: private`` on the API docs pages (Swagger UI, Redoc, and
  the raw OpenAPI schema) so browsers may reuse their own copy for an hour
  instead of refetching on every visit. ``private`` (never a shared cache)
  because the docs responses vary per caller session cookie.
- ``Cache-Control: public`` on anonymous storefront GETs (catalog,
  collections, bundles, content, site config) so browsers and edge caches
  may reuse the payload briefly on patchy connections. Only GET requests
  without an ``Authorization`` header qualify.

Headers are only added when missing so an explicit view-level value always
wins over this default.
"""

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https:; "
    "style-src 'self' 'unsafe-inline' https:; "
    "img-src 'self' data: https:; "
    "font-src 'self' https: data:; "
    "connect-src 'self' https:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"

DOCS_PATH_PREFIXES = ("/api/swagger/", "/api/redoc/", "/api/schema/")
DOCS_CACHE_CONTROL = "private, max-age=3600"

# Anonymous storefront reads safe to reuse briefly in browsers and shared
# caches. Authenticated or mutating requests never match: only GET without
# an Authorization header qualifies, so per-user data is never cached.
PUBLIC_GET_PREFIXES = (
    "/api/v1/categories/",
    "/api/v1/brands/",
    "/api/v1/products/",
    "/api/v1/collections/",
    "/api/v1/bundles/",
    "/api/v1/content/",
    "/api/v1/site-config/",
    "/api/v1/legal/",
)
PUBLIC_GET_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=30"


class SecurityHeadersMiddleware:
    """Add baseline hardening headers to every response when absent.

    Args:
        get_response: the next middleware or view in the chain.
    """

    def __init__(self, get_response):
        """Store the downstream callable.

        Args:
            get_response: the next middleware or view in the chain.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Process the request and stamp hardening headers on the response.

        Args:
            request: the incoming HTTP request.

        Returns:
            HttpResponse: the downstream response with defaults filled in.
        """
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.path.startswith(DOCS_PATH_PREFIXES):
            response.headers.setdefault("Cache-Control", DOCS_CACHE_CONTROL)
        elif (
            request.method == "GET"
            and not request.headers.get("Authorization")
            and request.path.startswith(PUBLIC_GET_PREFIXES)
        ):
            response.headers.setdefault("Cache-Control", PUBLIC_GET_CACHE_CONTROL)
        return response
