"""Locate the documents Gemini's code execution produced, without knowing where they sit.

Gemini delivers generated documents (`report.xlsx`, `summary.docx`, a saved `.py`) through an
opaque `googleusercontent.com` download URL whose JSPB field number is not published and has
been observed to move. Rather than hard-code a field and break on the next renumbering, this
module walks the candidate payload and recognises the URLs by shape.

Two independent signals are used, and either one alone is enough:

* a `googleusercontent.com` URL carrying a known document extension, and
* the `sandbox:/mnt/data/<name>` placeholder Gemini writes into reply text, which names the
  file even when the download URL does not.

The scan is deliberately conservative: it skips image/video/audio markers and the known
thumbnail hosts, because those already have dedicated parsers and re-reporting them would
duplicate every image in a reply.
"""

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..constants import DOCUMENT_MIME_TYPES, DOCUMENT_NAME_RE
from ..types.file import GeneratedFile

# Hosts that serve reply images and generated media. Their URLs are handled by the image,
# video and music parsers, so treating them as documents would duplicate that output.
_MEDIA_HOST_RE = re.compile(r"(?:^|\.)(?:googleusercontent\.com|gstatic\.com)$", re.IGNORECASE)

# Path markers that address reply media and nothing else. `/gg-dl/` is deliberately NOT
# here: it is the delivery path for generated documents too, and their URLs carry a bare
# token with no extension to tell the two apart. Media is told apart by `exclude_urls`
# instead - the image, video and music parsers have already claimed their own URLs, so what
# remains on that path is ours to consider.
_MEDIA_PATH_MARKERS = ("encrypted-tbn", "image_generation_content", "gds-ping")

_URL_RE = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.IGNORECASE)

_MAX_SCAN_NODES = 20000
_MAX_DEPTH = 24

# A sandbox placeholder runs to the end of its line because a filename may contain spaces
# (`báo cáo tháng.xlsx`), which stops it being a single token. The scan stops at the next
# placeholder too, so a reply listing two files on one line yields both. The line is then
# trimmed back to the last known document extension, leaving trailing prose out of the name.
_SANDBOX_LINE_RE = re.compile(
    r"sandbox:/mnt/(?:data|code)/((?:(?!sandbox:)[^\n\r<>\"'`])+)", re.IGNORECASE
)


def _suffix_of(url: str) -> str:
    """Lowercase extension carried by a URL path or query, or an empty string."""
    parsed = urlparse(url)
    for candidate in (unquote(parsed.path), unquote(parsed.query)):
        dot = candidate.rfind(".")
        if dot < 0:
            continue
        ext = candidate[dot:].split("?")[0].split("#")[0].lower()
        if ext:
            return ext
    return ""


def _is_media_url(url: str) -> bool:
    """Whether a URL addresses media that a dedicated parser already handles.

    Only unambiguous media paths are rejected. The generated-download path `/gg-dl/` serves
    images and documents alike and its URLs are opaque, so it is *not* treated as media here:
    an image that slipped past the image parser would then be reported as a document, which
    is the cheaper mistake to make. What keeps that in check is that a document is only ever
    reported on a positive signal - its own extension, or a `sandbox:` name in the reply text.
    """
    lowered = url.lower()
    if _MEDIA_HOST_RE.search(urlparse(lowered).hostname or ""):
        return any(marker in lowered for marker in _MEDIA_PATH_MARKERS)
    return False


def _document_extension(url: str) -> str:
    """Document extension carried by a URL's path or query, or an empty string.

    Gemini's document URLs are frequently opaque (`/gg-dl/AbCdEf...`), so an empty result is
    the common case and is not an error.
    """
    ext = _suffix_of(url)
    return ext if ext in DOCUMENT_MIME_TYPES else ""


def _filename_from_url(url: str) -> str:
    """Best-effort filename from a download URL.

    Prefers an explicit query parameter, because Gemini's delivery URLs often end in an
    opaque token with the real name passed as `?fn=report.docx`. Falls back to the path
    basename, appending the detected extension when the basename itself has none.
    """
    parsed = urlparse(url)
    for key in ("fn", "filename", "file", "name"):
        values = parse_qs(parsed.query).get(key)
        if values and values[0]:
            candidate = unquote(values[0]).strip()
            if candidate and "/" not in candidate and "\\" not in candidate:
                return candidate
    base = unquote(parsed.path.rsplit("/", 1)[-1]).strip()
    ext = _document_extension(url)
    if ext and not base.lower().endswith(ext):
        return f"{base}{ext}"
    return base


