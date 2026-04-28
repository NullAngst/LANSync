"""Source-side client and sync engine.

The Source connects to a Destination, authenticates, then for each enabled
mapping:
  1. Walks the local source directory.
  2. Asks the Destination to LIST its mirror.
  3. Diffs the two trees:
       - PUT files that are new or whose size/mtime differs.
       - If the mapping has delete_extraneous=True, DELETE files/dirs on
         the destination that no longer exist on the source.
  4. Optionally renames source files to sanitized names (sanitize_mode=
     "rename") before transferring.
  5. Always sanitizes destination-side relative paths so Windows targets
     accept the names.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Callable, Dict, List, Optional, Tuple

from . import protocol as proto
from .config import BackupConfig, FolderMapping, TransferConfig
from .ratelimit import RateLimiter
from .sanitize import (
    needs_sanitization,
    sanitize_component,
    sanitize_relative_path,
)


# ------------------------- result/progress types -------------------------

@dataclass
class FileEntry:
    rel: str
    size: int
    mtime: int
    is_dir: bool = False


@dataclass
class SyncProgress:
    mapping_index: int = 0
    mapping_total: int = 0
    file_index: int = 0
    file_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_file: str = ""
    deleted: int = 0
    skipped: int = 0
    transferred: int = 0
    failed: int = 0
    message: str = ""


ProgressFn = Callable[[SyncProgress], None]
LogFn = Callable[[str], None]


# ------------------------- low-level connection helper -------------------------

class _Conn:
    """Thin wrapper that owns one authenticated socket to the destination."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._lock = threading.Lock()

    def request(self, header: dict, payload: bytes = b"") -> Tuple[dict, bytes]:
        with self._lock:
            proto.send_message(self.sock, header, payload)
            return proto.recv_message(self.sock)

    def begin_put(self, header: dict):
        """Send a PUT_FILE header, then stream the body, then read the reply.

        Caller must hold self._lock for the duration. Returns (header, payload)
        of the destination's reply.
        """
        return self._lock

    def close(self) -> None:
        try:
            with self._lock:
                proto.send_message(self.sock, {"op": "BYE"})
                proto.recv_message(self.sock)
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _connect(peer_ip: str, port: int, key: str, use_tls: bool, timeout: float = 15.0) -> _Conn:
    """Open one authenticated connection to the destination."""
    s = socket.create_connection((peer_ip, port), timeout=timeout)
    s.settimeout(None)
    if use_tls:
        # Self-signed certs are expected on a LAN, so don't verify the
        # certificate. The HMAC handshake provides authentication.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(s, server_hostname=peer_ip)

    proto.send_message(s, {"op": "HELLO", "protocol": proto.PROTOCOL_VERSION,
                           "os": proto.detect_os()})
    hdr, _ = proto.recv_message(s)
    if hdr.get("op") != "HELLO_OK":
        raise RuntimeError(f"handshake failed: {hdr}")

    hdr, payload = proto.recv_message(s)
    if hdr.get("op") != "CHALLENGE":
        raise RuntimeError("expected CHALLENGE")
    response = proto.compute_response(key, payload)
    proto.send_message(s, {"op": "AUTH", "response": response})
    hdr, _ = proto.recv_message(s)
    if hdr.get("op") != "AUTH_OK":
        raise RuntimeError("authentication rejected")
    return _Conn(s)


# ------------------------- the sync engine -------------------------

