"""LANSync wire protocol.

A length-prefixed message format used over a single TCP socket (optionally
wrapped in TLS). Each message has a 4-byte big-endian header length, then a
UTF-8 JSON header, then an optional binary payload whose length is given by
the header's `payload_size` field.

Authentication: every connection performs an HMAC-SHA256 challenge/response
using the destination's pairing key. This proves the source knows the key
without ever sending the key over the wire.

Operations the source initiates against the destination:
    HELLO         — handshake, OS detection, protocol version
    AUTH          — challenge/response using the pairing key
    LIST          — list a destination directory tree (relative to a mapping)
    PUT_FILE      — upload a file with size, mtime, optional sha256
    DELETE        — delete a file or empty directory on destination
    MKDIR         — ensure a directory exists on destination
    BYE           — clean shutdown
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
from typing import Optional, Tuple

PROTOCOL_VERSION = 1
DEFAULT_PORT = 50515
HEADER_LEN_FMT = "!I"  # 4-byte big-endian unsigned int

MAX_HEADER_BYTES = 1 << 20      # 1 MiB header cap (paranoia)
MAX_PAYLOAD_BYTES = 1 << 40     # 1 TiB payload cap (effectively unlimited)


# ------------------------- low-level framing -------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise ConnectionError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def send_header(sock: socket.socket, header: dict) -> None:
    """Send only the framed header. Payload (if any) must be sent next."""
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("header too large")
    sock.sendall(struct.pack(HEADER_LEN_FMT, len(header_bytes)))
    sock.sendall(header_bytes)


def recv_header(sock: socket.socket) -> dict:
    """Receive only the framed header. Caller is responsible for any payload."""
    (hlen,) = struct.unpack(HEADER_LEN_FMT, _recv_exact(sock, 4))
    if hlen > MAX_HEADER_BYTES:
        raise ValueError(f"header length {hlen} exceeds max")
    return json.loads(_recv_exact(sock, hlen).decode("utf-8"))


def send_message(sock: socket.socket, header: dict, payload: bytes = b"") -> None:
    """Send one framed message with an in-memory payload. Auto-sets
    payload_size on the header."""
    header = dict(header)
    header["payload_size"] = len(payload)
    send_header(sock, header)
    if payload:
        sock.sendall(payload)


def recv_message(sock: socket.socket) -> Tuple[dict, bytes]:
    """Receive one framed message and return (header, payload). Reads the
    full payload into memory — only safe for small payloads."""
    header = recv_header(sock)
    plen = int(header.get("payload_size", 0))
    if plen < 0 or plen > MAX_PAYLOAD_BYTES:
        raise ValueError(f"invalid payload size {plen}")
    payload = _recv_exact(sock, plen) if plen else b""
    return header, payload


def recv_payload_streaming(sock: socket.socket, size: int, sink, chunk: int = 1 << 20):
    """Read a payload of `size` bytes from `sock` and write it to `sink.write`.

    Used for large file transfers so we never hold the whole file in RAM.
    Returns the SHA-256 hex digest of the received bytes.
    """
    h = hashlib.sha256()
    remaining = size
    while remaining > 0:
        buf = sock.recv(min(chunk, remaining))
        if not buf:
            raise ConnectionError("peer closed mid-transfer")
        sink.write(buf)
        h.update(buf)
        remaining -= len(buf)
    return h.hexdigest()


def send_payload_streaming(sock: socket.socket, source, size: int, chunk: int = 1 << 20,
                           limiter=None):
    """Stream `size` bytes from `source.read` to `sock`. Returns sha256 hex."""
    h = hashlib.sha256()
    remaining = size
    while remaining > 0:
        buf = source.read(min(chunk, remaining))
        if not buf:
            raise IOError("source ended early")
        if limiter is not None:
            limiter.consume(len(buf))
        sock.sendall(buf)
        h.update(buf)
        remaining -= len(buf)
    return h.hexdigest()


# ------------------------- authentication -------------------------

def make_challenge() -> bytes:
    return secrets.token_bytes(32)


def compute_response(key: str, challenge: bytes) -> str:
    """HMAC-SHA256 of the challenge under the pairing key."""
    return hmac.new(key.encode("utf-8"), challenge, hashlib.sha256).hexdigest()


def verify_response(key: str, challenge: bytes, response_hex: str) -> bool:
    expected = compute_response(key, challenge)
    return hmac.compare_digest(expected, response_hex)


# ------------------------- key persistence -------------------------

def detect_os() -> str:
    if os.name == "nt":
        return "windows"
    import platform
    s = platform.system().lower()
    if "darwin" in s:
        return "macos"
    return "linux"


def generate_key() -> str:
    """Human-typeable pairing key. URL-safe, ~43 chars from 32 random bytes."""
    return secrets.token_urlsafe(32)