def collect_sandbox_names(text: str) -> list[str]:
    """Filenames Gemini reported as produced, from `sandbox:` placeholders in reply text.

    This is the only place the original name appears in the clear, so it drives both the
    delivered filename and the detection that a document was produced at all. The name ends
    at its extension: everything after it on the line is prose, not part of the filename.
    A reply may list several files on one line, so the scan resumes after each match.
    """
    names: list[str] = []
    rest = text or ""
    while True:
        match = _SANDBOX_LINE_RE.search(rest)
        if not match:
            break
        rest = rest[match.end() :]
        ext_match = DOCUMENT_NAME_RE.search(match.group(1))
        if not ext_match:
            continue
        name = match.group(1)[: ext_match.end()].strip().strip(".,;:)]}\"'`").lstrip(".")
        # A name that survived must look like a bare filename; anything with a separator in
        # it is a path fragment and must never be joined onto a cache path.
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            continue
        if name not in names:
            names.append(name)
    return names


def _iter_strings(node: Any, depth: int = 0):
    """Yield every string in the payload, bounded in both breadth and depth.

    The bounds matter: a candidate payload is attacker-adjacent data in the sense that its
    shape comes from a remote server, and an unbounded recursive walk over a malformed or
    cyclic structure is a denial-of-service vector.
    """
    if depth > _MAX_DEPTH:
        return
    seen = 0
    stack: list[tuple[Any, int]] = [(node, depth)]
    while stack:
        current, level = stack.pop()
        seen += 1
        if seen > _MAX_SCAN_NODES:
            return
        if isinstance(current, str):
            yield current
        elif isinstance(current, dict):
            for key, value in current.items():
                if isinstance(key, str):
                    yield key
                stack.append((value, level + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend((item, level + 1) for item in current)


def collect_generated_files(
    candidate_data: Any, text: str = "", exclude_urls: set[str] | None = None
) -> list[GeneratedFile]:
    """Find documents Gemini produced, from the candidate payload and reply text.

    Parameters
    ----------
    candidate_data: `Any`
        Raw candidate list as received from the API.
    text: `str`, optional
        Candidate reply text, scanned for `sandbox:` placeholders.
    exclude_urls: `set[str]`, optional
        URLs already delivered as image, video or audio. They are skipped so a file the
        media parsers claimed is not reported a second time as a document.

    Returns
    -------
    `list[GeneratedFile]`
        One entry per distinct document found, in the order first encountered.

    """
    excluded = exclude_urls or set()
    sandbox_names = collect_sandbox_names(text)

    # Every URL in the payload that is not media. Kept in document order because that is the
    # order Gemini reports its outputs in, which is what lets an opaque URL be paired with
    # the name stated in text.
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in _iter_strings(candidate_data):
        for url in _URL_RE.findall(raw):
            url = url.rstrip(".,;")
            if url in excluded or url in seen or _is_media_url(url):
                continue
            seen.add(url)
            candidates.append(url)

    files: list[GeneratedFile] = []
    claimed: set[str] = set()

    def claim(url: str, name: str) -> None:
        claimed.add(url)
        files.append(GeneratedFile(url=url, title=name or url, name=name))

    # Pass 1: a URL that names its own document extension. Nothing to infer, so it wins.
    for url in candidates:
        if _document_extension(url):
            claim(url, _filename_from_url(url))

    # Pass 2: pair each `sandbox:` name with the next unclaimed URL. This is the common real
    # case - Gemini's download URLs carry a bare token and the name only exists in the text.
    unclaimed = [u for u in candidates if u not in claimed]
    for index, name in enumerate(sandbox_names):
        if index < len(unclaimed):
            claim(unclaimed[index], name)
        else:
            # Gemini named a file we found no URL for. Report it anyway: the caller learns the
            # document exists and can ask Gemini for its contents as text. Inventing a URL
            # here would hand the client a dead download link.
            files.append(GeneratedFile(url="", title=name, name=name))

    return [f for f in files if not f.is_media]
