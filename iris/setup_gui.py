"""Iris setup window: check the install, pick options, connect AI clients.

Run with `setup.bat`, `python -m iris.setup_gui`, or `iris-mcp-setup`.
Stdlib only (tkinter), so it opens on a fresh clone before `pip install`.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from iris import __version__
from iris import configure as cfg

ANSI = re.compile(r"\x1b\[[0-9;]*m")
GOOD, WARN, BAD, IDLE = "#1a7f37", "#9a6700", "#cf222e", "#6e7781"


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


class SetupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.busy = 0
        s = cfg.load_settings()
        if not s.python:
            s.python = cfg.default_python()

        self.v_python = tk.StringVar(value=s.python)
        self.v_tess = tk.StringVar(value=s.tesseract)
        self.v_logdir = tk.StringVar(value=s.log_dir)
        self.v_apps = tk.StringVar(value=s.apps_file)
        self.v_telemetry = tk.BooleanVar(value=s.telemetry)
        self.v_uia_walk = tk.BooleanVar(value=s.uia_walk)
        self.v_psm = tk.StringVar(value=str(s.ocr_psm))
        self.v_psm2 = tk.StringVar(value=str(s.ocr_pass2_psm))
        self.v_guide = tk.BooleanVar(value=False)
        self.v_clients = {c.key: tk.BooleanVar(value=False) for c in cfg.CLIENTS}
        self.client_status: dict[str, ttk.Label] = {}

        root.title("Iris setup")
        root.minsize(720, 640)
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._pump)
        self.refresh(first=True)

    # ------------------------------------------------------------------ UI
    def _style(self) -> None:
        st = ttk.Style(self.root)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        base = ("Segoe UI", 10)
        st.configure(".", font=base)
        st.configure("Title.TLabel", font=("Segoe UI Semibold", 17))
        st.configure("Sub.TLabel", foreground=IDLE)
        st.configure("Step.TLabelframe.Label", font=("Segoe UI Semibold", 11))
        st.configure("Accent.TButton", font=("Segoe UI Semibold", 10))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(16, 12, 16, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="Iris setup", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{__version__}", style="Sub.TLabel").pack(
            side="left", padx=8, pady=(6, 0)
        )
        ttk.Label(
            outer,
            text="Windows desktop control for Claude and Codex. Work top to bottom.",
            style="Sub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))

        probe = ttk.Button(outer, text="Install dependencies", width=20)
        self.extra_w = probe.winfo_reqwidth() + 6
        probe.destroy()

        row = 2
        # 1. Python
        f = self._step(outer, row, "1  Python")
        self._path_row(
            f,
            0,
            "Interpreter",
            self.v_python,
            self._browse_python,
            ("Install dependencies", self.install_deps),
        )
        self.lbl_python = self._status(f, 1)
        row += 1

        # 2. Tesseract
        f = self._step(outer, row, "2  OCR (Tesseract)")
        self._path_row(
            f,
            0,
            "tesseract.exe",
            self.v_tess,
            self._browse_tesseract,
            ("Install with winget", self.install_tesseract),
        )
        self.lbl_tess = self._status(f, 1)
        row += 1

        # 3. Options
        f = self._step(outer, row, "3  Options")
        self._path_row(f, 0, "Log folder", self.v_logdir, self._browse_logdir)
        self._path_row(
            f,
            1,
            "Apps file",
            self.v_apps,
            self._browse_apps,
            ("Create apps.yaml", self.create_apps),
        )
        opts = ttk.Frame(f)
        opts.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            opts, text="Keep a local event log (events.jsonl)", variable=self.v_telemetry
        ).pack(side="left")
        ttk.Checkbutton(
            opts, text="Full UIA tree walk (slower, finds more)", variable=self.v_uia_walk
        ).pack(side="left", padx=(16, 0))
        ocr = ttk.Frame(f)
        ocr.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(ocr, text="OCR page mode").pack(side="left")
        ttk.Spinbox(ocr, from_=0, to=13, width=4, textvariable=self.v_psm).pack(
            side="left", padx=(6, 16)
        )
        ttk.Label(ocr, text="Fallback mode").pack(side="left")
        ttk.Spinbox(ocr, from_=0, to=13, width=4, textvariable=self.v_psm2).pack(
            side="left", padx=6
        )
        ttk.Label(ocr, text="defaults 11 / 6", style="Sub.TLabel").pack(side="left", padx=6)
        ttk.Label(f, text="Blank fields use the defaults.", style="Sub.TLabel").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        row += 1

        # 4. Connect
        f = self._step(outer, row, "4  Connect your AI clients")
        for i, c in enumerate(cfg.CLIENTS):
            ttk.Checkbutton(f, text=c.label, variable=self.v_clients[c.key]).grid(
                row=i, column=0, sticky="w", pady=2
            )
            lbl = ttk.Label(f, text="checking...", foreground=IDLE)
            lbl.grid(row=i, column=1, sticky="w", padx=8)
            self.client_status[c.key] = lbl
            ttk.Button(f, text="Copy config", command=lambda c=c: self.copy_config(c)).grid(
                row=i, column=2, sticky="e", padx=(0, 6)
            )
            ttk.Button(f, text="Remove", command=lambda c=c: self.remove(c)).grid(
                row=i, column=3, sticky="e"
            )
        f.columnconfigure(1, weight=1)
        bottom = ttk.Frame(f)
        bottom.grid(row=len(cfg.CLIENTS), column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            bottom,
            text="Also add the Iris usage guide to CLAUDE.md / AGENTS.md",
            variable=self.v_guide,
        ).pack(side="left")
        ttk.Button(
            bottom, text="Connect selected", style="Accent.TButton", command=self.connect
        ).pack(side="right")
        row += 1

        # 5. Test
        f = self._step(outer, row, "5  Test")
        ttk.Button(f, text="Run doctor", command=self.run_doctor).pack(side="left")
        ttk.Button(f, text="Run self-test", command=self.run_self_test).pack(side="left", padx=6)
        ttk.Button(f, text="Open log folder", command=self.open_logs).pack(side="left")
        ttk.Button(f, text="Re-check", command=self.refresh).pack(side="right")
        row += 1

        # Output
        out = ttk.Frame(outer)
        out.grid(row=row, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(row, weight=1)
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)
        self.text = tk.Text(
            out,
            height=9,
            wrap="word",
            font=("Cascadia Mono", 9),
            relief="flat",
            background="#f6f8fa",
            padx=8,
            pady=6,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(out, command=self.text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set, state="disabled")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.grid(row=row + 1, column=0, sticky="ew", pady=(6, 0))
        self.progress.grid_remove()

    def _step(self, parent, row: int, title: str) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text=title, style="Step.TLabelframe", padding=(10, 6, 10, 8))
        f.grid(row=row, column=0, sticky="ew", pady=4)
        f.columnconfigure(3, minsize=self.extra_w)
        f.columnconfigure(1, weight=1)
        return f

    def _path_row(self, f, row, label, var, browse, extra: tuple | None = None) -> None:
        """label | entry | Browse | optional action button (fixed width, so
        every step's columns line up)."""
        ttk.Label(f, text=label, width=12).grid(row=row, column=0, sticky="w")
        ttk.Entry(f, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(f, text="Browse", command=browse).grid(row=row, column=2, sticky="e")
        slot = ttk.Frame(f)
        slot.grid(row=row, column=3, sticky="e", padx=(6, 0))
        if extra:
            ttk.Button(slot, text=extra[0], command=extra[1], width=20).pack()

    def _status(self, f, row: int) -> ttk.Label:
        lbl = ttk.Label(f, text="checking...", foreground=IDLE)
        lbl.grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        return lbl

    # ------------------------------------------------------------ settings
    def settings(self) -> cfg.Settings:
        def num(var: tk.StringVar, default: int) -> int:
            try:
                return max(0, min(13, int(var.get())))
            except ValueError:
                return default

        return cfg.Settings(
            python=self.v_python.get().strip(),
            log_dir=self.v_logdir.get().strip(),
            telemetry=self.v_telemetry.get(),
            apps_file=self.v_apps.get().strip(),
            tesseract=self.v_tess.get().strip(),
            ocr_psm=num(self.v_psm, cfg.OCR_PSM_DEFAULT),
            ocr_pass2_psm=num(self.v_psm2, cfg.OCR_PASS2_PSM_DEFAULT),
            uia_walk=self.v_uia_walk.get(),
        )

    def _close(self) -> None:
        try:
            cfg.save_settings(self.settings())
        except OSError:
            pass
        self.root.destroy()

    # ------------------------------------------------------------ threading
    def _bg(self, fn, *args) -> None:
        self.busy += 1
        self.progress.grid()
        self.progress.start(12)

        def run():
            try:
                fn(*args)
            except Exception as exc:  # surface any failure in the output pane
                self.log(f"ERROR: {exc}")
            finally:
                self.events.put(("done", None))

        threading.Thread(target=run, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self.text.configure(state="normal")
                    self.text.insert("end", payload + "\n")
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif kind == "ui":
                    payload()
                elif kind == "done":
                    self.busy = max(0, self.busy - 1)
                    if not self.busy:
                        self.progress.stop()
                        self.progress.grid_remove()
            except Exception as exc:  # one bad update must not stop the pump
                self.log(f"UI error: {exc}")
        self.root.after(100, self._pump)

    def log(self, msg: str) -> None:
        self.events.put(("log", msg))

    def ui(self, fn) -> None:
        self.events.put(("ui", fn))

    def _stream(self, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
        self.log("> " + subprocess.list2cmdline(argv))
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", **(extra_env or {})}
        try:
            p = subprocess.Popen(
                argv,
                cwd=cfg.REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=cfg.NO_WINDOW,
            )
        except OSError as exc:
            self.log(f"could not start: {exc}")
            return 1
        assert p.stdout is not None
        for line in p.stdout:
            self.log(ANSI.sub("", line.rstrip()))
        code = p.wait()
        self.log(f"(exit {code})")
        return code

    # -------------------------------------------------------------- checks
    def refresh(self, first: bool = False) -> None:
        s = self.settings()
        self._bg(self._refresh, s, first)

    def _refresh(self, s: cfg.Settings, first: bool) -> None:
        chk = cfg.check_python(s.python or cfg.default_python())
        if chk.ok:
            text, color = f"Python {chk.version}, all dependencies installed", GOOD
        elif chk.missing:
            text, color = f"Python {chk.version}, missing: {', '.join(chk.missing)}", WARN
        else:
            text, color = chk.error, BAD
        self.ui(lambda t=text, c=color: self.lbl_python.configure(text="● " + t, foreground=c))

        tess = cfg.find_tesseract(s)
        if tess:
            how = "" if s.tesseract else " (auto-detected)"
            self.ui(
                lambda t=f"● Found {tess}{how}": self.lbl_tess.configure(text=t, foreground=GOOD)
            )
        else:
            self.ui(
                lambda: self.lbl_tess.configure(
                    text="● Not found. OCR fallback is off; UIA and pixel search still work.",
                    foreground=WARN,
                )
            )

        spec = cfg.launch_spec(s)
        for c in cfg.CLIENTS:
            st = c.status(spec)
            color = GOOD if st.registered and st.matches else WARN if st.installed else IDLE
            lbl = self.client_status[c.key]
            self.ui(
                lambda lbl=lbl, st=st, color=color: lbl.configure(
                    text="● " + st.label, foreground=color
                )
            )
            if first:
                var = self.v_clients[c.key]
                self.ui(lambda var=var, on=st.installed: var.set(on))
        if first:
            self.log(
                "Ready. Installed clients are ticked in step 4; press Connect selected when done."
            )

    # ------------------------------------------------------------- actions
    def install_deps(self) -> None:
        s = self.settings()
        argv = cfg.pip_install_command(s.python or cfg.default_python())
        self._bg(lambda: (self._stream(argv), self._refresh(s, False)))

    def install_tesseract(self) -> None:
        if not messagebox.askyesno(
            "Install Tesseract",
            "Install Tesseract OCR with winget?\n\n"
            + subprocess.list2cmdline(cfg.TESSERACT_WINGET)
            + "\n\nWindows may ask for permission.",
        ):
            return
        s = self.settings()
        self._bg(lambda: (self._stream(cfg.TESSERACT_WINGET), self._refresh(s, False)))

    def create_apps(self) -> None:
        path = cfg.create_apps_file()
        self.v_apps.set(str(path))
        self.log(f"apps file: {path}")
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError:
            pass

    def connect(self) -> None:
        s = self.settings()
        chosen = [c for c in cfg.CLIENTS if self.v_clients[c.key].get()]
        if not chosen:
            self.log("Pick at least one client in step 4.")
            return
        guide = self.v_guide.get()

        def check():  # the interpreter probe can take seconds; keep the window live
            chk = cfg.check_python(s.python or cfg.default_python())
            if chk.ok:
                work()
                return
            problem = chk.error or "missing " + ", ".join(chk.missing or [])

            def ask():
                if messagebox.askyesno(
                    "Python not ready",
                    f"The selected Python is not ready:\n{problem}"
                    "\n\nIris will fail to start until this is fixed. Connect anyway?",
                ):
                    self._bg(work)

            self.ui(ask)

        def work():
            try:
                cfg.save_settings(s)
            except OSError as exc:
                self.log(f"Could not save settings: {exc}")
            spec = cfg.launch_spec(s)
            for c in chosen:
                try:
                    self.log(c.register(spec))
                    if guide:
                        self.log(cfg.install_guide(c))
                except Exception as exc:  # one client failing must not block the rest
                    self.log(f"{c.label}: FAILED: {exc}")
            self._refresh(s, False)

        self._bg(check)

    def remove(self, c: cfg.Client) -> None:
        if not messagebox.askyesno("Remove Iris", f"Remove Iris from {c.label}?"):
            return
        s = self.settings()

        def work():
            self.log(c.unregister())
            if c.guide_path() is not None:
                self.log(cfg.remove_guide(c))
            self._refresh(s, False)

        self._bg(work)

    def copy_config(self, c: cfg.Client) -> None:
        snippet = c.snippet(cfg.launch_spec(self.settings()))
        self.root.clipboard_clear()
        self.root.clipboard_append(snippet)
        self.log(f"Copied {c.label} config to the clipboard:\n{snippet}")

    def run_doctor(self) -> None:
        s = self.settings()
        py = cfg.console_python(s.python or cfg.default_python())
        self._bg(self._stream, [py, "-m", "iris.doctor"], s.env())

    def run_self_test(self) -> None:
        s = self.settings()
        py = cfg.console_python(s.python or cfg.default_python())
        code = (
            "import json; from iris.self_test import run_self_test; "
            "print(json.dumps(run_self_test(), indent=2))"
        )
        self.log(
            "Self-test opens a small test window and clicks inside it. Hands off for a few seconds."
        )
        self._bg(self._stream, [py, "-c", code], s.env())

    def open_logs(self) -> None:
        s = self.settings()
        if s.log_dir:
            path = Path(s.log_dir)
        elif (cfg.REPO_ROOT / "server.py").exists():
            path = cfg.REPO_ROOT / "logs"
        else:
            path = (
                Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iris-mcp" / "iris-mcp" / "Logs"
            )
        if path.exists():
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            self.log(f"No logs yet at {path}. Iris writes them on first start.")

    # -------------------------------------------------------------- browse
    def _browse_python(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose python.exe", filetypes=[("Python", "python.exe"), ("Programs", "*.exe")]
        )
        if p:
            self.v_python.set(cfg.console_python(p))
            self.refresh()

    def _browse_tesseract(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose tesseract.exe",
            filetypes=[("Tesseract", "tesseract.exe"), ("Programs", "*.exe")],
        )
        if p:
            self.v_tess.set(str(Path(p)))
            self.refresh()

    def _browse_logdir(self) -> None:
        p = filedialog.askdirectory(title="Choose a log folder")
        if p:
            self.v_logdir.set(str(Path(p)))

    def _browse_apps(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose apps.yaml", filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")]
        )
        if p:
            self.v_apps.set(str(Path(p)))


def main() -> int:
    if sys.platform != "win32":
        print("Iris runs on Windows only.")
        return 1
    _dpi_aware()
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
