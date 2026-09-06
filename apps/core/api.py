"""Shared helpers for API views across apps.

Service functions raise Django's ``ValidationError`` to signal a broken business
rule; DRF only translates its own ``rest_framework`` variant, so an untranslated
Django error would surface as a 500. ``service_error_to_400`` wraps a service
call so those business-rule failures become clean 400 responses instead.
"""

from functools import wraps

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError


def service_error_to_400(mutation):
    """Convert a service-layer validation error into a DRF 400 response.

    Args:
        mutation (Callable): the service function to invoke.

    Returns:
        Callable: a wrapper that raises DRF's ``ValidationError`` on a
            service validation failure.
    """

    @wraps(mutation)
    def wrapper(*args, **kwargs):
        try:
            return mutation(*args, **kwargs)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.messages) from exc

    return wrapper
