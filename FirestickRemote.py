import os
import sys
import shutil
import threading
import subprocess
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk


# ---------------- ADB / PATH ----------------

def _base_dir() -> str:
    # Folder where the EXE lives (or this .py when running from source)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bin_dir() -> str:
    # Expect /bin next to the EXE / .py
    return os.path.join(_base_dir(), "bin")


def init_adb_keys() -> None:
    # Use a bundled adb private key so all users appear as the same "computer"
    key_path = os.path.join(_bin_dir(), "firestick_remote_adbkey")
    if os.path.exists(key_path):
        os.environ["ADB_VENDOR_KEYS"] = key_path
        print("Using bundled ADB key:", key_path)
    else:
        print("WARNING: firestick_remote_adbkey not found; using default adb keys.")


def adb_path() -> str:
    # Prefer the bundled adb.exe; fallback to PATH; last resort "adb"
    candidate = os.path.join(_bin_dir(), "adb.exe")
    if os.path.exists(candidate):
        return candidate

    found = shutil.which("adb")
    return found if found else "adb"


def run_adb_command(args):
    """
    Run an adb command and return (success, stdout, stderr).
    On Windows: hides the adb console.
    """
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

        # Debug: intentionally simple/rough (more like real life)
        print("adb >", cmd)
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

    # If any device is unauthorized, treat as not authorized (simple rule)
    for line in out.splitlines():
        if "\tunauthorized" in line:
            return False
        if "\tdevice" in line:
            return True
    return False


# ---------------- MAIN APP ----------------

