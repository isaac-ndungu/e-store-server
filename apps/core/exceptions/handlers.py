"""DRF exception handler that returns consistent, safe error messages.

In non-debug mode, unexpected exceptions are logged but not exposed to the
client with stack traces or internal details. Validation errors still return
their field-level messages so clients can act on them.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    """Handle exceptions and return normalized error responses.

    Args:
        exc: The exception instance raised.
        context: Additional context about the exception.

    Returns:
        Response: A DRF Response instance with normalized error structure.
    """
    response = exception_handler(exc, context)

    if response is not None:
        if isinstance(exc, exceptions.ValidationError):
            if isinstance(response.data, dict):
                errors = response.data
            elif isinstance(response.data, list):
                errors = {"non_field_errors": response.data}
            else:
                errors = {"detail": response.data}
            return Response({"errors": errors}, status=response.status_code)

        if isinstance(exc, exceptions.NotAuthenticated):
            return Response(
                {"errors": {"detail": "Authentication credentials were not provided."}},
                status=response.status_code,
            )

        if isinstance(exc, exceptions.PermissionDenied):
            return Response(
                {"errors": {"detail": "You do not have permission to perform this action."}},
                status=response.status_code,
            )

        if isinstance(exc, exceptions.NotFound):
            return Response(
                {"errors": {"detail": "Not found."}},
                status=response.status_code,
            )

        if isinstance(exc, exceptions.MethodNotAllowed):
            return Response(
                {"errors": {"detail": f"Method '{exc.detail}' not allowed."}},
                status=response.status_code,
            )

        if isinstance(exc, exceptions.Throttled):
            wait = getattr(exc, "wait", None)
            detail = "Request was throttled."
            if wait:
                detail = f"Request was throttled. Expected available in {int(wait)} seconds."
            return Response({"errors": {"detail": detail}}, status=response.status_code)

        if isinstance(response.data, dict):
            if "detail" in response.data and len(response.data) == 1:
                return Response({"errors": response.data}, status=response.status_code)
            return Response({"errors": response.data}, status=response.status_code)
        if isinstance(response.data, list):
            return Response({"errors": {"non_field_errors": response.data}}, status=response.status_code)
        return Response({"errors": {"detail": response.data}}, status=response.status_code)

    if isinstance(exc, Http404):
        return Response({"errors": {"detail": "Not found."}}, status=404)

    if isinstance(exc, DjangoValidationError):
        if hasattr(exc, "message_dict"):
            return Response({"errors": exc.message_dict}, status=400)
        if hasattr(exc, "messages"):
            return Response({"errors": {"non_field_errors": list(exc.messages)}}, status=400)
        return Response({"errors": {"non_field_errors": [str(exc)]}}, status=400)

    logger.exception("Unhandled exception", extra={"exception_class": exc.__class__.__name__})

    if settings.DEBUG:
        return Response(
            {"errors": {"detail": str(exc), "type": exc.__class__.__name__}},
            status=500,
        )

    return Response({"errors": {"detail": "An internal server error occurred."}}, status=500)
