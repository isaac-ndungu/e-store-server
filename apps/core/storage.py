"""Cloudinary-backed media storage.

The default file storage serves two kinds of uploads: product, category,
brand, and review images, plus PDF documents (product manuals, guides,
datasheets) and ticket attachments that may be images or PDFs. Cloudinary
requires the correct resource type per upload and rejects, for example, a PDF
sent as an image, so this storage selects the resource type from the file
extension instead of uploading every file as a single fixed type.

Cloudinary's upload response returns a ``public_id`` without a file
extension, so ``_save`` persists ``public_id`` plus the upload format
(``photo.jpg``). The suffix is what lets every later read (``url``,
``open``, ``delete``) recover the resource type offline. Delivery URLs keep
the suffix  -  Cloudinary strips it to locate the resource  -  while ``delete``
strips it before calling the destroy API, which expects the bare public id.
Names stored before this scheme (bare public ids) carry no type signal and
resolve as images, which covers every such row in practice; only a
suffix-less non-image would need re-uploading.
"""

import os

import cloudinary.uploader
from cloudinary_storage.storage import RESOURCE_TYPES, MediaCloudinaryStorage
from django.core.files.uploadedfile import UploadedFile

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
RAW_EXTENSIONS = frozenset(
    {
        "pdf",
        "txt",
        "csv",
        "log",
        "md",
        "json",
        "xml",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "zip",
        "bin",
    }
)


class CloudinaryMediaStorage(MediaCloudinaryStorage):
    """Default media storage that maps each file to its Cloudinary type.

    Images upload as ``image`` so transformations and responsive URLs keep
    working, videos upload as ``video``, and everything else (PDF manuals,
    ticket attachments) uploads as ``raw`` so Cloudinary accepts the file.
    Stored names keep the upload format as a suffix so the type stays
    recoverable without an extra API call.
    """

    def _get_resource_type(self, name):
        """Return the Cloudinary resource type for a storage name.

        The suffix appended at save time carries the type: a known image or
        video suffix resolves to that type, a known document suffix resolves
        to raw, and a bare name from before this scheme resolves as an image.

        Args:
            name: the storage key being uploaded, opened, or deleted.

        Returns:
            str: ``image``, ``video``, or ``raw``.
        """
        extension = os.path.splitext(name)[1].lower().lstrip(".")
        if extension in IMAGE_EXTENSIONS:
            return RESOURCE_TYPES["IMAGE"]
        if extension in VIDEO_EXTENSIONS:
            return RESOURCE_TYPES["VIDEO"]
        if extension in RAW_EXTENSIONS:
            return RESOURCE_TYPES["RAW"]
        return RESOURCE_TYPES["IMAGE"]

    def _save(self, name, content):
        """Persist an upload and return its storage key with format suffix.

        Args:
            name: the upload path including the original filename.
            content: the file content to upload.

        Returns:
            str: the Cloudinary public id plus the upload format
                (``media/products/images/photo.jpg``), or the bare public
                id when no format is known.
        """
        name = self._normalise_name(name)
        name = self._prepend_prefix(name)
        original_extension = os.path.splitext(name)[1].lower().lstrip(".")
        options = {
            "use_filename": True,
            "resource_type": self._get_resource_type(name),
            "tags": self.TAG,
        }
        folder = os.path.dirname(name)
        if folder:
            options["folder"] = folder
        response = cloudinary.uploader.upload(UploadedFile(content, name), **options)
        suffix = (response.get("format") or original_extension).lower()
        public_id = response["public_id"]
        return f"{public_id}.{suffix}" if suffix else public_id

    def delete(self, name):
        """Delete a stored file, accepting the suffixed storage key.

        Args:
            name: the storage key as returned by ``save``.

        Returns:
            bool: True when Cloudinary confirmed the deletion.
        """
        base, dot, extension = name.rpartition(".")
        public_id = (
            base
            if dot
            and extension.lower()
            in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | RAW_EXTENSIONS
            else name
        )
        response = cloudinary.uploader.destroy(
            public_id, invalidate=True, resource_type=self._get_resource_type(name)
        )
        return response["result"] == "ok"
