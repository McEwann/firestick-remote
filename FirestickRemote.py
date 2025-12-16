import os
import sys
import shutil
import threading
import subprocess
import time
import re
import json
import zipfile
import tempfile
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

APP_VERSION = "1.2.5"
BIN_REQUIRED_VERSION = "1.0.0"

GITHUB_OWNER = "McEwann"
GITHUB_REPO = "firestick-remote"

DANGEROUS_KEYWORDS = [
    "reboot", "rm ", "wipe", "factory", "uninstall", "format",
    "pm uninstall", "recovery", "bootloader"
]


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bin_dir() -> str:
    return os.path.join(_base_dir(), "bin")


def init_adb_keys() -> None:
    key_path = os.path.join(_bin_dir(), "firestick_remote_adbkey")
    if os.path.exists(key_path):
        os.environ["ADB_VENDOR_KEYS"] = key_path
        print("Using bundled ADB key:", key_path)
    else:
        print("Bundled ADB key not found; using default adb keys.")


def adb_path() -> str:
    candidate = os.path.join(_bin_dir(), "adb.exe")
    if os.path.exists(candidate):
        return candidate
    return shutil.which("adb") or "adb"


def run_adb_command(args):
    cmd = [adb_path()] + args

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            startupinfo=startupinfo,
            creationflags=creationflags
        )

        ok = (completed.returncode == 0)
        out = (completed.stdout or "").strip()
        err = (completed.stderr or "").strip()

        print("adb >", " ".join(cmd))
        if out:
            print("out >", out)
        if err:
            print("err >", err, file=sys.stderr)

        return ok, out, err

    except FileNotFoundError:
        return False, "", (
            "adb executable not found.\n\n"
            "Make sure bin/adb.exe is next to FirestickRemote.exe, "
            "or add adb to your PATH."
        )
    except Exception as e:
        return False, "", str(e)


def device_authorized() -> bool:
    ok, out, _ = run_adb_command(["devices"])
    if not ok:
        return False

    for line in out.splitlines():
        if "\tunauthorized" in line:
            return False
        if "\tdevice" in line:
            return True
    return False


def _bin_version_path() -> str:
    return os.path.join(_bin_dir(), "bin_version.txt")


def read_bin_version() -> str:
    try:
        with open(_bin_version_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _version_tuple(v: str):
    parts = []
    for p in str(v).strip().lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def bin_is_compatible() -> bool:
    found = read_bin_version()
    if not found:
        return False
    return _version_tuple(found) >= _version_tuple(BIN_REQUIRED_VERSION)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "FirestickRemoteUpdater/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def get_latest_release() -> dict:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    return _http_json(url)


def find_asset_download_url(release_json: dict, asset_name: str) -> str | None:
    for a in release_json.get("assets", []):
        if a.get("name") == asset_name:
            return a.get("browser_download_url")
    return None


def download_public_file(url: str, dest_path: str) -> None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "FirestickRemoteUpdater/1.0")
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def _validate_downloaded_exe(path: str) -> None:
    if not os.path.exists(path):
        raise RuntimeError("Downloaded EXE file is missing.")

    size = os.path.getsize(path)
    if size < 1024 * 200:
        raise RuntimeError(f"Downloaded EXE looks too small ({size} bytes).")

    with open(path, "rb") as f:
        sig = f.read(2)
    if sig != b"MZ":
        raise RuntimeError("Downloaded file is not a valid Windows executable (missing MZ header).")


