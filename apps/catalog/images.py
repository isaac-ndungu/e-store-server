"""Image processing for product photographs.

Resizes and re-encodes uploaded product images into the responsive variant
set the storefront serves (AVIF/WebP at fixed widths). Originals are kept
for reference, but storefront image URLs come from the processed variants so
a full-resolution upload is never served directly to browsers.
"""

import hashlib
import io
import os

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

PROCESSED_IMAGE_WIDTHS = (320, 640, 1024)
PROCESSED_IMAGE_FORMATS = ("webp", "avif")
PREFERRED_DISPLAY_WIDTH = 640
WEBP_QUALITY = 82
AVIF_QUALITY = 50


def avif_encoding_supported():
    """Return whether this Pillow build can encode AVIF files.

    Returns:
        bool: True when AVIF encoding is available.
    """
    from PIL import features

    return bool(features.check("avif"))


def generate_variants(storage_name):
    """Generate responsive variants for a stored image and return their URLs.

    Downscales the image to each supported width and re-encodes it as WebP
    and (when available) AVIF. Widths larger than the source are skipped so
    small uploads are never upscaled. Variants are stored alongside the
    original under deterministic names.

    Args:
        storage_name (str): the storage key of the original image.

    Returns:
        list[dict]: one entry per produced variant with keys ``width``,
            ``height``, ``format``, and ``url``. Empty if the source cannot be
            decoded.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    ext = os.path.splitext(storage_name)[1]
    stem = storage_name[: -len(ext)] if ext else storage_name
    formats = list(PROCESSED_IMAGE_FORMATS)
    if "avif" in formats and not avif_encoding_supported():
        formats.remove("avif")

    variants = []
    try:
        with default_storage.open(storage_name) as handle:
            source_bytes = handle.read()
        source_fingerprint = hashlib.sha256(source_bytes).hexdigest()[:12]
        with Image.open(io.BytesIO(source_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            img.load()
            for width in PROCESSED_IMAGE_WIDTHS:
                if width > img.width:
                    continue
                height = max(1, round(img.height * width / img.width))
                resized = img
                if img.width != width:
                    resized = img.resize((width, height), Image.LANCZOS)
                for image_format in formats:
                    buffer = io.BytesIO()
                    if image_format == "webp":
                        save_kwargs = {"quality": WEBP_QUALITY, "method": 6}
                    else:
                        save_kwargs = {"quality": AVIF_QUALITY}
                    try:
                        resized.save(buffer, format=image_format.upper(), **save_kwargs)
                    except OSError, ValueError:
                        continue
                    variant_name = (
                        f"{stem}--{source_fingerprint}--{width}w.{image_format}"
                    )
                    if default_storage.exists(variant_name):
                        name = variant_name
                    else:
                        name = default_storage.save(
                            variant_name,
                            ContentFile(buffer.getvalue()),
                        )
                    variants.append(
                        {
                            "width": width,
                            "height": height,
                            "format": image_format,
                            "url": default_storage.url(name),
                        }
                    )
    except UnidentifiedImageError, OSError, ValueError:
        return []
    return variants


def delete_image_files(storage_name):
    """Delete a stored original image and its current processed variants.

    Variant names combine the original's storage path with a fingerprint of its
    content, so cleanup reads the source to find the generated files instead of
    keeping a manifest. Legacy unfingerprinted variant names are removed too.
    The storage backend suppresses errors for files that do not exist, making
    this safe to call repeatedly and safe for formats that were never written.

    Args:
        storage_name (str): the storage key of the original image.
    """
    ext = os.path.splitext(storage_name)[1]
    stem = storage_name[: -len(ext)] if ext else storage_name
    try:
        with default_storage.open(storage_name) as handle:
            fingerprint = hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:
        fingerprint = None
    for width in PROCESSED_IMAGE_WIDTHS:
        for image_format in PROCESSED_IMAGE_FORMATS:
            if fingerprint:
                default_storage.delete(
                    f"{stem}--{fingerprint}--{width}w.{image_format}"
                )
            default_storage.delete(f"{stem}--{width}w.{image_format}")
    default_storage.delete(storage_name)


def preferred_image_url(storage_name, sources):
    """Return the best display URL for an image from its processed variants.

    Prefers the 640px WebP variant, then any 640px variant, then the largest
    processed variant, and falls back to the original file only when nothing
    has been processed yet.

    Args:
        storage_name (str): the storage key of the original image.
        sources (list[dict]): processed variants as produced by
            ``generate_variants``.

    Returns:
        str | None: the display URL, or None when the file cannot be
            resolved.
    """
    if sources:
        preferred_width = [
            source for source in sources if source["width"] == PREFERRED_DISPLAY_WIDTH
        ]
        if preferred_width:
            webp = next(
                (source for source in preferred_width if source["format"] == "webp"),
                None,
            )
            return (webp or preferred_width[0])["url"]
        return sorted(sources, key=lambda source: source["width"])[-1]["url"]
    if not storage_name:
        return None
    try:
        return default_storage.url(storage_name)
    except ValueError:
        return None
