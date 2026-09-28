"""Cloudinary-backed media storage.

The default file storage serves two kinds of uploads: product, category,
brand, and review images, plus PDF documents (product manuals, guides,
datasheets) and ticket attachments that may be images or PDFs. Cloudinary
requires the correct resource type per upload and rejects, for example, a PDF
sent as an image, so this storage selects the resource type from the file
extension instead of uploading every file as a single fixed type.
"""

import os

from cloudinary_storage.storage import RESOURCE_TYPES, MediaCloudinaryStorage

IMAGE_EXTENSIONS = frozenset(
    {
        "jpg",
        "jpeg",
        "jpe",
        "png",
        "gif",
        "webp",
        "bmp",
        "tif",
        "tiff",
        "ico",
        "svg",
        "avif",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        "mp4",
        "webm",
        "mov",
        "avi",
        "mkv",
        "m4v",
        "3gp",
        "ogv",
        "wmv",
        "mpeg",
        "flv",
    }
)


class CloudinaryMediaStorage(MediaCloudinaryStorage):
    """Default media storage that maps each file to its Cloudinary type.

    Images upload as ``image`` so transformations and responsive URLs keep
    working, videos upload as ``video``, and everything else (PDF manuals,
    ticket attachments) uploads as ``raw`` so Cloudinary accepts the file.
    URL building, opening, and deletion behave exactly like the parent class.
    """

    def _get_resource_type(self, name):
        """Return the Cloudinary resource type for a storage name.

        Args:
            name: the storage key being uploaded, opened, or deleted.

        Returns:
            str: ``image`` for image extensions, ``video`` for video
                extensions, and ``raw`` for anything else.
        """
        extension = os.path.splitext(name)[1].lower().lstrip(".")
        if extension in IMAGE_EXTENSIONS:
            return RESOURCE_TYPES["IMAGE"]
        if extension in VIDEO_EXTENSIONS:
            return RESOURCE_TYPES["VIDEO"]
        return RESOURCE_TYPES["RAW"]
