"""Rate limits for account endpoints that target a person rather than an IP.

The reset-request endpoint must be limited both per caller IP and per submitted
email address: an IP limit alone lets a distributed caller hammer one mailbox,
and an email limit alone lets one caller fan out across many addresses.
"""

import hashlib

from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.services import normalize_email


class PasswordResetEmailThrottle(SimpleRateThrottle):
    """Limit password-reset requests per submitted email address.

    The normalized address is hashed before it enters the cache key so the
    rate-limit store never holds a raw customer email. Requests without a
    usable email are left to the serializer and the per-IP throttle.
    """

    scope = "password_reset_email"

    def get_cache_key(self, request, view):
        """Return a cache key for the submitted email, or None when absent.

        Args:
            request: the incoming reset-request.
            view: the view handling the request.

        Returns:
            str | None: a scoped cache key, or None when no email was supplied
                as a string.
        """
        raw_email = request.data.get("email") if hasattr(request, "data") else None
        if not isinstance(raw_email, str):
            return None
        email = normalize_email(raw_email)
        if not email:
            return None
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}
