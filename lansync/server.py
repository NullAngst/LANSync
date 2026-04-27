"""Destination-side server.

Listens on a TCP port. When a Source connects it:
  1. exchanges HELLO and reports OS/version,
  2. authenticates the source via HMAC challenge using the local key,
  3. services LIST / MKDIR / PUT_FILE / DELETE / BYE messages.

Multiple sources may connect concurrently; each gets a worker thread.
"""
from __future__ import annotations

import os
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import protocol as proto


class DestinationServer:
    """Accepts source connections and applies their changes locally."""

    def __init__(
        self,
        key: str,
        port: int = proto.DEFAULT_PORT,
        bind_host: str = "0.0.0.0",
        ssl_context: Optional[ssl.SSLContext] = None,
        log: Optional[Callable[[str], None]] = None,
        # Allowed destination roots — incoming PUTs must resolve under one
        # of these. If None, all paths are allowed (open mode).
        allowed_roots: Optional[list[str]] = None,
    ):
        self.key = key
        self.port = port
        self.bind_host = bind_host
        self.ssl_context = ssl_context
        self.log = log or (lambda m: None)
        self.allowed_roots = [str(Path(r).resolve()) for r in (allowed_roots or [])]
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self.log(f"destination listening on {self.bind_host}:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self.log("destination stopped")

    # ---- accept loop ----

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True)
            t.start()

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        self.log(f"connection from {peer}")
        try:
            if self.ssl_context is not None:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)

            # 1) HELLO
            hdr, _ = proto.recv_message(conn)
            if hdr.get("op") != "HELLO":
                proto.send_message(conn, {"op": "ERROR", "msg": "expected HELLO"})
                return
            proto.send_message(conn, {
                "op": "HELLO_OK",
                "protocol": proto.PROTOCOL_VERSION,
                "os": proto.detect_os(),
            })

            # 2) AUTH challenge/response
            challenge = proto.make_challenge()
            proto.send_message(conn, {"op": "CHALLENGE"}, payload=challenge)
            hdr, _ = proto.recv_message(conn)
            if hdr.get("op") != "AUTH" or not proto.verify_response(
                self.key, challenge, hdr.get("response", "")
            ):
                proto.send_message(conn, {"op": "ERROR", "msg": "auth failed"})
                self.log(f"auth failed from {peer}")
                return
            proto.send_message(conn, {"op": "AUTH_OK"})
            self.log(f"authenticated {peer}")

            # 3) command loop
            while True:
                hdr = proto.recv_header(conn)
                op = hdr.get("op")
                if op == "BYE":
                    # Drain any payload (should be 0) then ack.
                    plen = int(hdr.get("payload_size", 0))
                    if plen:
                        proto._recv_exact(conn, plen)
                    proto.send_message(conn, {"op": "BYE_OK"})
                    return
                if op == "PUT_FILE":
                    # Stream the body straight to disk — never buffer the
                    # whole file in RAM.
                    self._handle_put_streaming(conn, hdr)
                    continue
                # Other ops have small payloads; read them now.
                plen = int(hdr.get("payload_size", 0))
                payload = proto._recv_exact(conn, plen) if plen else b""
                handler = self._handlers.get(op)
                if handler is None:
                    proto.send_message(conn, {"op": "ERROR", "msg": f"unknown op {op}"})
                    continue
                try:
                    handler(self, conn, hdr, payload)
                except Exception as e:
                    proto.send_message(conn, {"op": "ERROR", "msg": str(e)})
        except (ConnectionError, OSError, ssl.SSLError) as e:
            self.log(f"connection from {peer} ended: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---- safety: confine paths to allowed roots ----

    def _resolve_under_root(self, root: str, rel: str) -> Path:
        root_p = Path(root).resolve()
        if self.allowed_roots and str(root_p) not in self.allowed_roots:
            raise PermissionError(f"root {root_p} not allowed")
        # Normalize separators in the relative path.
        rel = rel.replace("\\", "/").lstrip("/")
        target = (root_p / rel).resolve()
        # Confine: target must be under root.
        try:
            target.relative_to(root_p)
        except ValueError:
            raise PermissionError(f"path escape blocked: {rel}")
        return target

    # ---- handlers ----

    def _h_list(self, conn, hdr, _payload):
        root = hdr["root"]
        root_p = self._resolve_under_root(root, "")
        entries = []
        if root_p.exists():
            for dirpath, dirnames, filenames in os.walk(root_p):
                base = Path(dirpath)
                for name in filenames:
                    p = base / name
                    try:
                        st = p.stat()
                        rel = str(p.relative_to(root_p)).replace(os.sep, "/")
                        entries.append({
                            "rel": rel,
                            "size": st.st_size,
                            "mtime": int(st.st_mtime),
                            "is_dir": False,
                        })
                    except OSError:
                        pass
                for name in dirnames:
                    p = base / name
                    try:
                        rel = str(p.relative_to(root_p)).replace(os.sep, "/")
                        entries.append({"rel": rel, "size": 0, "mtime": 0, "is_dir": True})
                    except OSError:
                        pass
        proto.send_message(conn, {"op": "LIST_OK", "entries": entries})

    def _h_mkdir(self, conn, hdr, _payload):
        target = self._resolve_under_root(hdr["root"], hdr["rel"])
        target.mkdir(parents=True, exist_ok=True)
        proto.send_message(conn, {"op": "MKDIR_OK"})

    def _handle_put_streaming(self, conn, hdr) -> None:
        """Streaming PUT: read the file body straight from the socket to disk."""
        try:
            target = self._resolve_under_root(hdr["root"], hdr["rel"])
        except Exception as e:
            # Drain the payload so the protocol stays in sync, then error.
            plen = int(hdr.get("payload_size", 0))
            if plen:
                # Read and discard.
                remaining = plen
                while remaining > 0:
                    chunk = conn.recv(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            proto.send_message(conn, {"op": "ERROR", "msg": str(e)})
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        size = int(hdr["size"])
        plen = int(hdr.get("payload_size", 0))
        if plen != size:
            # Drain and error.
            remaining = plen
            while remaining > 0:
                chunk = conn.recv(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            proto.send_message(conn, {"op": "ERROR", "msg": "size mismatch"})
            return
        expected_sha = hdr.get("sha256")
        tmp = target.with_suffix(target.suffix + ".lansync.part")
        try:
            with open(tmp, "wb") as f:
                got_sha = proto.recv_payload_streaming(conn, size, f)
            if expected_sha and got_sha != expected_sha:
                tmp.unlink(missing_ok=True)
                proto.send_message(conn, {"op": "ERROR", "msg": "hash mismatch"})
                return
            mtime = hdr.get("mtime")
            os.replace(tmp, target)
            if mtime is not None:
                try:
                    os.utime(target, (time.time(), int(mtime)))
                except OSError:
                    pass
            proto.send_message(conn, {"op": "PUT_OK", "sha256": got_sha})
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _h_put_file(self, conn, hdr, payload):
        """Compatibility shim — actual handling is in _handle_put_streaming."""
        proto.send_message(conn, {"op": "ERROR", "msg": "PUT must be streamed"})

    def _h_delete(self, conn, hdr, _payload):
        target = self._resolve_under_root(hdr["root"], hdr["rel"])
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                pass  # not empty; leave it
        elif target.exists():
            target.unlink()
        proto.send_message(conn, {"op": "DELETE_OK"})

    _handlers = {
        "LIST": _h_list,
        "MKDIR": _h_mkdir,
        "PUT_FILE": _h_put_file,
        "DELETE": _h_delete,
    }
