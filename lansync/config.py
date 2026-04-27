"""Configuration model.

A BackupConfig is a named, exportable JSON file describing one sync job:
who connects to whom, which folders map to which, and per-mapping options
like whether to delete extraneous files on the destination.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class FolderMapping:
    """One source folder mapped to one destination folder.

    `source_path` is read on the Source machine.
    `dest_path` is written on the Destination machine.
    `delete_extraneous` controls whether files present on destination but
    absent from source are removed (configurable per mapping, as requested).
    `sanitize_mode` is one of:
        "off"     — copy filenames byte-for-byte (may fail on Windows dest).
        "copy"    — sanitize only the destination copy; source untouched.
        "rename"  — rename the source file too, keeping a 1:1 mirror.
    """
    source_path: str
    dest_path: str
    delete_extraneous: bool = False
    sanitize_mode: str = "copy"  # off | copy | rename
    enabled: bool = True


@dataclass
class TransferConfig:
    """Transfer-layer tuning."""
    concurrency: int = 4              # parallel file transfers
    speed_limit_kbps: int = 0         # 0 = unlimited, total across workers
    chunk_size: int = 1024 * 1024     # bytes per network read/write
    verify_hash: bool = True          # SHA-256 verify after transfer
    port: int = 0                     # 0 = use default (50515)
    use_tls: bool = True              # encrypt transport


@dataclass
class BackupConfig:
    """Top-level named backup configuration."""
    name: str = "Untitled Backup"
    role: str = "source"              # source | destination
    peer_ip: str = ""                 # the OTHER machine's IP
    peer_key: str = ""                # paired key from the OTHER machine
    peer_os: str = "auto"             # auto | linux | windows | macos
    mappings: List[FolderMapping] = field(default_factory=list)
    transfer: TransferConfig = field(default_factory=TransferConfig)
    schema_version: int = 1

    # ---- (de)serialization ----

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "BackupConfig":
        d = json.loads(text)
        mappings = [FolderMapping(**m) for m in d.pop("mappings", [])]
        transfer = TransferConfig(**d.pop("transfer", {}))
        return cls(mappings=mappings, transfer=transfer, **d)

    def save(self, path: str | os.PathLike) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | os.PathLike) -> "BackupConfig":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def default_config_dir() -> Path:
    """Per-user config directory for stored backups and the local key."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    p = base / "lansync"
    p.mkdir(parents=True, exist_ok=True)
    return p
