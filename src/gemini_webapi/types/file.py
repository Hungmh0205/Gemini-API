import mimetypes
from typing import Any

from pydantic import BaseModel, ConfigDict

from gemini_webapi.constants import DOCUMENT_MIME_TYPES

# Extensions Gemini delivers through a media-specific field rather than as a document
# attachment. A file found by scanning the candidate is treated as a document unless its
# name ends in one of these.
MEDIA_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".svg",
        ".heic",
        ".avif",
        ".mp4",
        ".webm",
        ".mov",
        ".mkv",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
        ".aac",
    }
)


class GeneratedFile(BaseModel):
    """A downloadable document Gemini produced as part of its reply.

    Gemini's code execution writes results to a sandbox and reports them in reply text as a
    `sandbox:/mnt/data/report.xlsx` placeholder. The bytes are served from an opaque
    `googleusercontent.com` download URL, so `name` normally comes from that placeholder
    rather than from the URL path - which is what makes the delivered filename match what
    the model said it wrote.

    Parameters
    ----------
    url: `str`
        Download URL for the file content.
    title: `str`, optional
        Human-readable label. Defaults to the file name.
    name: `str`, optional
        Original filename including extension, e.g. ``"report.xlsx"``.
    mime_type: `str`, optional
        Declared MIME type. Guessed from `name` when not supplied.
    size: `int`, optional
        Declared size in bytes, when Gemini reports one.

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    title: str = ""
    name: str = ""
    mime_type: str = ""
    size: int | None = None
    client_ref: Any | None = None

    def __str__(self) -> str:
        return self.name or self.title or self.url

    def __repr__(self) -> str:
        return f"GeneratedFile(name={self.name!r}, url={self.url!r})"

    @property
    def extension(self) -> str:
        """Lowercase file extension including the dot, or empty when the name carries none."""
        name = self.name or self.title
        dot = name.rfind(".")
        return name[dot:].lower() if dot > 0 else ""

    @property
    def resolved_mime_type(self) -> str:
        """MIME type to serve.

        The extension wins over the declared type: Gemini frequently labels an `.xlsx` as a
        generic binary, and serving that as `application/octet-stream` loses the filename
        association some clients rely on. With neither signal the file is opaque.
        """
        ext = self.extension
        if ext in DOCUMENT_MIME_TYPES:
            return DOCUMENT_MIME_TYPES[ext]
        if self.mime_type:
            return self.mime_type
        guessed = mimetypes.guess_type(self.name or self.title)[0]
        return guessed or "application/octet-stream"

    @property
    def is_media(self) -> bool:
        """Whether the file is image, audio or video rather than a document.

        Media arrives through dedicated candidate fields that name it explicitly, so a file
        discovered by scanning only counts as media when its own name says so. This keeps a
        `report.png` from being re-reported as a generated document attachment when it is
        really an image the caller already received.
        """
        return self.extension in MEDIA_EXTENSIONS
