"""End-to-end integration test.

Spins up a destination server on localhost, configures a source backup,
runs the engine, and checks the destination tree matches the source —
including delete-extraneous and sanitization paths.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Make the package importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lansync.config import BackupConfig, FolderMapping, TransferConfig
from lansync.server import DestinationServer
from lansync.sync import SyncEngine
from lansync.protocol import generate_key
from lansync.sanitize import sanitize_relative_path


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def run_test(label, fn):
    print(f"\n=== {label} ===")
    try:
        fn()
        print(f"OK: {label}")
    except AssertionError as e:
        print(f"FAIL: {label}: {e}")
        raise


def test_basic_sync(port):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "movies" / "a.mkv", b"hello world")
        write(src / "movies" / "sub" / "b.txt", b"x" * 10000)
        write(src / "movies" / "c.bin", os.urandom(50000))

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                name="t", role="source",
                peer_ip="127.0.0.1", peer_key=key, peer_os="linux",
                mappings=[FolderMapping(
                    source_path=str(src / "movies"),
                    dest_path=str(dst / "movies"),
                    delete_extraneous=False,
                    sanitize_mode="off",
                )],
                transfer=TransferConfig(
                    concurrency=2, port=port, use_tls=False, verify_hash=True,
                ),
            )
            engine = SyncEngine(cfg, log=print)
            engine.run()
        finally:
            server.stop()

        assert (dst / "movies" / "a.mkv").read_bytes() == b"hello world"
        assert (dst / "movies" / "sub" / "b.txt").read_bytes() == b"x" * 10000
        assert (dst / "movies" / "c.bin").stat().st_size == 50000


def test_delete_extraneous(port):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "m" / "new.dat", b"new")
        # Pre-existing extra on destination that should be removed.
        write(dst / "m" / "old.dat", b"old")
        write(dst / "m" / "junkdir" / "leftover.txt", b"junk")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                role="source", peer_ip="127.0.0.1", peer_key=key,
                mappings=[FolderMapping(
                    source_path=str(src / "m"),
                    dest_path=str(dst / "m"),
                    delete_extraneous=True,
                    sanitize_mode="off",
                )],
                transfer=TransferConfig(port=port, use_tls=False),
            )
            SyncEngine(cfg, log=print).run()
        finally:
            server.stop()

        assert (dst / "m" / "new.dat").read_bytes() == b"new"
        assert not (dst / "m" / "old.dat").exists(), "extraneous file should be deleted"
        assert not (dst / "m" / "junkdir" / "leftover.txt").exists()


def test_no_delete_when_disabled(port):
    """Per-mapping toggle: delete_extraneous=False must preserve extras."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "m" / "new.dat", b"new")
        write(dst / "m" / "keepme.dat", b"keep")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                role="source", peer_ip="127.0.0.1", peer_key=key,
                mappings=[FolderMapping(
                    source_path=str(src / "m"),
                    dest_path=str(dst / "m"),
                    delete_extraneous=False,
                )],
                transfer=TransferConfig(port=port, use_tls=False),
            )
            SyncEngine(cfg, log=print).run()
        finally:
            server.stop()

        assert (dst / "m" / "keepme.dat").read_bytes() == b"keep", \
            "extras must be preserved when delete_extraneous=False"


def test_sanitize_copy(port):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        # File with a Windows-illegal char.
        bad_name = "Movie: The Best?.mkv"
        write(src / "m" / bad_name, b"data")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                role="source", peer_ip="127.0.0.1", peer_key=key,
                mappings=[FolderMapping(
                    source_path=str(src / "m"),
                    dest_path=str(dst / "m"),
                    sanitize_mode="copy",
                )],
                transfer=TransferConfig(port=port, use_tls=False),
            )
            SyncEngine(cfg, log=print).run()
        finally:
            server.stop()

        # Source untouched.
        assert (src / "m" / bad_name).exists()
        # Destination has sanitized name.
        sanitized = sanitize_relative_path(bad_name)
        assert (dst / "m" / sanitized).read_bytes() == b"data", \
            f"expected sanitized file at {sanitized}"


def test_idempotent_skip(port):
    """Second run with no source changes should send nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "m" / "a.dat", b"hello")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                role="source", peer_ip="127.0.0.1", peer_key=key,
                mappings=[FolderMapping(
                    source_path=str(src / "m"),
                    dest_path=str(dst / "m"),
                )],
                transfer=TransferConfig(port=port, use_tls=False),
            )
            r1 = SyncEngine(cfg, log=print).run()
            assert r1.transferred == 1
            r2 = SyncEngine(cfg, log=print).run()
            assert r2.transferred == 0, f"second run should skip, sent {r2.transferred}"
            assert r2.skipped == 1
        finally:
            server.stop()


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = BackupConfig(
            name="My Movies",
            role="source",
            peer_ip="192.168.1.10",
            peer_key="abc",
            mappings=[FolderMapping("/a", "/b", delete_extraneous=True,
                                    sanitize_mode="copy")],
            transfer=TransferConfig(concurrency=8, speed_limit_kbps=2048),
        )
        p = Path(tmp) / "x.json"
        cfg.save(p)
        cfg2 = BackupConfig.load(p)
        assert cfg2.name == cfg.name
        assert cfg2.mappings[0].delete_extraneous
        assert cfg2.transfer.concurrency == 8


def test_auth_failure(port):
    """Source with a bad key must be rejected, not silently allowed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "m" / "a.dat", b"x")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            cfg = BackupConfig(
                role="source", peer_ip="127.0.0.1", peer_key="wrong-key",
                mappings=[FolderMapping(str(src / "m"), str(dst / "m"))],
                transfer=TransferConfig(port=port, use_tls=False),
            )
            try:
                SyncEngine(cfg, log=print).run()
            except RuntimeError as e:
                assert "auth" in str(e).lower() or "rejected" in str(e).lower()
                return
            raise AssertionError("auth should have failed")
        finally:
            server.stop()


def test_path_escape_blocked(port):
    """A malicious source must not be able to write outside the dest root."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"; dst = tmp / "dst"
        src.mkdir(); dst.mkdir()
        write(src / "m" / "a.dat", b"x")

        key = generate_key()
        server = DestinationServer(key=key, port=port, log=print, ssl_context=None)
        server.start()
        try:
            # Send a manual MKDIR with .. in it.
            import socket
            from lansync import protocol as proto
            s = socket.create_connection(("127.0.0.1", port))
            proto.send_message(s, {"op": "HELLO"})
            proto.recv_message(s)
            hdr, payload = proto.recv_message(s)
            proto.send_message(s, {"op": "AUTH",
                                    "response": proto.compute_response(key, payload)})
            proto.recv_message(s)
            proto.send_message(s, {"op": "MKDIR", "root": str(dst / "m"),
                                    "rel": "../../etc/evil"})
            reply, _ = proto.recv_message(s)
            assert reply.get("op") == "ERROR", f"escape must be rejected, got {reply}"
            s.close()
        finally:
            server.stop()


def main():
    import random
    used = set()
    def next_port():
        while True:
            p = random.randint(52000, 59000)
            if p not in used:
                used.add(p)
                return p

    run_test("config round-trip", test_config_roundtrip)
    run_test("basic sync", lambda: test_basic_sync(next_port()))
    run_test("delete extraneous", lambda: test_delete_extraneous(next_port()))
    run_test("preserve when disabled", lambda: test_no_delete_when_disabled(next_port()))
    run_test("sanitize copy mode", lambda: test_sanitize_copy(next_port()))
    run_test("idempotent skip", lambda: test_idempotent_skip(next_port()))
    run_test("auth rejection", lambda: test_auth_failure(next_port()))
    run_test("path escape blocked", lambda: test_path_escape_blocked(next_port()))
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
