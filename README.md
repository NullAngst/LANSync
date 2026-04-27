# LANSync

LAN file sync with a Tkinter GUI, key-based pairing, and named importable
backup configs. Push folders from one machine to another over a local
network, with optional Windows-safe filename sanitization, configurable
deletion of files that no longer exist on the source, and a tunable
transfer layer with bandwidth limits and SHA-256 verification.

Works between **Linux ↔ Linux**, **Windows ↔ Windows**, and **Linux ↔
Windows** (and macOS, in either direction).

## Features

* **Single launchable app on both machines.** No need to install or
  configure SSH, SMB, FTP, or rsync. One Python program on each side.
* **Key-based pairing.** On first launch each machine generates a random
  pairing key. The destination shows its key; the source enters that key
  plus the destination's IP. Authentication is HMAC-SHA256 challenge/
  response — the key is never sent on the wire.
* **TLS on the LAN.** Connections are encrypted with a self-signed
  certificate generated on first launch. Falls back to plaintext if the
  optional `cryptography` package is missing.
* **Named backups, exportable as JSON.** Save many configurations
  ("Movies → NAS", "Documents → Laptop"). Export a config as a single
  JSON file you can import on another install.
* **Per-mapping options.**
  * **Delete extraneous:** off by default. Turn it on for a folder where
    you want a strict mirror — replace a CD on source, the old files
    disappear from destination on the next sync. Leave it off for a
    folder where you sweep the source regularly but want destination to
    keep accumulating.
  * **Filename sanitization:** off / sanitize-on-copy / rename-source-too.
    Use the rename mode if you want a strict 1:1 mirror including
    filenames; use copy mode if your Linux source has Windows-illegal
    characters that you'd rather not rewrite.
* **Transfer settings.** Parallel file uploads, total-bandwidth cap in
  KB/s, network chunk size, optional SHA-256 verification, custom port.
* **Idempotent.** Re-running a backup only sends files that are new or
  whose size or mtime differ.
* **Cross-platform paths.** Forward slashes are used on the wire and
  resolved natively on each side, so `/mnt/media/movies → E:/backups/
  movies` works regardless of which side is which OS.

## How it maps to the problem

| What you asked for | Where it lives |
|---|---|
| Launch on both systems, set one Source, one Destination | Role dropdown per backup; the Destination listener is a separate Start/Stop in the header so one machine can do both at once |
| Set folders to copy to destination | Add folder → browse → set destination path |
| Pairing via IP + key shown on the other UI | Header shows local key with Copy; backup form has Peer IP + Peer key fields |
| Export/import named backup config | Sidebar: New / Duplicate / Delete / Import… / Export… |
| Sanitize Linux→Windows filenames, two modes | Per-mapping setting: off / sanitize copy only / rename source too |
| Configurable transport (FTP/SFTP/SMB/etc) | Custom protocol with HMAC auth + TLS, hash verify, parallel files, bandwidth cap, custom port |
| Verify moved files match host | `verify_hash` toggle: SHA-256 computed on source and re-computed on destination after write; mismatch rejects the file |
| Concurrency, speed limit | `Parallel files` and `Speed limit (KB/s, 0=unlimited)` in transfer settings |
| Remove dest files not on source | Per-mapping `Delete files on destination that no longer exist on source` checkbox |
| Per-directory toggle for that delete | Each mapping in the table has its own value for the toggle |
| Linux↔Linux, Windows↔Windows, Linux↔Windows, both directions | Either machine can be Source or Destination for any backup, on any OS |

## Installation