class FirestickRemote(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.master = master

        # State
        self.ip_var = tk.StringVar(value="")
        self.port_var = tk.StringVar(value="5555")
        self.status_var = tk.StringVar(value="Not connected")

        self.is_connected = False
        self.remote_buttons = []

        # Keep-alive (stay awake)
        self.keep_alive_var = tk.BooleanVar(value=False)
        self._keepalive_stop = threading.Event()
        self._keepalive_thread = None

        self._configure_style()
        self._build_ui()
        self.update_remote_buttons_state()
        self._center_window()

        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI / STYLE ----

    def _configure_style(self):
        style = ttk.Style()

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Colours
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

        style.configure(
            "Title.TLabel",
            background=bg_main,
            foreground=text_main,
            font=("Segoe UI", 12, "bold")
        )
        style.configure(
            "Subtitle.TLabel",
            background=bg_main,
            foreground=text_muted,
            font=("Segoe UI", 8)
        )
        style.configure(
            "Label.TLabel",
            background=bg_card,
            foreground=text_main,
            font=("Segoe UI", 9)
        )

        style.configure(
            "Status.TLabel",
            background=bg_card,
            foreground=danger,
            font=("Segoe UI", 9, "bold")
        )
        style.configure(
            "StatusGood.TLabel",
            background=bg_card,
            foreground="#4ade80",
            font=("Segoe UI", 9, "bold")
        )

        style.configure(
            "TEntry",
            fieldbackground="#020617",
            foreground=text_main,
            bordercolor="#1f2937",
            padding=3
        )

        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 4),
            relief="flat",
            foreground="#0f172a",
            background=accent
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#0ea5e9"), ("disabled", "#1e293b")],
            foreground=[("disabled", "#6b7280")]
        )

        style.configure(
            "Remote.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=6,
            relief="flat",
            foreground=text_main,
            background="#1f2937"
        )
        style.map(
            "Remote.TButton",
            background=[
                ("pressed", "#38bdf8"),
                ("active", "#2563eb"),
                ("disabled", "#111827")
            ],
            foreground=[
                ("pressed", "#0b1120"),
                ("active", "#e5e7eb"),
                ("disabled", "#4b5563")
            ]
        )

    def _build_ui(self):
        self.master.title("Fire Stick ADB Remote")
        self.master.minsize(380, 380)
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)

        main = ttk.Frame(self.master, style="Main.TFrame", padding=16)
        main.grid(row=0, column=0, sticky="nsew")

        # Header
        header = ttk.Frame(main, style="Main.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="Fire Stick ADB Remote", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Control authorized Fire TV devices over ADB",
            style="Subtitle.TLabel"
        ).grid(row=1, column=0, pady=(2, 12), sticky="w")

        # Connection card
        conn_card = ttk.Frame(main, style="Card.TFrame", padding=12)
        conn_card.grid(row=1, column=0, sticky="ew")
        conn_card.columnconfigure(0, weight=1)

        # IP + port + buttons
        conn_row = ttk.Frame(conn_card, style="Card.TFrame")
        conn_row.grid(row=0, column=0, sticky="ew")
        conn_row.columnconfigure(1, weight=1)

        ttk.Label(conn_row, text="IP address", style="Label.TLabel").grid(row=0, column=0, sticky="w")
        ip_entry = ttk.Entry(conn_row, textvariable=self.ip_var, width=18)
        ip_entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        ttk.Label(conn_row, text="Port", style="Label.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 0))
        port_entry = ttk.Entry(conn_row, textvariable=self.port_var, width=8)
        port_entry.grid(row=1, column=1, sticky="w", pady=(2, 0), padx=(10, 0))

        btn_frame = ttk.Frame(conn_row, style="Card.TFrame")
        btn_frame.grid(row=1, column=2, sticky="e", padx=(10, 0))

        ttk.Button(btn_frame, text="Connect", style="Accent.TButton", command=self.connect).grid(
            row=0, column=0, padx=(0, 4)
        )
        ttk.Button(btn_frame, text="Disconnect", style="Accent.TButton", command=self.disconnect).grid(
            row=0, column=1
        )

        # Status
        self.status_label = ttk.Label(conn_card, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        # Keep awake checkbox (stay alive)
        ttk.Checkbutton(
            conn_card,
            text="Keep Fire TV awake",
            variable=self.keep_alive_var,
            command=self._on_toggle_keep_alive
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        # Remote card
        remote_card = ttk.Frame(main, style="Remote.TFrame", padding=16)
        remote_card.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        top_row = ttk.Frame(remote_card, style="Remote.TFrame")
        top_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_row.columnconfigure(0, weight=1)

        ttk.Label(
            top_row,
            text="Virtual Remote",
            foreground="#e5e7eb",
            background="#020617",
            font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            top_row,
            text="Use arrow keys / Enter / Esc as shortcuts",
            foreground="#6b7280",
            background="#020617",
            font=("Segoe UI", 8)
        ).grid(row=1, column=0, sticky="w")

        remote_grid = ttk.Frame(remote_card, style="Remote.TFrame")
        remote_grid.grid(row=1, column=0, pady=(4, 0))
        for c in range(3):
            remote_grid.columnconfigure(c, weight=1)

        def add_btn(label, r, c, keycode, width=8, pady=4):
            b = ttk.Button(
                remote_grid,
                text=label,
                width=width,
                style="Remote.TButton",
                command=lambda: self.send_key(keycode)
            )
            b.grid(row=r, column=c, padx=6, pady=pady)
            self.remote_buttons.append(b)

        # D-pad
        add_btn("▲", 0, 1, 19)
        add_btn("◀", 1, 0, 21)
        add_btn("OK", 1, 1, 66, width=10)
        add_btn("▶", 1, 2, 22)
        add_btn("▼", 2, 1, 20)

        bottom = ttk.Frame(remote_card, style="Remote.TFrame")
        bottom.grid(row=2, column=0, pady=(10, 0))

        def add_bottom(label, keycode, col):
            b = ttk.Button(
                bottom,
                text=label,
                width=10,
                style="Remote.TButton",
                command=lambda: self.send_key(keycode)
            )
            b.grid(row=0, column=col, padx=6)
            self.remote_buttons.append(b)

        add_bottom("Back", 4, 0)
        add_bottom("Home", 3, 1)
        add_bottom("Menu", 82, 2)
        add_bottom("Play / Pause", 85, 3)

        footer = ttk.Frame(main, style="Main.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, text="Written by Craig Douglas Poole QA", style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="e"
        )

        # Keyboard shortcuts
        self.master.bind("<Up>", lambda e: self.send_key(19))
        self.master.bind("<Down>", lambda e: self.send_key(20))
        self.master.bind("<Left>", lambda e: self.send_key(21))
        self.master.bind("<Right>", lambda e: self.send_key(22))
        self.master.bind("<Return>", lambda e: self.send_key(66))
        self.master.bind("<Escape>", lambda e: self.send_key(4))

    def _center_window(self):
        self.master.update_idletasks()
        w = self.master.winfo_width()
        h = self.master.winfo_height()
        sw = self.master.winfo_screenwidth()
        sh = self.master.winfo_screenheight()
        x = int((sw - w) / 2)
        y = int((sh - h) / 2)
        self.master.geometry(f"{w}x{h}+{x}+{y}")

    # ---- State helpers ----

    def update_remote_buttons_state(self):
        if self.is_connected:
            for btn in self.remote_buttons:
                btn.state(["!disabled"])
            self.status_label.configure(style="StatusGood.TLabel")
        else:
            for btn in self.remote_buttons:
                btn.state(["disabled"])
            self.status_label.configure(style="Status.TLabel")

    # ---- Keep-alive ----

    def _on_toggle_keep_alive(self):
        # If not connected, we don't start the thread yet, but we keep the checkbox value.
        if self.keep_alive_var.get():
            if self.is_connected:
                self._start_keep_alive()
        else:
            self._stop_keep_alive()

    def _start_keep_alive(self):
        # Only start one
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return

        self._keepalive_stop.clear()

        def loop():
            # "No-op-ish" keyevent. Low chance of disrupting UI.
            # Interval intentionally not too aggressive.
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

    # ---- Actions ----

    def connect(self):
        ip = self.ip_var.get().strip()
        port = (self.port_var.get().strip() or "5555").strip()

        if not ip:
            messagebox.showerror("Error", "Please enter an IP address.")
            return

        success, out, err = run_adb_command(["connect", f"{ip}:{port}"])

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

                # If user wants it, start keep-alive
                if self.keep_alive_var.get():
                    self._start_keep_alive()
        else:
            self.is_connected = False
            self.status_var.set("Connection failed")
            messagebox.showerror("ADB error", err or out or "Unknown error")

        self.update_remote_buttons_state()

    def disconnect(self):
        # Stop keep-alive first (clean exit)
        self._stop_keep_alive()

        ip = self.ip_var.get().strip()
        port = (self.port_var.get().strip() or "5555").strip()

        if ip:
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
                    lambda: messagebox.showerror(
                        "ADB error", err or out or "Failed to send key event"
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        self._stop_keep_alive()
        self.master.destroy()


# ---------------- ENTRY POINT ----------------

if __name__ == "__main__":
    init_adb_keys()
    root = tk.Tk()
    app = FirestickRemote(root)
    root.mainloop()