class SyncEngine:
    """Drives one sync run for a BackupConfig in the 'source' role."""

    def __init__(self, cfg: BackupConfig, log: LogFn = print,
                 progress: Optional[ProgressFn] = None,
                 cancel_event: Optional[threading.Event] = None):
        if cfg.role != "source":
            raise ValueError("SyncEngine is for role='source'")
        self.cfg = cfg
        self.log = log
        self.progress = progress or (lambda p: None)
        self.cancel = cancel_event or threading.Event()
        self.limiter = RateLimiter(cfg.transfer.speed_limit_kbps * 1024)
        self._port = cfg.transfer.port or proto.DEFAULT_PORT

    # ---- top-level entry point ----

    def run(self) -> SyncProgress:
        prog = SyncProgress(mapping_total=len([m for m in self.cfg.mappings if m.enabled]))

        # Open a small pool of connections — one per worker — so puts can
        # be parallel. The control connection also serves as a worker.
        n_workers = max(1, int(self.cfg.transfer.concurrency))
        connections: List[_Conn] = []
        try:
            for _ in range(n_workers):
                connections.append(_connect(
                    self.cfg.peer_ip, self._port, self.cfg.peer_key,
                    use_tls=self.cfg.transfer.use_tls,
                ))
        except Exception as e:
            for c in connections:
                c.close()
            raise RuntimeError(f"could not connect to {self.cfg.peer_ip}:{self._port}: {e}")

        try:
            for i, mapping in enumerate(self.cfg.mappings):
                if not mapping.enabled:
                    continue
                if self.cancel.is_set():
                    prog.message = "cancelled"
                    self.progress(prog)
                    break
                prog.mapping_index = i + 1
                prog.message = f"syncing {mapping.source_path} -> {mapping.dest_path}"
                self.log(prog.message)
                self.progress(prog)
                self._sync_mapping(mapping, connections, prog)
            prog.message = "done"
            self.progress(prog)
            return prog
        finally:
            for c in connections:
                c.close()

    # ---- per-mapping driver ----

    def _sync_mapping(self, mapping: FolderMapping, connections: List[_Conn],
                      prog: SyncProgress) -> None:
        src_root = Path(mapping.source_path)
        if not src_root.exists() or not src_root.is_dir():
            self.log(f"source missing: {src_root}")
            return

        # Optional: sanitize source filenames first so source==destination.
        if mapping.sanitize_mode == "rename":
            self._rename_source_in_place(src_root)

        # Walk the source.
        src_files: Dict[str, FileEntry] = {}
        src_dirs: List[str] = []
        for dirpath, dirnames, filenames in os.walk(src_root):
            base = Path(dirpath)
            for d in dirnames:
                rel = str((base / d).relative_to(src_root)).replace(os.sep, "/")
                src_dirs.append(rel)
            for fn in filenames:
                p = base / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = str(p.relative_to(src_root)).replace(os.sep, "/")
                src_files[rel] = FileEntry(rel=rel, size=st.st_size,
                                           mtime=int(st.st_mtime), is_dir=False)

        # For sanitize_mode == "copy" the destination uses sanitized names,
        # but we still index source by original names so we know what to
        # read locally. Build a (src_rel -> dest_rel) map.
        if mapping.sanitize_mode in ("copy", "rename"):
            dest_rel_for: Dict[str, str] = {
                s: sanitize_relative_path(s) for s in src_files.keys()
            }
            dest_dirs = [sanitize_relative_path(d) for d in src_dirs]
        else:
            dest_rel_for = {s: s for s in src_files.keys()}
            dest_dirs = list(src_dirs)

        # Detect sanitization collisions (two different source names that
        # collapse to the same destination name).
        seen: Dict[str, str] = {}
        collisions = []
        for src_rel, dest_rel in dest_rel_for.items():
            if dest_rel in seen and seen[dest_rel] != src_rel:
                collisions.append((seen[dest_rel], src_rel, dest_rel))
            else:
                seen[dest_rel] = src_rel
        for a, b, d in collisions:
            self.log(f"WARNING: sanitization collision: '{a}' and '{b}' both map to '{d}'")

        # Ask destination for its current state.
        # Entries are returned as a JSON payload, not in the header, so that
        # directories with tens of thousands of files don't hit the 1 MiB
        # header cap.
        ctrl = connections[0]
        reply, payload = ctrl.request({"op": "LIST", "root": mapping.dest_path})
        if reply.get("op") != "LIST_OK":
            raise RuntimeError(f"LIST failed: {reply}")
        raw_entries = json.loads(payload.decode("utf-8")) if payload else []

        dest_index: Dict[str, FileEntry] = {}
        dest_dir_set = set()
        for e in raw_entries:
            if e.get("is_dir"):
                dest_dir_set.add(e["rel"])
            else:
                dest_index[e["rel"]] = FileEntry(
                    rel=e["rel"], size=int(e["size"]),
                    mtime=int(e.get("mtime", 0)), is_dir=False,
                )

        # Diff: which files need PUT?
        to_put: List[Tuple[str, str, FileEntry]] = []  # (src_rel, dest_rel, entry)
        for src_rel, entry in src_files.items():
            dest_rel = dest_rel_for[src_rel]
            existing = dest_index.get(dest_rel)
            if existing is None:
                to_put.append((src_rel, dest_rel, entry))
            elif existing.size != entry.size or abs(existing.mtime - entry.mtime) > 1:
                to_put.append((src_rel, dest_rel, entry))
            else:
                prog.skipped += 1

        # Diff: which files/dirs to DELETE on destination?
        to_delete_files: List[str] = []
        to_delete_dirs: List[str] = []
        if mapping.delete_extraneous:
            wanted_files = set(dest_rel_for.values())
            wanted_dirs = set(dest_dirs)
            for d_rel in dest_index:
                if d_rel not in wanted_files:
                    to_delete_files.append(d_rel)
            # Delete deepest dirs first so they're empty when removed.
            for d_rel in sorted(dest_dir_set, key=lambda x: -x.count("/")):
                if d_rel not in wanted_dirs:
                    to_delete_dirs.append(d_rel)

        # Pre-create destination directories in sorted (shallowest-first) order.
        for d in sorted(dest_dirs, key=lambda x: x.count("/")):
            ctrl.request({"op": "MKDIR", "root": mapping.dest_path, "rel": d})

        # Update progress totals.
        prog.file_total += len(to_put)
        prog.bytes_total += sum(e.size for _, _, e in to_put)
        self.progress(prog)

        # Drive uploads concurrently across the connection pool.
        self._upload_all(mapping, to_put, connections, prog)

        # Delete extraneous files, then empty dirs.
        for rel in to_delete_files:
            if self.cancel.is_set():
                break
            ctrl.request({"op": "DELETE", "root": mapping.dest_path, "rel": rel})
            prog.deleted += 1
            prog.message = f"deleted {rel}"
            self.progress(prog)
        for rel in to_delete_dirs:
            if self.cancel.is_set():
                break
            ctrl.request({"op": "DELETE", "root": mapping.dest_path, "rel": rel})

    # ---- upload pool ----

    def _upload_all(self, mapping: FolderMapping,
                    to_put: List[Tuple[str, str, FileEntry]],
                    connections: List[_Conn], prog: SyncProgress) -> None:
        if not to_put:
            return
        # Round-robin connections to workers so each worker has its own.
        q: Queue = Queue()
        for item in to_put:
            q.put(item)

        n = len(connections)

        def worker(conn: _Conn):
            while True:
                if self.cancel.is_set():
                    return
                try:
                    src_rel, dest_rel, entry = q.get_nowait()
                except Exception:
                    return
                try:
                    self._put_one(mapping, conn, src_rel, dest_rel, entry, prog)
                except Exception as e:
                    prog.failed += 1
                    self.log(f"FAILED {src_rel}: {e}")
                    self.progress(prog)
                finally:
                    q.task_done()

        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(worker, c) for c in connections]
            for f in as_completed(futures):
                f.result()

    def _put_one(self, mapping: FolderMapping, conn: _Conn,
                 src_rel: str, dest_rel: str, entry: FileEntry,
                 prog: SyncProgress) -> None:
        local_path = Path(mapping.source_path) / src_rel
        size = entry.size
        prog.current_file = dest_rel
        prog.message = f"sending {dest_rel}"
        self.progress(prog)

        # Optionally compute hash up front for verification.
        sha = None
        if self.cfg.transfer.verify_hash:
            h = hashlib.sha256()
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()

        header = {
            "op": "PUT_FILE",
            "root": mapping.dest_path,
            "rel": dest_rel,
            "size": size,
            "mtime": entry.mtime,
            "payload_size": size,
        }
        if sha:
            header["sha256"] = sha

        with conn._lock:
            proto.send_header(conn.sock, header)
            with open(local_path, "rb") as f:
                proto.send_payload_streaming(
                    conn.sock, f, size,
                    chunk=self.cfg.transfer.chunk_size,
                    limiter=self.limiter,
                )
            reply, _ = proto.recv_message(conn.sock)

        if reply.get("op") != "PUT_OK":
            raise RuntimeError(f"PUT rejected: {reply}")
        prog.transferred += 1
        prog.file_index += 1
        prog.bytes_done += size
        self.progress(prog)

    # ---- optional source-side rename for 1:1 mirroring ----

    def _rename_source_in_place(self, src_root: Path) -> None:
        """Rename source files/dirs whose names need sanitization.

        Walk bottom-up so renaming a directory doesn't invalidate the paths
        of its children mid-walk.
        """
        for dirpath, dirnames, filenames in os.walk(src_root, topdown=False):
            base = Path(dirpath)
            for name in filenames + dirnames:
                clean = sanitize_component(name)
                if clean != name:
                    old = base / name
                    new = base / clean
                    if new.exists():
                        self.log(f"skip rename (would clobber): {old} -> {new}")
                        continue
                    try:
                        old.rename(new)
                        self.log(f"renamed source: {old} -> {new}")
                    except OSError as e:
                        self.log(f"rename failed {old}: {e}")
