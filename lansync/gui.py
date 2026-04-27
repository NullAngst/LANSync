"""Tkinter GUI for LANSync.

Layout:
  - Header: shows this machine's local pairing key + a Copy button + a
    Regenerate button. The destination listener lives behind a Start/Stop
    toggle so a machine can act as either Source, Destination, or both.
  - Left pane: list of saved Backup Configs (the "named backups" the user
    wanted), with New / Duplicate / Delete / Import / Export buttons.
  - Right pane (per selected config): peer IP + peer key fields, role
    (source/destination), folder mappings table, transfer settings, and a
    Run button (only enabled in source role).
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from .config import BackupConfig, FolderMapping, TransferConfig, default_config_dir
from .keystore import get_local_key, regenerate_local_key
from .server import DestinationServer
from .sync import SyncEngine, SyncProgress
from .tls import make_server_ssl_context
from .protocol import detect_os, DEFAULT_PORT


CONFIGS_DIR_NAME = "configs"


def configs_dir() -> Path:
    p = default_config_dir() / CONFIGS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


class LanSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LANSync")
        self.geometry("980x680")
        self.minsize(820, 560)

        self.local_key = get_local_key()
        self.server: Optional[DestinationServer] = None
        self.configs: Dict[str, BackupConfig] = {}
        self.current_path: Optional[Path] = None
        self.sync_thread: Optional[threading.Thread] = None
        self.cancel_event: Optional[threading.Event] = None

        self._build_ui()
        self._reload_configs()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._build_header()

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        # Left: backup list
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Backups", font=("", 11, "bold")).pack(anchor="w")
        self.config_list = tk.Listbox(left, width=28, height=22, exportselection=False)
        self.config_list.pack(fill="y", expand=True, pady=(2, 4))
        self.config_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_config())

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="New", command=self._new_config).pack(side="left")
        ttk.Button(btns, text="Duplicate", command=self._duplicate_config).pack(side="left")
        ttk.Button(btns, text="Delete", command=self._delete_config).pack(side="left")
        btns2 = ttk.Frame(left)
        btns2.pack(fill="x", pady=(4, 0))
        ttk.Button(btns2, text="Import…", command=self._import_config).pack(side="left")
        ttk.Button(btns2, text="Export…", command=self._export_config).pack(side="left")

        # Right: editor
        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self._build_editor(right)

        self._build_status_bar()

    def _build_header(self):
        f = ttk.LabelFrame(self, text="This machine", padding=8)
        f.pack(fill="x", padx=8, pady=(8, 0))
        row1 = ttk.Frame(f); row1.pack(fill="x")
        ttk.Label(row1, text="Pairing key:").pack(side="left")
        self.key_var = tk.StringVar(value=self.local_key)
        e = ttk.Entry(row1, textvariable=self.key_var, width=60)
        e.pack(side="left", padx=6)
        e.configure(state="readonly")
        ttk.Button(row1, text="Copy", command=self._copy_key).pack(side="left")
        ttk.Button(row1, text="Regenerate", command=self._regen_key).pack(side="left")
        ttk.Label(row1, text="   OS:").pack(side="left")
        ttk.Label(row1, text=detect_os()).pack(side="left")

        row2 = ttk.Frame(f); row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Destination listener:").pack(side="left")
        self.listener_state = tk.StringVar(value="Stopped")
        ttk.Label(row2, textvariable=self.listener_state, width=10).pack(side="left", padx=4)
        ttk.Label(row2, text="Port:").pack(side="left")
        self.listen_port_var = tk.IntVar(value=DEFAULT_PORT)
        ttk.Spinbox(row2, from_=1, to=65535, textvariable=self.listen_port_var,
                    width=8).pack(side="left", padx=4)
        self.listen_btn = ttk.Button(row2, text="Start", command=self._toggle_listener)
        self.listen_btn.pack(side="left")
        ttk.Label(row2, text="   (turn this on for the OTHER machine to push to this one)",
                  foreground="#666").pack(side="left", padx=8)

    def _build_editor(self, parent: ttk.Frame):
        # --- backup-name + role + peer ---
        top = ttk.LabelFrame(parent, text="Backup", padding=8)
        top.pack(fill="x")

        r1 = ttk.Frame(top); r1.pack(fill="x")
        ttk.Label(r1, text="Name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.name_var, width=40).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(r1, text="Role:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.role_var = tk.StringVar(value="source")
        ttk.Combobox(r1, textvariable=self.role_var, values=["source", "destination"],
                     state="readonly", width=12).grid(row=0, column=3, sticky="w", padx=4)

        r2 = ttk.Frame(top); r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Peer IP:").grid(row=0, column=0, sticky="w")
        self.peer_ip_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.peer_ip_var, width=22).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(r2, text="Peer key:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.peer_key_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.peer_key_var, width=48).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(r2, text="Peer OS:").grid(row=0, column=4, sticky="w", padx=(12, 0))
        self.peer_os_var = tk.StringVar(value="auto")
        ttk.Combobox(r2, textvariable=self.peer_os_var,
                     values=["auto", "linux", "windows", "macos"],
                     state="readonly", width=10).grid(row=0, column=5, sticky="w", padx=4)

        # --- mappings ---
        mid = ttk.LabelFrame(parent, text="Folder mappings", padding=8)
        mid.pack(fill="both", expand=True, pady=(8, 0))

        cols = ("source", "dest", "delete", "sanitize", "enabled")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        self.tree.heading("source", text="Source path")
        self.tree.heading("dest", text="Destination path")
        self.tree.heading("delete", text="Delete extras")
        self.tree.heading("sanitize", text="Sanitize")
        self.tree.heading("enabled", text="Enabled")
        self.tree.column("source", width=260)
        self.tree.column("dest", width=260)
        self.tree.column("delete", width=90, anchor="center")
        self.tree.column("sanitize", width=80, anchor="center")
        self.tree.column("enabled", width=70, anchor="center")
        self.tree.pack(fill="both", expand=True)

        b = ttk.Frame(mid); b.pack(fill="x", pady=(6, 0))
        ttk.Button(b, text="Add folder…", command=self._add_mapping).pack(side="left")
        ttk.Button(b, text="Edit…", command=self._edit_mapping).pack(side="left")
        ttk.Button(b, text="Remove", command=self._remove_mapping).pack(side="left")

        # --- transfer settings ---
        tf = ttk.LabelFrame(parent, text="Transfer settings", padding=8)
        tf.pack(fill="x", pady=(8, 0))
        self.concurrency_var = tk.IntVar(value=4)
        self.speed_var = tk.IntVar(value=0)
        self.chunk_var = tk.IntVar(value=1024)
        self.verify_var = tk.BooleanVar(value=True)
        self.tls_var = tk.BooleanVar(value=True)
        self.port_var = tk.IntVar(value=0)

        for col, label, var, w in [
            (0, "Parallel files:", self.concurrency_var, 6),
            (2, "Speed limit (KB/s, 0=unlimited):", self.speed_var, 8),
            (4, "Chunk (KB):", self.chunk_var, 8),
        ]:
            ttk.Label(tf, text=label).grid(row=0, column=col, sticky="w")
            ttk.Spinbox(tf, from_=0, to=1_000_000, textvariable=var, width=w).grid(
                row=0, column=col + 1, sticky="w", padx=4)

        ttk.Checkbutton(tf, text="Verify SHA-256 after each file",
                        variable=self.verify_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(tf, text="Use TLS",
                        variable=self.tls_var).grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(tf, text="Port (0=default):").grid(row=1, column=4, sticky="w", pady=(4, 0))
        ttk.Spinbox(tf, from_=0, to=65535, textvariable=self.port_var, width=8).grid(
            row=1, column=5, sticky="w", padx=4, pady=(4, 0))

        # --- run + save ---
        bot = ttk.Frame(parent); bot.pack(fill="x", pady=(8, 0))
        ttk.Button(bot, text="Save", command=self._save_current).pack(side="left")
        self.run_btn = ttk.Button(bot, text="Run sync", command=self._run_sync)
        self.run_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(bot, text="Cancel", command=self._cancel_sync, state="disabled")
        self.cancel_btn.pack(side="left", padx=(4, 0))

    def _build_status_bar(self):
        s = ttk.Frame(self)
        s.pack(fill="x", side="bottom")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(s, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=8, pady=4)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(s, textvariable=self.status_var, anchor="w").pack(side="left", padx=8)

    # ------------------------------------------------------------------
    # Header actions
    # ------------------------------------------------------------------

    def _copy_key(self):
        self.clipboard_clear()
        self.clipboard_append(self.local_key)
        self.status_var.set("Pairing key copied to clipboard.")

    def _regen_key(self):
        if not messagebox.askyesno(
            "Regenerate key",
            "This invalidates the key on every other machine that pushes to this one. Continue?"
        ):
            return
        self.local_key = regenerate_local_key()
        self.key_var.set(self.local_key)
        if self.server is not None:
            # Restart the listener with the new key.
            self._toggle_listener()
            self._toggle_listener()

    def _toggle_listener(self):
        if self.server is None:
            ssl_ctx = make_server_ssl_context()
            if ssl_ctx is None:
                if not messagebox.askyesno(
                    "TLS unavailable",
                    "The 'cryptography' package is not installed. Start the listener "
                    "in plaintext mode?"
                ):
                    return
            try:
                self.server = DestinationServer(
                    key=self.local_key,
                    port=int(self.listen_port_var.get()),
                    ssl_context=ssl_ctx,
                    log=lambda m: self.after(0, self.status_var.set, m),
                )
                self.server.start()
                self.listener_state.set("Running")
                self.listen_btn.configure(text="Stop")
            except Exception as e:
                self.server = None
                messagebox.showerror("Listener", f"Could not start: {e}")
        else:
            self.server.stop()
            self.server = None
            self.listener_state.set("Stopped")
            self.listen_btn.configure(text="Start")

    # ------------------------------------------------------------------
    # Config list
    # ------------------------------------------------------------------

    def _reload_configs(self):
        self.config_list.delete(0, "end")
        self.configs.clear()
        for p in sorted(configs_dir().glob("*.json")):
            try:
                cfg = BackupConfig.load(p)
            except Exception:
                continue
            self.configs[str(p)] = cfg
            self.config_list.insert("end", cfg.name)

    def _on_select_config(self):
        sel = self.config_list.curselection()
        if not sel:
            return
        path = list(self.configs.keys())[sel[0]]
        self.current_path = Path(path)
        self._load_into_form(self.configs[path])

    def _load_into_form(self, cfg: BackupConfig):
        self.name_var.set(cfg.name)
        self.role_var.set(cfg.role)
        self.peer_ip_var.set(cfg.peer_ip)
        self.peer_key_var.set(cfg.peer_key)
        self.peer_os_var.set(cfg.peer_os)
        self.concurrency_var.set(cfg.transfer.concurrency)
        self.speed_var.set(cfg.transfer.speed_limit_kbps)
        self.chunk_var.set(max(1, cfg.transfer.chunk_size // 1024))
        self.verify_var.set(cfg.transfer.verify_hash)
        self.tls_var.set(cfg.transfer.use_tls)
        self.port_var.set(cfg.transfer.port)
        self.tree.delete(*self.tree.get_children())
        for m in cfg.mappings:
            self.tree.insert("", "end", values=(
                m.source_path, m.dest_path,
                "yes" if m.delete_extraneous else "no",
                m.sanitize_mode,
                "yes" if m.enabled else "no",
            ))

    def _form_to_config(self) -> BackupConfig:
        mappings: List[FolderMapping] = []
        for iid in self.tree.get_children():
            v = self.tree.item(iid)["values"]
            mappings.append(FolderMapping(
                source_path=str(v[0]),
                dest_path=str(v[1]),
                delete_extraneous=(str(v[2]) == "yes"),
                sanitize_mode=str(v[3]),
                enabled=(str(v[4]) == "yes"),
            ))
        return BackupConfig(
            name=self.name_var.get().strip() or "Untitled Backup",
            role=self.role_var.get(),
            peer_ip=self.peer_ip_var.get().strip(),
            peer_key=self.peer_key_var.get().strip(),
            peer_os=self.peer_os_var.get(),
            mappings=mappings,
            transfer=TransferConfig(
                concurrency=int(self.concurrency_var.get()),
                speed_limit_kbps=int(self.speed_var.get()),
                chunk_size=max(1, int(self.chunk_var.get())) * 1024,
                verify_hash=bool(self.verify_var.get()),
                port=int(self.port_var.get()),
                use_tls=bool(self.tls_var.get()),
            ),
        )

    def _new_config(self):
        cfg = BackupConfig(name="New Backup")
        path = configs_dir() / self._safe_filename(cfg.name)
        cfg.save(path)
        self._reload_configs()
        self._select_by_path(path)

    def _duplicate_config(self):
        if not self.current_path:
            return
        cfg = self._form_to_config()
        cfg.name = cfg.name + " (copy)"
        path = configs_dir() / self._safe_filename(cfg.name)
        cfg.save(path)
        self._reload_configs()
        self._select_by_path(path)

    def _delete_config(self):
        if not self.current_path:
            return
        if not messagebox.askyesno("Delete backup",
                                   f"Delete '{self.current_path.stem}'?"):
            return
        try:
            self.current_path.unlink()
        except OSError as e:
            messagebox.showerror("Delete", str(e))
            return
        self.current_path = None
        self._reload_configs()

    def _import_config(self):
        path = filedialog.askopenfilename(filetypes=[("LANSync config", "*.json"),
                                                     ("All files", "*.*")])
        if not path:
            return
        try:
            cfg = BackupConfig.load(path)
        except Exception as e:
            messagebox.showerror("Import", f"Invalid config: {e}")
            return
        target = configs_dir() / self._safe_filename(cfg.name)
        cfg.save(target)
        self._reload_configs()
        self._select_by_path(target)

    def _export_config(self):
        if not self.current_path:
            return
        cfg = self._form_to_config()
        path = filedialog.asksaveasfilename(
            initialfile=self._safe_filename(cfg.name),
            defaultextension=".json",
            filetypes=[("LANSync config", "*.json")],
        )
        if not path:
            return
        cfg.save(path)
        self.status_var.set(f"Exported to {path}")

    def _save_current(self):
        cfg = self._form_to_config()
        if not self.current_path:
            self.current_path = configs_dir() / self._safe_filename(cfg.name)
        cfg.save(self.current_path)
        self._reload_configs()
        self._select_by_path(self.current_path)
        self.status_var.set(f"Saved '{cfg.name}'.")

    @staticmethod
    def _safe_filename(name: str) -> str:
        base = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
        return (base or "backup") + ".json"

    def _select_by_path(self, path: Path):
        keys = list(self.configs.keys())
        target = str(path)
        if target in keys:
            i = keys.index(target)
            self.config_list.selection_clear(0, "end")
            self.config_list.selection_set(i)
            self.config_list.see(i)
            self.current_path = path
            self._load_into_form(self.configs[target])

    # ------------------------------------------------------------------
    # Mapping editor
    # ------------------------------------------------------------------

    def _add_mapping(self):
        src = filedialog.askdirectory(title="Pick source folder")
        if not src:
            return
        MappingDialog(self, FolderMapping(source_path=src, dest_path=""),
                      on_save=self._mapping_saved)

    def _edit_mapping(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        v = self.tree.item(iid)["values"]
        m = FolderMapping(
            source_path=str(v[0]), dest_path=str(v[1]),
            delete_extraneous=(str(v[2]) == "yes"),
            sanitize_mode=str(v[3]),
            enabled=(str(v[4]) == "yes"),
        )
        MappingDialog(self, m,
                      on_save=lambda nm: self._mapping_saved(nm, replace_iid=iid))

    def _remove_mapping(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)

    def _mapping_saved(self, m: FolderMapping, replace_iid: Optional[str] = None):
        values = (m.source_path, m.dest_path,
                  "yes" if m.delete_extraneous else "no",
                  m.sanitize_mode,
                  "yes" if m.enabled else "no")
        if replace_iid:
            self.tree.item(replace_iid, values=values)
        else:
            self.tree.insert("", "end", values=values)

    # ------------------------------------------------------------------
    # Run sync
    # ------------------------------------------------------------------

    def _run_sync(self):
        cfg = self._form_to_config()
        if cfg.role != "source":
            messagebox.showinfo("Run", "Set role to 'source' to push files. The destination machine should have its listener running.")
            return
        if not cfg.peer_ip or not cfg.peer_key:
            messagebox.showerror("Run", "Peer IP and peer key are required.")
            return
        if not cfg.mappings:
            messagebox.showerror("Run", "Add at least one folder mapping.")
            return

        self.cancel_event = threading.Event()
        engine = SyncEngine(
            cfg,
            log=lambda m: self.after(0, self.status_var.set, m),
            progress=lambda p: self.after(0, self._update_progress, p),
            cancel_event=self.cancel_event,
        )

        def runner():
            try:
                engine.run()
                self.after(0, self.status_var.set, "Sync complete.")
            except Exception as e:
                self.after(0, messagebox.showerror, "Sync error", str(e))
            finally:
                self.after(0, self._sync_finished)

        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.sync_thread = threading.Thread(target=runner, daemon=True)
        self.sync_thread.start()

    def _cancel_sync(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.status_var.set("Cancelling…")

    def _sync_finished(self):
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    def _update_progress(self, p: SyncProgress):
        if p.bytes_total > 0:
            self.progress_var.set(100 * p.bytes_done / p.bytes_total)
        elif p.file_total > 0:
            self.progress_var.set(100 * p.file_index / p.file_total)
        msg = (f"[{p.mapping_index}/{p.mapping_total}] "
               f"{p.transferred} sent · {p.skipped} skipped · "
               f"{p.deleted} deleted · {p.failed} failed — {p.message}")
        self.status_var.set(msg)


class MappingDialog(tk.Toplevel):
    """Dialog for configuring one folder mapping (per-mapping options)."""

    def __init__(self, master, mapping: FolderMapping, on_save):
        super().__init__(master)
        self.title("Folder mapping")
        self.transient(master)
        self.grab_set()
        self.on_save = on_save
        self.mapping = mapping
        self.resizable(False, False)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Source folder (this machine):").grid(row=0, column=0, sticky="w")
        self.src_var = tk.StringVar(value=mapping.source_path)
        ttk.Entry(f, textvariable=self.src_var, width=60).grid(row=1, column=0, sticky="we")
        ttk.Button(f, text="Browse…", command=self._browse_src).grid(row=1, column=1, padx=4)

        ttk.Label(f, text="Destination folder (on peer machine, absolute path):").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.dst_var = tk.StringVar(value=mapping.dest_path)
        ttk.Entry(f, textvariable=self.dst_var, width=60).grid(row=3, column=0, sticky="we")

        ttk.Label(f, text="(e.g. E:/backups/movies on Windows, /mnt/backup/movies on Linux)",
                  foreground="#666").grid(row=4, column=0, sticky="w", pady=(0, 8))

        self.del_var = tk.BooleanVar(value=mapping.delete_extraneous)
        ttk.Checkbutton(
            f, variable=self.del_var,
            text="Delete files on destination that no longer exist on source"
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))

        self.enabled_var = tk.BooleanVar(value=mapping.enabled)
        ttk.Checkbutton(f, variable=self.enabled_var, text="Enabled").grid(
            row=6, column=0, sticky="w")

        ttk.Label(f, text="Filename sanitization (for Windows-incompatible names):").grid(
            row=7, column=0, sticky="w", pady=(8, 0))
        self.sanitize_var = tk.StringVar(value=mapping.sanitize_mode)
        sf = ttk.Frame(f); sf.grid(row=8, column=0, sticky="w")
        ttk.Radiobutton(sf, text="Off (copy names verbatim)",
                        variable=self.sanitize_var, value="off").pack(anchor="w")
        ttk.Radiobutton(sf, text="Sanitize copy only (source untouched)",
                        variable=self.sanitize_var, value="copy").pack(anchor="w")
        ttk.Radiobutton(sf, text="Rename source too (keep mirror 1:1)",
                        variable=self.sanitize_var, value="rename").pack(anchor="w")

        b = ttk.Frame(f); b.grid(row=9, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=self._save).pack(side="right", padx=(0, 6))

    def _browse_src(self):
        d = filedialog.askdirectory(parent=self, title="Pick source folder")
        if d:
            self.src_var.set(d)

    def _save(self):
        if not self.src_var.get() or not self.dst_var.get():
            messagebox.showerror("Mapping", "Source and destination paths are required.",
                                 parent=self)
            return
        m = FolderMapping(
            source_path=self.src_var.get(),
            dest_path=self.dst_var.get(),
            delete_extraneous=bool(self.del_var.get()),
            sanitize_mode=self.sanitize_var.get(),
            enabled=bool(self.enabled_var.get()),
        )
        self.on_save(m)
        self.destroy()


def main():
    app = LanSyncApp()
    app.mainloop()


if __name__ == "__main__":
    main()
