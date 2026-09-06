"""Upload validation for support attachments.

A ticket attachment is a receipt, screenshot, or fault photo — either an image
or a PDF. The type is verified by content (Pillow decode / PDF magic bytes),
never by file extension, and the size is capped, so the attachment endpoint
cannot be used to upload executable content or park oversized files.
"""

from django.core.exceptions import ValidationError

from apps.catalog.validators import validate_image_upload, validate_pdf_upload
from apps.support.constants import ATTACHMENT_MAX_SIZE_MB


def validate_ticket_attachment(file):
    """Validate that an uploaded ticket attachment is an allowed image or PDF.

    The file passes when it is a real image (JPEG/PNG/WebP/AVIF) or a PDF within
    the configured size ceiling. Anything else is rejected with a single opaque
    message rather than echoing the probed content type back.

    Args:
        file: the uploaded file object.

    Raises:
        ValidationError: when the file is neither a valid image nor a valid PDF
            within the size limit.
    """
    try:
        validate_image_upload(file, max_size_mb=ATTACHMENT_MAX_SIZE_MB)
        return
    except ValidationError:
        pass

    try:
        validate_pdf_upload(file, max_size_mb=ATTACHMENT_MAX_SIZE_MB)
        return
    except ValidationError:
        pass

    raise ValidationError(
        "Attach a valid image (JPEG, PNG, WebP, or AVIF) or PDF within "
        f"{ATTACHMENT_MAX_SIZE_MB} MB."
    )
