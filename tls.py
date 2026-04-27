"""Self-signed TLS support.

LANSync runs on a LAN where there is no CA. We generate one self-signed
certificate per install and present it on the destination side. The source
verifies the connection via the HMAC handshake, not the cert chain, so
TLS here is purely for confidentiality and integrity on the wire.

If the `cryptography` package is unavailable we fall back to plaintext
sockets and tell the user.
"""
from __future__ import annotations

import datetime as _dt
import ssl
from pathlib import Path
from typing import Optional, Tuple

from .config import default_config_dir


def _cert_paths() -> Tuple[Path, Path]:
    d = default_config_dir()
    return d / "lansync.cert.pem", d / "lansync.key.pem"


def ensure_cert() -> Optional[Tuple[Path, Path]]:
    """Generate a long-lived self-signed cert if none exists. Return paths,
    or None if the optional `cryptography` dependency is missing."""
    cert_path, key_path = _cert_paths()
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lansync.local")])
    now = _dt.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, key_path


def make_server_ssl_context() -> Optional[ssl.SSLContext]:
    paths = ensure_cert()
    if paths is None:
        return None
    cert_path, key_path = paths
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx
