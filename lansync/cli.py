"""Command-line interface.

Useful for cron / Task Scheduler / running on a headless box. The GUI is
the primary surface, but anything you can configure in the GUI can also
be run from the CLI by pointing it at an exported config.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from .config import BackupConfig
from .keystore import get_local_key
from .protocol import DEFAULT_PORT
from .server import DestinationServer
from .sync import SyncEngine, SyncProgress
from .tls import make_server_ssl_context


def _print_progress(p: SyncProgress) -> None:
    pct = (100 * p.bytes_done / p.bytes_total) if p.bytes_total else 0
    sys.stdout.write(
        f"\r[{p.mapping_index}/{p.mapping_total}] "
        f"{p.transferred} sent / {p.skipped} skip / {p.deleted} del / "
        f"{p.failed} fail  {pct:5.1f}%  {p.current_file[:40]:40}"
    )
    sys.stdout.flush()


def cmd_run(args) -> int:
    cfg = BackupConfig.load(args.config)
    if cfg.role != "source":
        print("config role is not 'source'; nothing to push", file=sys.stderr)
        return 2
    engine = SyncEngine(cfg, log=print, progress=_print_progress)
    engine.run()
    print()
    return 0


def cmd_listen(args) -> int:
    key = get_local_key()
    print(f"local pairing key: {key}")
    ctx = make_server_ssl_context()
    if ctx is None:
        print("WARNING: cryptography not installed; running plaintext")
    server = DestinationServer(
        key=key, port=args.port, ssl_context=ctx,
        log=lambda m: print(f"[server] {m}"),
    )
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_key(args) -> int:
    print(get_local_key())
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    gui_main()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="lansync", description="LAN file sync")
    sub = p.add_subparsers(dest="cmd")

    pg = sub.add_parser("gui", help="launch the GUI (default)")
    pg.set_defaults(func=cmd_gui)

    pl = sub.add_parser("listen", help="run as a destination listener")
    pl.add_argument("--port", type=int, default=DEFAULT_PORT)
    pl.set_defaults(func=cmd_listen)

    pr = sub.add_parser("run", help="run a saved or exported config as the source")
    pr.add_argument("config", help="path to a backup config JSON")
    pr.set_defaults(func=cmd_run)

    pk = sub.add_parser("key", help="print this machine's local pairing key")
    pk.set_defaults(func=cmd_key)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_gui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
