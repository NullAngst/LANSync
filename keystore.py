"""The local pairing key.

On first launch we generate a random key and store it in the per-user
config directory. The UI displays this key so the *other* machine can be
told what it is. The other machine pastes it into its "peer key" field so
its source-mode connections are authenticated by this destination.
"""
from __future__ import annotations

from pathlib import Path

from .config import default_config_dir
from .protocol import generate_key

_KEY_FILE = "local.key"


def get_local_key() -> str:
    p: Path = default_config_dir() / _KEY_FILE
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        if text:
            return text
    key = generate_key()
    p.write_text(key, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return key


def regenerate_local_key() -> str:
    p: Path = default_config_dir() / _KEY_FILE
    key = generate_key()
    p.write_text(key, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return key
