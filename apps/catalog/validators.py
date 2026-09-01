"""Shared validators for the catalog app.

Reusable file-upload validators that enforce size and format constraints on
images uploaded.
"""

import io

from django.core.exceptions import ValidationError

ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "AVIF"}
DEFAULT_MAX_UPLOAD_SIZE_MB = 10


def validate_image_upload(file, max_size_mb=None):
    """Validate that an uploaded file is an allowed image within the size limit.

    Checks the file is a real image decodable by Pillow (``ImageField`` does
    this automatically, but calling it here allows reuse outside ``ImageField``
    paths), that its format is in the allowed set, and that its size does not
    exceed the limit.

    Args:
        file: the uploaded file object (must have ``read()``, ``size``, and
            ``name`` attributes).
        max_size_mb (int | None): maximum file size in megabytes. Defaults to
            ``DEFAULT_MAX_UPLOAD_SIZE_MB``.

    Raises:
        ValidationError: if the file exceeds the size limit or its format is
            not in the allowed set.
    """
    if max_size_mb is None:
        max_size_mb = DEFAULT_MAX_UPLOAD_SIZE_MB

    max_bytes = max_size_mb * 1024 * 1024
    if hasattr(file, "size") and file.size > max_bytes:
        raise ValidationError(
            f"File size ({file.size} bytes) exceeds the maximum allowed size "
            f"of {max_size_mb} MB."
        )

    try:
        from PIL import Image, UnidentifiedImageError

        if hasattr(file, "seek"):
            file.seek(0)
        data = file.read()
        img = Image.open(io.BytesIO(data))
        img_format = img.format
        img.close()
    except (UnidentifiedImageError, ValueError, OSError) as exc:
        raise ValidationError(
            "Upload a valid image file (JPEG, PNG, WebP, or AVIF)."
        ) from exc

    if img_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError(
            f"Image format '{img_format}' is not allowed. "
            f"Accepted formats: {', '.join(sorted(ALLOWED_IMAGE_FORMATS))}."
        )