Requires Python 3.10 or newer. Tkinter ships with the standard CPython
installer on Windows and macOS. On Debian/Ubuntu you may need
`sudo apt install python3-tk`. On Fedora it's `sudo dnf install
python3-tkinter`.

```bash
git clone https://github.com/NullAngst/lansync
cd lansync
pip install -e .[tls]
```

The `[tls]` extra installs `cryptography` for TLS. You can omit it and
LANSync will still work in plaintext mode (fine on a trusted LAN, but the
GUI will warn you).

## Building Standalone Executables

If you prefer to run LANSync as a standalone application without requiring Python or a virtual environment on the target machine, you can compile it using PyInstaller. 

**Note on Cross-Compiling:** You cannot cross-compile these binaries. To get a Windows `.exe`, you must run the build process on a Windows machine. To get a Linux binary, you must build on Linux.

First, ensure you have installed the project and PyInstaller:
```bash
pip install -e .[tls]
pip install pyinstaller
```

### Linux
Run the following command from the root of the repository:
```bash
pyinstaller --onefile --noconsole --name lansync lansync/__main__.py
```
Your compiled, ready-to-run Linux binary will be located in the newly created `dist/` directory.

### Windows
1. Install Python from the official website. **Important:** Check the box that says "Add python.exe to PATH" during installation.
2. Open PowerShell in the root of the repository and run:
```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -e .
pip install pyinstaller
pyinstaller --onefile --noconsole --name lansync lansync\__main__.py
```
Your standalone `lansync.exe` will be located in the `dist\` directory.

## Quickstart

### On the destination machine

1. Run `lansync` (or `python -m lansync`).
2. In the header you'll see a long random pairing key. Click **Copy**.
3. Note the machine's LAN IP (`ip a` on Linux, `ipconfig` on Windows).
4. Click **Start** next to "Destination listener". The state goes to
   *Running*. The destination is now waiting for incoming sync sessions
   on TCP port 50515 (configurable).

### On the source machine

1. Run `lansync`.
2. Click **New** in the sidebar to create a backup.
3. Fill in:
   * **Name:** something descriptive — `Movies → Backup PC`.
   * **Role:** `source`.
   * **Peer IP:** the destination's LAN IP.
   * **Peer key:** the key you copied from the destination.
4. Click **Add folder…**, pick `/mnt/media/movies` on the source.
5. In the dialog, set the **Destination folder** to where it should land
   on the other machine — `E:/backups/movies` if the destination is
   Windows, `/mnt/backup/movies` if it's Linux. Choose your delete and
   sanitize options. Save.
6. Click **Save** in the editor, then **Run sync**.

### Reusing the configuration

* Click **Export…** to write the backup to a JSON file.
* On any other machine, click **Import…** and pick that file. All
  settings carry over, including peer IP and key.
* For unattended runs (cron / Task Scheduler):

```bash
lansync run /path/to/movies-backup.json
```

## Filename sanitization

Windows forbids `< > : " / \ | ? *`, control characters, trailing dots
or spaces, and reserved names like `CON`, `NUL`, `LPT1`. Linux and macOS
are happy with most of those, so a Linux source can produce filenames
that fail to land on a Windows destination.

Each mapping picks one of three behaviors:

* **Off** — copy filenames byte-for-byte. Fastest, but may fail on
  Windows.
* **Sanitize copy only** *(default)* — destination receives a sanitized
  name; the source file is untouched. The `Movie: The Best?.mkv` on
  Linux becomes `Movie_ The Best_.mkv` on Windows.
* **Rename source too** — sanitize the source file in place before
  copying, so source and destination stay 1:1. Useful if you want a
  strict mirror you can later sync back the other way.

## Delete extraneous files

Off by default per mapping. When enabled, the sync will:

1. List the destination folder recursively.
2. For each file present on destination but not on source, delete it.
3. For each empty directory whose source equivalent no longer exists,
   remove it.

This is per-mapping so you can mix-and-match: enable it for media
libraries where you want a strict mirror, leave it off for an "incoming"
folder you sweep on the source while the destination keeps everything.

## Transfer tuning

| Setting | Effect |
|---|---|
| Parallel files | Workers that upload files simultaneously. Each opens its own authenticated connection. |
| Speed limit (KB/s) | Total cap shared across all workers, via a token bucket. `0` = unlimited. |
| Chunk (KB) | Size of each socket read/write. Bigger → less syscall overhead but coarser rate-limit granularity. |
| Verify SHA-256 | Source hashes the file before sending; destination re-hashes after writing. Mismatch rejects the upload. |
| Use TLS | Wrap the TCP socket in TLS. Required if the LAN is not fully trusted. |
| Port | Default 50515. Both ends must agree. |

## Security

* The pairing key is the only authentication credential. Keep it off
  shared chat. Regenerate it from the header bar if it leaks; every
  machine that pushes to this one will then need the new key.
* The destination confines all writes under the configured destination
  paths. Path-traversal attempts (`../etc/...`) are rejected before
  reaching disk.
* TLS with a self-signed cert protects the wire. Certificate identity is
  not used for authentication — the HMAC handshake is. This is the
  right tradeoff for ad-hoc LAN sync but it does mean an attacker on the
  same network with the pairing key could connect.

## Command-line reference

```
lansync                          # launch the GUI
lansync gui                      # same
lansync listen [--port 50515]    # run as a destination, no GUI
lansync run path/to/config.json  # run a saved source config
lansync key                      # print this machine's pairing key
```

## Project layout

```
lansync/
  __init__.py
  __main__.py        # python -m lansync entry point
  cli.py             # argparse CLI
  config.py          # BackupConfig / FolderMapping / TransferConfig dataclasses
  gui.py             # Tkinter UI
  keystore.py        # local pairing key persistence
  protocol.py        # wire protocol: framing, HMAC auth
  ratelimit.py       # token-bucket bandwidth limiter
  sanitize.py        # cross-platform filename sanitization
  server.py          # destination-side listener
  sync.py            # source-side engine, diff & upload pool
  tls.py             # self-signed cert generation
tests/
  test_e2e.py        # end-to-end integration test
```

## Running the tests

```bash
python tests/test_e2e.py
```

The tests spin up a destination server on `localhost`, exercise basic
sync, deletion, sanitization, idempotency, authentication failures, and
path-traversal protection.

## License

MIT.