def read_manifest_from_release(release_json: dict) -> dict:
    url = find_asset_download_url(release_json, "manifest.json")
    if not url:
        raise RuntimeError("Release is missing manifest.json asset.")

    tmp = os.path.join(tempfile.gettempdir(), f"firestick_manifest_{int(time.time())}.json")
    download_public_file(url, tmp)

    with open(tmp, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    try:
        os.remove(tmp)
    except Exception:
        pass

    return manifest


def apply_bin_update(zip_path: str, bin_dir: str) -> None:
    os.makedirs(bin_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                continue

            target_path = os.path.join(bin_dir, name)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            with z.open(member, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def schedule_exe_swap(new_exe_path: str, current_exe_path: str) -> None:
    bat = os.path.join(tempfile.gettempdir(), "firestick_update.bat")

    script = f"""@echo off
setlocal
ping 127.0.0.1 -n 3 >nul
move /Y "{current_exe_path}" "{current_exe_path}.bak" >nul
move /Y "{new_exe_path}" "{current_exe_path}" >nul
start "" "{current_exe_path}"
del "%~f0"
"""

    with open(bat, "w", encoding="utf-8") as f:
        f.write(script)

    subprocess.Popen(
        ["cmd.exe", "/c", bat],
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    sys.exit(0)


class FirestickRemote(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.master = master

        self.ip_var = tk.StringVar(value="")
        self.port_var = tk.StringVar(value="5555")
        self.status_var = tk.StringVar(value="Not connected")

        self.is_connected = False
        self.remote_buttons = []

        self.keep_alive_var = tk.BooleanVar(value=False)
        self._keepalive_stop = threading.Event()
        self._keepalive_thread = None

        self.cmd_var = tk.StringVar(value="")
        self.advanced_cmd_var = tk.BooleanVar(value=False)
        self._cmd_history = []
        self._cmd_history_index = 0

        self.cmd_entry = None
        self.cmd_send_btn = None
        self.advanced_cb = None
        self.cmd_output = None

        self._configure_style()
        self._build_ui()
        self.update_remote_buttons_state()
        self._center_window()

        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg_main = "#0b1120"
        bg_card = "#111827"
        bg_remote = "#020617"
        accent = "#38bdf8"
        danger = "#f97373"
        text_main = "#e5e7eb"
        text_muted = "#9ca3af"

        self.master.configure(bg=bg_main)

        style.configure("Main.TFrame", background=bg_main)
        style.configure("Card.TFrame", background=bg_card)
        style.configure("Remote.TFrame", background=bg_remote)

        style.configure("Title.TLabel", background=bg_main, foreground=text_main, font=("Segoe UI", 12, "bold"))
        style.configure("Subtitle.TLabel", background=bg_main, foreground=text_muted, font=("Segoe UI", 8))
        style.configure("Label.TLabel", background=bg_card, foreground=text_main, font=("Segoe UI", 9))

        style.configure("Status.TLabel", background=bg_card, foreground=danger, font=("Segoe UI", 9, "bold"))
        style.configure("StatusGood.TLabel", background=bg_card, foreground="#4ade80", font=("Segoe UI", 9, "bold"))

        style.configure("TEntry", fieldbackground=bg_remote, foreground=text_main, bordercolor="#1f2937", padding=3)

        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 4),
            relief="flat",
            foreground="#0f172a",
            background=accent
        )
        style.map("Accent.TButton",
                  background=[("active", "#0ea5e9"), ("disabled", "#1e293b")],
                  foreground=[("disabled", "#6b7280")])

        style.configure("Remote.TButton", font=("Segoe UI", 9, "bold"), padding=6, relief="flat",
                        foreground=text_main, background="#1f2937")
        style.map("Remote.TButton",
                  background=[("pressed", "#38bdf8"), ("active", "#2563eb"), ("disabled", "#111827")],
                  foreground=[("pressed", "#0b1120"), ("active", "#e5e7eb"), ("disabled", "#4b5563")])

        style.configure("Card.TCheckbutton", background=bg_card, foreground=text_main, font=("Segoe UI", 9))
        style.map("Card.TCheckbutton",
                  foreground=[("disabled", "#4b5563")],
                  background=[("active", bg_card)])

    def _build_ui(self):
        self.master.title("Fire Stick ADB Remote")
        self.master.minsize(420, 440)
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)

        main = ttk.Frame(self.master, style="Main.TFrame", padding=16)
        main.grid(row=0, column=0, sticky="nsew")

        header = ttk.Frame(main, style="Main.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="Fire Stick ADB Remote", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Control authorized Fire TV devices over ADB", style="Subtitle.TLabel").grid(
            row=1, column=0, pady=(2, 12), sticky="w"
        )

        conn_card = ttk.Frame(main, style="Card.TFrame", padding=12)
        conn_card.grid(row=1, column=0, sticky="ew")
        conn_card.columnconfigure(0, weight=1)

        conn_row = ttk.Frame(conn_card, style="Card.TFrame")
        conn_row.grid(row=0, column=0, sticky="ew")
        conn_row.columnconfigure(0, weight=1)

        ttk.Label(conn_row, text="IP address", style="Label.TLabel").grid(row=0, column=0, sticky="w")
        self.ip_entry = ttk.Entry(conn_row, textvariable=self.ip_var, width=18)
        self.ip_entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        ttk.Label(conn_row, text="Port", style="Label.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.port_entry = ttk.Entry(conn_row, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=1, column=1, sticky="w", pady=(2, 0), padx=(10, 0))

        btn_frame = ttk.Frame(conn_row, style="Card.TFrame")
        btn_frame.grid(row=1, column=2, sticky="e", padx=(10, 0))

        self.connect_btn = ttk.Button(btn_frame, text="Connect", style="Accent.TButton", command=self.connect)
        self.connect_btn.grid(row=0, column=0, padx=(0, 4))

        self.disconnect_btn = ttk.Button(btn_frame, text="Disconnect", style="Accent.TButton", command=self.disconnect)
        self.disconnect_btn.grid(row=0, column=1)

        self.status_label = ttk.Label(conn_card, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.keep_alive_cb = ttk.Checkbutton(
            conn_card,
            text="Keep Fire TV awake",
            variable=self.keep_alive_var,
            command=self._on_toggle_keep_alive,
            style="Card.TCheckbutton"
        )
        self.keep_alive_cb.grid(row=2, column=0, sticky="w", pady=(6, 0))

        update_row = ttk.Frame(conn_card, style="Card.TFrame")
        update_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        update_row.columnconfigure(0, weight=1)

        self.version_label = ttk.Label(
            update_row,
            text=f"App v{APP_VERSION} (bin req {BIN_REQUIRED_VERSION}, bin found {read_bin_version() or 'missing'})",
            style="Label.TLabel"
        )
        self.version_label.grid(row=0, column=0, sticky="w")

        self.update_btn = ttk.Button(
            update_row, text="Check for updates", style="Accent.TButton", command=self.check_updates
        )
        self.update_btn.grid(row=0, column=1, sticky="e")

        remote_card = ttk.Frame(main, style="Remote.TFrame", padding=16)
        remote_card.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        top_row = ttk.Frame(remote_card, style="Remote.TFrame")
        top_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_row.columnconfigure(0, weight=1)

        ttk.Label(top_row, text="Virtual Remote", foreground="#e5e7eb", background="#020617",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(top_row, text="Use arrow keys / Enter / Esc as shortcuts", foreground="#6b7280",
                  background="#020617", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w")

        remote_grid = ttk.Frame(remote_card, style="Remote.TFrame")
        remote_grid.grid(row=1, column=0, pady=(4, 0))
        for c in range(3):
            remote_grid.columnconfigure(c, weight=1)

        def add_btn(label, r, c, command, width=8, pady=4):
            b = ttk.Button(remote_grid, text=label, width=width, style="Remote.TButton", command=command)
            b.grid(row=r, column=c, padx=6, pady=pady)
            self.remote_buttons.append(b)

        add_btn("▲", 0, 1, lambda: self.send_key(19))
        add_btn("◀", 1, 0, lambda: self.send_key(21))
        add_btn("OK", 1, 1, self.send_ok, width=10)
        add_btn("▶", 1, 2, lambda: self.send_key(22))
        add_btn("▼", 2, 1, lambda: self.send_key(20))

        bottom = ttk.Frame(remote_card, style="Remote.TFrame")
        bottom.grid(row=2, column=0, pady=(10, 0))

        def add_bottom(label, keycode, col):
            b = ttk.Button(bottom, text=label, width=10, style="Remote.TButton",
                           command=lambda: self.send_key(keycode))
            b.grid(row=0, column=col, padx=6)
            self.remote_buttons.append(b)

        add_bottom("Back", 4, 0)
        add_bottom("Home", 3, 1)
        add_bottom("Menu", 82, 2)
        add_bottom("Play / Pause", 85, 3)

        cmd_card = ttk.Frame(main, style="Card.TFrame", padding=12)
        cmd_card.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        cmd_card.columnconfigure(1, weight=1)

        ttk.Label(cmd_card, text="Manual ADB Command", style="Label.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )

        ttk.Label(cmd_card, text="adb shell", style="Label.TLabel").grid(row=1, column=0, sticky="w")

        self.cmd_entry = ttk.Entry(cmd_card, textvariable=self.cmd_var)
        self.cmd_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6))

        self.cmd_send_btn = ttk.Button(
            cmd_card, text="Send", style="Accent.TButton", command=self.send_manual_command
        )
        self.cmd_send_btn.grid(row=1, column=2)

        self.advanced_cb = ttk.Checkbutton(
            cmd_card,
            text="Run as full adb command (advanced)",
            variable=self.advanced_cmd_var,
            style="Card.TCheckbutton"
        )
        self.advanced_cb.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.cmd_output = tk.Text(
            cmd_card, height=4, wrap="word",
            bg="#020617", fg="#e5e7eb", relief="flat"
        )
        self.cmd_output.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.cmd_output.configure(state="disabled")

        footer = ttk.Frame(main, style="Main.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="Written by Craig Douglas Poole QA", style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="e"
        )

        self.master.bind("<Up>", lambda e: self.send_key(19))
        self.master.bind("<Down>", lambda e: self.send_key(20))
        self.master.bind("<Left>", lambda e: self.send_key(21))
        self.master.bind("<Right>", lambda e: self.send_key(22))
        self.master.bind("<Return>", lambda e: self.send_ok())
        self.master.bind("<Escape>", lambda e: self.send_key(4))

        self.ip_entry.bind("<Return>", lambda e: self.connect())
        self.port_entry.bind("<Return>", lambda e: self.connect())

        self.cmd_entry.bind("<Return>", lambda e: self.send_manual_command())
        self.cmd_entry.bind("<Up>", self._history_up)
        self.cmd_entry.bind("<Down>", self._history_down)

    def _center_window(self):
        self.master.update_idletasks()
        w = self.master.winfo_width()
        h = self.master.winfo_height()
        sw = self.master.winfo_screenwidth()
        sh = self.master.winfo_screenheight()
        x = int((sw - w) / 2)
        y = int((sh - h) / 2)
        self.master.geometry(f"{w}x{h}+{x}+{y}")

    def update_remote_buttons_state(self):
        if self.is_connected:
            for btn in self.remote_buttons:
                btn.state(["!disabled"])
            self.status_label.configure(style="StatusGood.TLabel")
            self.disconnect_btn.state(["!disabled"])
            self.connect_btn.state(["disabled"])
            self.keep_alive_cb.state(["!disabled"])

            if self.cmd_entry is not None:
                self.cmd_entry.state(["!disabled"])
            if self.cmd_send_btn is not None:
                self.cmd_send_btn.state(["!disabled"])
            if self.advanced_cb is not None:
                self.advanced_cb.state(["!disabled"])
        else:
            for btn in self.remote_buttons:
                btn.state(["disabled"])
            self.status_label.configure(style="Status.TLabel")
            self.disconnect_btn.state(["disabled"])
            self.connect_btn.state(["!disabled"])
            self.keep_alive_cb.state(["disabled"])

            if self.cmd_entry is not None:
                self.cmd_entry.state(["disabled"])
            if self.cmd_send_btn is not None:
                self.cmd_send_btn.state(["disabled"])
            if self.advanced_cb is not None:
                self.advanced_cb.state(["disabled"])

    def _is_dangerous(self, cmd: str) -> bool:
        c = (cmd or "").lower()
        return any(k in c for k in DANGEROUS_KEYWORDS)

    def _append_cmd_output(self, text: str):
        if self.cmd_output is None:
            return
        self.cmd_output.configure(state="normal")
        self.cmd_output.insert("end", text + "\n")
        self.cmd_output.see("end")
        self.cmd_output.configure(state="disabled")

    def _push_history(self, cmd: str):
        cmd = (cmd or "").strip()
        if not cmd:
            return
        if self._cmd_history and self._cmd_history[-1] == cmd:
            self._cmd_history_index = len(self._cmd_history)
            return
        self._cmd_history.append(cmd)
        if len(self._cmd_history) > 30:
            self._cmd_history = self._cmd_history[-30:]
        self._cmd_history_index = len(self._cmd_history)

    def _history_up(self, event=None):
        if not self._cmd_history:
            return "break"
        if self._cmd_history_index > 0:
            self._cmd_history_index -= 1
        self.cmd_var.set(self._cmd_history[self._cmd_history_index])
        if self.cmd_entry is not None:
            self.cmd_entry.icursor("end")
        return "break"

    def _history_down(self, event=None):
        if not self._cmd_history:
            return "break"
        if self._cmd_history_index < len(self._cmd_history) - 1:
            self._cmd_history_index += 1
            self.cmd_var.set(self._cmd_history[self._cmd_history_index])
        else:
            self._cmd_history_index = len(self._cmd_history)
            self.cmd_var.set("")
        if self.cmd_entry is not None:
            self.cmd_entry.icursor("end")
        return "break"

    def send_manual_command(self):
        if not self.is_connected:
            messagebox.showwarning("Not connected", "Please connect to a Fire TV first.")
            return

        cmd = self.cmd_var.get().strip()
        if not cmd:
            return

        self._push_history(cmd)

        if self.advanced_cmd_var.get():
            args = cmd.split()
            shown = f"adb {cmd}"
        else:
            args = ["shell"] + cmd.split()
            shown = f"adb shell {cmd}"

        if self._is_dangerous(cmd):
            ok = messagebox.askyesno(
                "Confirm command",
                "This command may be dangerous.\n\n"
                f"{shown}\n\n"
                "Are you sure?"
            )
            if not ok:
                return

        self._append_cmd_output(f"$ {shown}")

        def worker():
            ok, out, err = run_adb_command(args)
            result = out if out else err if err else "OK"
            prefix = "✓" if ok else "✗"
            self.master.after(0, lambda: self._append_cmd_output(f"{prefix} {result}\n"))

        threading.Thread(target=worker, daemon=True).start()

    def check_updates(self):
        def worker():
            if not GITHUB_OWNER or not GITHUB_REPO:
                self.master.after(0, lambda: messagebox.showerror(
                    "Updater not configured",
                    "Set GITHUB_OWNER and GITHUB_REPO at the top of the script."
                ))
                return

            self.master.after(0, lambda: self.update_btn.state(["disabled"]))

            try:
                release = get_latest_release()
                manifest = read_manifest_from_release(release)
            except urllib.error.HTTPError as e:
                self.master.after(0, lambda: messagebox.showerror(
                    "Update error",
                    f"Failed to check updates.\n\nHTTP {e.code}: {e.reason}"
                ))
                self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                return
            except Exception as e:
                self.master.after(0, lambda: messagebox.showerror("Update error", str(e)))
                self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                return

            latest_app = str(manifest.get("app_version", "")).strip().lstrip("v")
            latest_bin_req = str(manifest.get("bin_required_version", "")).strip()
            exe_name = manifest.get("exe_asset", "FirestickRemote.exe")
            bin_name = manifest.get("bin_asset", "bin_update.zip")

            if not latest_app:
                self.master.after(0, lambda: messagebox.showerror("Update error", "manifest.json missing app_version."))
                self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                return

            if _version_tuple(latest_app) <= _version_tuple(APP_VERSION):
                self.master.after(0, lambda: messagebox.showinfo(
                    "Updates",
                    f"You're up to date.\n\nCurrent: {APP_VERSION}\nLatest: {latest_app}"
                ))
                self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                return

            exe_url = find_asset_download_url(release, exe_name)
            if not exe_url:
                self.master.after(0, lambda: messagebox.showerror(
                    "Update error",
                    f"Release is missing required asset: {exe_name}"
                ))
                self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                return

            def confirm_and_continue():
                yes = messagebox.askyesno(
                    "Update available",
                    f"Update found!\n\n"
                    f"Current: {APP_VERSION}\n"
                    f"Latest: {latest_app}\n\n"
                    f"Install update now?"
                )
                if not yes:
                    self.update_btn.state(["!disabled"])
                    return
                threading.Thread(target=do_update, daemon=True).start()

            def do_update():
                tmp_exe = os.path.join(tempfile.gettempdir(), f"FirestickRemote_{latest_app}.new.exe")
                try:
                    download_public_file(exe_url, tmp_exe)
                    _validate_downloaded_exe(tmp_exe)
                except Exception as e:
                    self.master.after(0, lambda: messagebox.showerror(
                        "Update error",
                        f"Failed to download/validate EXE.\n\n{e}"
                    ))
                    self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                    return

                found_bin = read_bin_version()
                if latest_bin_req and (_version_tuple(found_bin or "0.0.0") < _version_tuple(latest_bin_req)):
                    bin_url = find_asset_download_url(release, bin_name)
                    if not bin_url:
                        self.master.after(0, lambda: messagebox.showerror(
                            "Bin update required",
                            f"This update requires bin version {latest_bin_req}, but the release has no {bin_name} asset."
                        ))
                        self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                        return

                    tmp_zip = os.path.join(tempfile.gettempdir(), f"FirestickRemote_bin_{latest_bin_req}.zip")
                    try:
                        download_public_file(bin_url, tmp_zip)
                        apply_bin_update(tmp_zip, _bin_dir())
                    except Exception as e:
                        self.master.after(0, lambda: messagebox.showerror(
                            "Bin update failed",
                            f"Failed to apply bin update.\n\n{e}"
                        ))
                        self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                        return
                    finally:
                        try:
                            os.remove(tmp_zip)
                        except Exception:
                            pass

                if not getattr(sys, "frozen", False):
                    self.master.after(0, lambda: messagebox.showinfo(
                        "Update downloaded",
                        "Update downloaded.\n\nYou're running from source (not EXE), so auto-swap is disabled."
                    ))
                    self.master.after(0, lambda: self.update_btn.state(["!disabled"]))
                    return

                current_exe = sys.executable
                schedule_exe_swap(tmp_exe, current_exe)
                self.master.after(0, self.master.destroy)

            self.master.after(0, confirm_and_continue)

        threading.Thread(target=worker, daemon=True).start()

    def send_ok(self):
        if not self.is_connected:
            messagebox.showwarning("Not connected", "Please connect to a Fire TV first.")
            return

        def worker():
            run_adb_command(["shell", "input", "keyevent", "23"])
            time.sleep(0.05)
            run_adb_command(["shell", "input", "keyevent", "66"])

        threading.Thread(target=worker, daemon=True).start()

    def _on_toggle_keep_alive(self):
        if self.keep_alive_var.get():
            if self.is_connected:
                self._start_keep_alive()
        else:
            self._stop_keep_alive()

    def _start_keep_alive(self):
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return

        self._keepalive_stop.clear()

        def loop():
            while not self._keepalive_stop.wait(45):
                if not self.is_connected:
                    break
                ok, _, _ = run_adb_command(["shell", "input", "keyevent", "0"])
                if not ok:
                    break

        self._keepalive_thread = threading.Thread(target=loop, daemon=True)
        self._keepalive_thread.start()

    def _stop_keep_alive(self):
        self._keepalive_stop.set()

    def _valid_ip(self, ip: str) -> bool:
        m = re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip)
        if not m:
            return False
        parts = ip.split(".")
        return all(0 <= int(p) <= 255 for p in parts)

    def _valid_port(self, port: str) -> bool:
        if not port.isdigit():
            return False
        p = int(port)
        return 1 <= p <= 65535

    def connect(self):
        if not bin_is_compatible():
            messagebox.showerror(
                "Bin update required",
                "Your FirestickRemote bin folder is missing/out of date.\n\n"
                f"Required bin version: {BIN_REQUIRED_VERSION}\n"
                f"Found bin version: {read_bin_version() or 'missing'}\n\n"
                "Use 'Check for updates' (or install the bin update package)."
            )
            return

        ip = self.ip_var.get().strip()
        port = (self.port_var.get().strip() or "5555").strip()

        if not ip:
            messagebox.showerror("Error", "Please enter an IP address.")
            return
        if not self._valid_ip(ip):
            messagebox.showerror("Error", "Please enter a valid IPv4 address (e.g. 192.168.1.50).")
            return
        if not self._valid_port(port):
            messagebox.showerror("Error", "Please enter a valid port (1–65535).")
            return

        self.connect_btn.state(["disabled"])
        self.disconnect_btn.state(["disabled"])
        self.status_var.set(f"Connecting to {ip}:{port}...")

        def worker():
            success, out, err = run_adb_command(["connect", f"{ip}:{port}"])

            def finish_ui():
                if success:
                    if not device_authorized():
                        msg = (
                            "Connected, but the Fire TV has not yet authorized this tool.\n\n"
                            "On your Fire TV, you should see a popup saying:\n"
                            "  'Allow USB debugging?'\n\n"
                            "Tick 'Always allow from this computer' and press OK,\n"
                            "then press Connect again."
                        )
                        messagebox.showinfo("Authorize on Fire TV", msg)
                        self.is_connected = False
                        self.status_var.set("Not connected")
                    else:
                        self.is_connected = True
                        self.status_var.set(f"Connected to {ip}:{port}")
                        if self.keep_alive_var.get():
                            self._start_keep_alive()
                else:
                    self.is_connected = False
                    self.status_var.set("Connection failed")
                    messagebox.showerror("ADB error", err or out or "Unknown error :(")

                self.version_label.configure(
                    text=f"App v{APP_VERSION} (bin req {BIN_REQUIRED_VERSION}, bin found {read_bin_version() or 'missing'})"
                )
                self.update_remote_buttons_state()

            self.master.after(0, finish_ui)

        threading.Thread(target=worker, daemon=True).start()

    def disconnect(self):
        self._stop_keep_alive()

        ip = self.ip_var.get().strip()
        port = (self.port_var.get().strip() or "5555").strip()

        if ip and self._valid_port(port):
            run_adb_command(["disconnect", f"{ip}:{port}"])
        else:
            run_adb_command(["disconnect"])

        self.is_connected = False
        self.status_var.set("Disconnected")
        self.update_remote_buttons_state()

    def send_key(self, keycode: int):
        if not self.is_connected:
            messagebox.showwarning("Not connected", "Please connect to a Fire TV first.")
            return

        def worker():
            ok, out, err = run_adb_command(["shell", "input", "keyevent", str(keycode)])
            if not ok:
                self.master.after(
                    0,
                    lambda: messagebox.showerror("ADB error", err or out or "Failed to send key event"),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        self._stop_keep_alive()
        self.master.destroy()


if __name__ == "__main__":
    init_adb_keys()

    root = tk.Tk()

    icon_path = os.path.join(_bin_dir(), "firestick.ico")
    if os.path.exists(icon_path):
        try:
            root.iconbitmap(icon_path)
        except Exception:
            pass

    app = FirestickRemote(root)
    root.mainloop()
