"""Filename sanitization for cross-platform sync.

Windows is the strictest target, so the rules below normalize a name into
something that is legal on Windows, macOS, and Linux simultaneously. Two
operating modes are supported by the sync engine:

  - "copy": the original file on the source is left alone; the file is
    written to the destination under a sanitized name.
  - "rename": the source file is renamed to the sanitized name first, so
    the source and destination stay byte-identical.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

# Characters Windows forbids in filenames.
_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Names Windows reserves regardless of extension.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str, replacement: str = "_") -> str:
    """Sanitize a single path component (no separators).

    Returns a name safe on Windows, macOS, and Linux.
    """
    if not name or name in (".", ".."):
        return name

    # Normalize Unicode to a stable form so visually identical names match.
    name = unicodedata.normalize("NFC", name)

    # Replace forbidden characters.
    name = _WIN_FORBIDDEN.sub(replacement, name)

    # Windows trims trailing dots and spaces; do the same so round-trips
    # produce a name that exists on disk after a copy.
    name = name.rstrip(" .")

    if not name:
        name = replacement

    # Avoid reserved device names. Match against the stem only.
    stem, dot, ext = name.partition(".")
    if stem.upper() in _WIN_RESERVED:
        stem = stem + replacement
        name = stem + dot + ext

    return name


def sanitize_relative_path(rel: str) -> str:
    """Sanitize each component of a forward-slash relative path."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(sanitize_component(p) for p in parts)


def needs_sanitization(rel: str) -> bool:
    """True if any component would be altered by sanitize_relative_path."""
    return sanitize_relative_path(rel) != rel.replace("\\", "/").lstrip("/")


def to_native(rel: str, target_os: str) -> str:
    """Convert a forward-slash relative path to the target OS's native form."""
    if target_os == "windows":
        return str(PureWindowsPath(*rel.split("/")))
    return str(PurePosixPath(*rel.split("/")))
