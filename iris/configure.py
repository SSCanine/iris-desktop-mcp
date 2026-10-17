"""Register the Iris MCP server with Claude Code, Claude Desktop, and Codex.

Stdlib only, so it runs on a fresh clone before `pip install`. The setup UI
(`iris.setup_gui`) and the command line below both drive these functions:

    python -m iris.configure --client claude-code --client codex
    python -m iris.configure --client codex --print      # show, change nothing
    python -m iris.configure --client claude-desktop --remove

Every file this module edits is backed up first
(`<file>.iris-backup-<timestamp>`) and written atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SERVER_NAME = "iris"
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
GUIDE_FILE = PACKAGE_DIR / "agent_guide.md"
GUIDE_BEGIN = "<!-- iris-mcp:begin -->"
GUIDE_END = "<!-- iris-mcp:end -->"

# import name -> pip name, mirrors requirements.txt
REQUIRED_MODULES = {
    "mcp": "mcp",
    "mss": "mss",
    "PIL": "pillow",
    "pyautogui": "pyautogui",
    "win32api": "pywin32",
    "uiautomation": "uiautomation",
    "pytesseract": "pytesseract",
    "yaml": "pyyaml",
    "psutil": "psutil",
    "platformdirs": "platformdirs",
    "numpy": "numpy",
    "cv2": "opencv-python-headless",
}

TESSERACT_WINGET = [
    "winget",
    "install",
    "--id",
    "UB-Mannheim.TesseractOCR",
    "-e",
    "--accept-source-agreements",
    "--accept-package-agreements",
]

# Keep child consoles from flashing when the GUI runs a subprocess.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

OCR_PSM_DEFAULT = 11
OCR_PASS2_PSM_DEFAULT = 6


# ---------------------------------------------------------------------------
# Settings -> launch spec
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    """What the user picks in the setup UI. Becomes the server's env vars."""

    python: str = ""  # interpreter that has Iris's dependencies
    log_dir: str = ""  # IRIS_LOG_DIR (blank = default)
    telemetry: bool = True  # IRIS_TELEMETRY=0 when off
    apps_file: str = ""  # IRIS_APPS (blank = default search)
    tesseract: str = ""  # IRIS_TESSERACT (blank = auto-detect)
    ocr_psm: int = OCR_PSM_DEFAULT
    ocr_pass2_psm: int = OCR_PASS2_PSM_DEFAULT
    uia_walk: bool = False  # IRIS_UIA_WALK=1 forces full UIA tree walks

    def env(self) -> dict[str, str]:
        """Only non-default values, so client configs stay short."""
        env: dict[str, str] = {}
        if self.log_dir.strip():
            env["IRIS_LOG_DIR"] = self.log_dir.strip()
        if not self.telemetry:
            env["IRIS_TELEMETRY"] = "0"
        if self.apps_file.strip():
            env["IRIS_APPS"] = self.apps_file.strip()
        if self.tesseract.strip():
            env["IRIS_TESSERACT"] = self.tesseract.strip()
        if int(self.ocr_psm) != OCR_PSM_DEFAULT:
            env["IRIS_OCR_PSM"] = str(int(self.ocr_psm))
        if int(self.ocr_pass2_psm) != OCR_PASS2_PSM_DEFAULT:
            env["IRIS_OCR_PASS2_PSM"] = str(int(self.ocr_pass2_psm))
        if self.uia_walk:
            env["IRIS_UIA_WALK"] = "1"
        return env

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]

    def entry(self, with_type: bool = True) -> dict:
        out: dict = {"type": "stdio"} if with_type else {}
        out["command"] = self.command
        out["args"] = list(self.args)
        if self.env:
            out["env"] = dict(self.env)
        return out


def console_python(path: str | Path) -> str:
    """pythonw.exe has no stdout, so it cannot speak MCP stdio. Swap it."""
    p = Path(path)
    if p.name.lower() == "pythonw.exe":
        sibling = p.with_name("python.exe")
        if sibling.exists():
            return str(sibling)
    return str(p)


def default_python() -> str:
    return console_python(sys.executable)


def launch_spec(settings: Settings) -> LaunchSpec:
    python = console_python(settings.python.strip() or default_python())
    server = REPO_ROOT / "server.py"
    # A source checkout launches server.py by absolute path (clients start the
    # server from an arbitrary cwd). A pip install uses the package module.
    args = [str(server)] if server.exists() else ["-m", "iris"]
    return LaunchSpec(command=python, args=args, env=settings.env())


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------
_PROBE = (
    "import importlib.util, json, sys\n"
    "mods = json.loads(sys.argv[1])\n"
    "missing = [pip for mod, pip in mods.items() if importlib.util.find_spec(mod) is None]\n"
    "print(json.dumps({'version': sys.version.split()[0], 'missing': missing}))\n"
)


@dataclass
class PythonCheck:
    ok: bool
    version: str = ""
    missing: list[str] | None = None
    error: str = ""


def check_python(python: str) -> PythonCheck:
    python = console_python(python)
    if not Path(python).is_file():
        return PythonCheck(False, error=f"not a file: {python}")
    try:
        out = subprocess.run(
            [python, "-c", _PROBE, json.dumps(REQUIRED_MODULES)],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return PythonCheck(False, error=f"could not run {python}: {exc}")
    major_minor = tuple(int(x) for x in data["version"].split(".")[:2])
    if major_minor < (3, 10):
        return PythonCheck(False, data["version"], data["missing"], "Iris needs Python 3.10+")
    return PythonCheck(not data["missing"], data["version"], data["missing"])


def pip_install_command(python: str) -> list[str]:
    req = REPO_ROOT / "requirements.txt"
    if req.exists():
        return [console_python(python), "-m", "pip", "install", "-r", str(req)]
    return [console_python(python), "-m", "pip", "install", "--upgrade", "iris-mcp"]


def find_tesseract(settings: Settings) -> Path | None:
    explicit = settings.tesseract.strip().strip('"')
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    from iris.tesseract_bootstrap import locate_tesseract

    return locate_tesseract()


def user_config_dir() -> Path:
    """Same folder platformdirs gives iris.launcher, without needing platformdirs."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "iris-mcp" / "iris-mcp"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "iris-mcp"


def settings_file() -> Path:
    return user_config_dir() / "setup.json"


def load_settings() -> Settings:
    try:
        return Settings.from_dict(json.loads(settings_file().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return Settings(python=default_python())


def save_settings(settings: Settings) -> None:
    _write_atomic(settings_file(), json.dumps(asdict(settings), indent=2) + "\n")


def create_apps_file() -> Path:
    """Copy apps.yaml.example into the user config dir (never overwrites)."""
    target = user_config_dir() / "apps.yaml"
    if not target.exists():
        example = REPO_ROOT / "apps.yaml.example"
        if not example.exists():
            example = PACKAGE_DIR / "apps.yaml.example"
        text = (
            example.read_text(encoding="utf-8")
            if example.exists()
            else '# name:\n#   launch: "C:\\\\path\\\\to\\\\app.exe"\n'
        )
        _write_atomic(target, text)
    return target


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".iris-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.iris-backup-{stamp}")
    n = 1
    while dest.exists():  # never overwrite an earlier (possibly pristine) backup
        n += 1
        dest = path.with_name(f"{path.name}.iris-backup-{stamp}-{n}")
    shutil.copy2(path, dest)
    return dest


def _toml_str(s: str) -> str:
    # A JSON string is a valid TOML basic string (same escapes).
    return json.dumps(s, ensure_ascii=False)


_HEADER = re.compile(r"^\s*\[\[?\s*(?P<key>[^\]]+?)\s*\]\]?\s*(#.*)?$")


def _norm_key(key: str) -> str:
    return re.sub(r"\s+", "", key).replace('"', "").replace("'", "")


def remove_toml_server(text: str, name: str = SERVER_NAME) -> str:
    """Drop [mcp_servers.<name>] and its sub-tables; keep everything else."""
    own = f"mcp_servers.{name}"
    out: list[str] = []
    skipping = False
    # Blank/comment lines at the end of our block usually belong to the NEXT
    # table ("# keep this" above [mcp_servers.other]); hold them until we know.
    pending: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m:
            key = _norm_key(m.group("key"))
            mine = key == own or key.startswith(own + ".")
            if skipping and not mine:
                out.extend(pending)
            pending = []
            skipping = mine
        if not skipping:
            out.append(line)
        elif not m and (not line.strip() or line.lstrip().startswith("#")):
            pending.append(line)
        else:
            pending = []
    return "".join(out)


def toml_server_block(spec: LaunchSpec, name: str = SERVER_NAME) -> str:
    lines = [
        f"[mcp_servers.{name}]",
        f"command = {_toml_str(spec.command)}",
        "args = [" + ", ".join(_toml_str(a) for a in spec.args) + "]",
        # Iris imports OpenCV + UIA at startup; Codex's 10 s default is tight.
        "startup_timeout_sec = 30",
    ]
    if spec.env:
        pairs = ", ".join(f"{k} = {_toml_str(v)}" for k, v in spec.env.items())
        lines.append(f"env = {{ {pairs} }}")
    return "\n".join(lines) + "\n"


def upsert_toml_server(text: str, spec: LaunchSpec, name: str = SERVER_NAME) -> str:
    kept = remove_toml_server(text, name).rstrip()
    return (kept + "\n\n" if kept else "") + toml_server_block(spec, name)


def install_guide_text(text: str, guide: str) -> str:
    block = f"{GUIDE_BEGIN}\n{guide.strip()}\n{GUIDE_END}"
    if GUIDE_BEGIN in text and GUIDE_END in text:
        start = text.index(GUIDE_BEGIN)
        end = text.index(GUIDE_END, start) + len(GUIDE_END)
        return text[:start] + block + text[end:]
    kept = text.rstrip()
    return (kept + "\n\n" if kept else "") + block + "\n"


def remove_guide_text(text: str) -> str:
    if GUIDE_BEGIN not in text or GUIDE_END not in text:
        return text
    start = text.index(GUIDE_BEGIN)
    end = text.index(GUIDE_END, start) + len(GUIDE_END)
    return (text[:start].rstrip() + "\n" + text[end:].lstrip("\n")).lstrip("\n")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
@dataclass
class ClientStatus:
    installed: bool
    registered: bool
    matches: bool
    detail: str = ""

    @property
    def label(self) -> str:
        if not self.installed:
            return "not found"
        if not self.registered:
            return "not connected"
        return "connected" if self.matches else "connected (different settings)"


class Client:
    key = ""
    label = ""
    restart_hint = ""

    def installed(self) -> bool:
        raise NotImplementedError

    def current_entry(self) -> dict | None:
        raise NotImplementedError

    def register(self, spec: LaunchSpec) -> str:
        raise NotImplementedError

    def unregister(self) -> str:
        raise NotImplementedError

    def snippet(self, spec: LaunchSpec) -> str:
        raise NotImplementedError

    def guide_path(self) -> Path | None:
        return None

    def status(self, spec: LaunchSpec) -> ClientStatus:
        if not self.installed():
            return ClientStatus(False, False, False)
        entry = self.current_entry()
        if entry is None:
            return ClientStatus(True, False, False)
        want = spec.entry(with_type=False)
        have = {k: entry.get(k) for k in ("command", "args", "env") if entry.get(k)}
        return ClientStatus(True, True, have == want)


class ClaudeCode(Client):
    key = "claude-code"
    label = "Claude Code"
    restart_hint = "Restart Claude Code sessions to load Iris."

    def _home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def _config(self) -> Path:
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            return self._home() / ".claude.json"
        return Path.home() / ".claude.json"

    def _cli(self) -> str | None:
        return shutil.which("claude")

    def installed(self) -> bool:
        return bool(self._cli()) or self._config().exists()

    def current_entry(self) -> dict | None:
        try:
            data = json.loads(self._config().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return (data.get("mcpServers") or {}).get(SERVER_NAME)

    def _add_argv(self, spec: LaunchSpec, cli: str = "claude") -> list[str]:
        # --scope goes before the name and -e after it: -e is variadic and
        # swallows a following server name.
        argv = [cli, "mcp", "add", "--scope", "user", SERVER_NAME]
        for k, v in spec.env.items():
            argv += ["-e", f"{k}={v}"]
        return argv + ["--", spec.command, *spec.args]

    def register(self, spec: LaunchSpec) -> str:
        cli = self._cli()
        if not cli:
            return "claude CLI not on PATH. Run this yourself:\n" + self.snippet(spec)
        _check_shim_args(cli, self._add_argv(spec, cli))
        old = self.current_entry()
        if old is not None:
            _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])
        code, out = _run(self._add_argv(spec, cli))
        if code != 0:
            restored = ""
            if old and old.get("command"):
                prev = LaunchSpec(
                    old["command"], list(old.get("args") or []), dict(old.get("env") or {})
                )
                rc, _ = _run(self._add_argv(prev, cli))
                restored = (
                    " Previous entry restored."
                    if rc == 0
                    else " Previous entry could NOT be restored."
                )
            raise RuntimeError(f"claude mcp add failed ({code}): {out.strip()}{restored}")
        return f"Claude Code: registered (user scope). {self.restart_hint}"

    def unregister(self) -> str:
        cli = self._cli()
        if not cli:
            return f"claude CLI not on PATH. Run: claude mcp remove {SERVER_NAME} --scope user"
        code, out = _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])
        return "Claude Code: removed." if code == 0 else f"Claude Code: {out.strip()}"

    def snippet(self, spec: LaunchSpec) -> str:
        return subprocess.list2cmdline(self._add_argv(spec))

    def guide_path(self) -> Path | None:
        return self._home() / "CLAUDE.md"


class ClaudeDesktop(Client):
    key = "claude-desktop"
    label = "Claude Desktop"
    restart_hint = "Quit Claude Desktop from the tray and reopen it."

    def config_path(self) -> Path:
        candidates: list[Path] = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # Microsoft Store (MSIX) installs keep a virtualized copy.
            candidates += sorted(
                (Path(local) / "Packages").glob(
                    "Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"
                )
            )
        # A Store install reads its own virtualized copy first, so prefer it.
        ordered = candidates[1:] + candidates[:1] if appdata else candidates
        for c in ordered:
            if c.exists():
                return c
        for c in ordered:
            if c.parent.exists():
                return c
        return candidates[0] if candidates else Path.home() / "claude_desktop_config.json"

    def installed(self) -> bool:
        return self.config_path().parent.exists()

    def _load(self) -> dict:
        try:
            text = self.config_path().read_text(encoding="utf-8-sig")  # Notepad adds a BOM
        except FileNotFoundError:
            return {}
        if not text.strip():
            return {}
        data = json.loads(text)  # malformed JSON raises; never overwrite it blind
        if not isinstance(data, dict):
            raise ValueError(f"{self.config_path()} is not a JSON object")
        return data

    def current_entry(self) -> dict | None:
        try:
            servers = self._load().get("mcpServers")
        except (OSError, ValueError):
            return None
        return servers.get(SERVER_NAME) if isinstance(servers, dict) else None

    def register(self, spec: LaunchSpec) -> str:
        path = self.config_path()
        data = self._load()
        if not isinstance(data.get("mcpServers"), dict):
            data["mcpServers"] = {}
        data["mcpServers"][SERVER_NAME] = spec.entry(with_type=False)
        backup(path)
        _write_atomic(path, json.dumps(data, indent=2) + "\n")
        return f"Claude Desktop: wrote {path}. {self.restart_hint}"

    def unregister(self) -> str:
        path = self.config_path()
        data = self._load()
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or SERVER_NAME not in servers:
            return "Claude Desktop: not registered."
        backup(path)
        del data["mcpServers"][SERVER_NAME]
        _write_atomic(path, json.dumps(data, indent=2) + "\n")
        return "Claude Desktop: removed."

    def snippet(self, spec: LaunchSpec) -> str:
        return json.dumps({"mcpServers": {SERVER_NAME: spec.entry(with_type=False)}}, indent=2)


class Codex(Client):
    key = "codex"
    label = "Codex (CLI, app, IDE)"
    restart_hint = "Start a new Codex session to load Iris."

    def _home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    def config_path(self) -> Path:
        return self._home() / "config.toml"

    def installed(self) -> bool:
        return self._home().exists() or bool(shutil.which("codex"))

    def _text(self) -> str:
        try:
            return self.config_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def current_entry(self) -> dict | None:
        text = self._text()
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10: presence only
            return {} if f"[mcp_servers.{SERVER_NAME}]" in text else None
        try:
            return (tomllib.loads(text).get("mcp_servers") or {}).get(SERVER_NAME)
        except tomllib.TOMLDecodeError:
            return None

    def register(self, spec: LaunchSpec) -> str:
        path = self.config_path()
        new = upsert_toml_server(self._text(), spec)
        try:
            import tomllib

            tomllib.loads(new)  # refuse to write a file Codex cannot parse
        except ModuleNotFoundError:
            pass
        backup(path)
        _write_atomic(path, new)
        return f"Codex: wrote {path}. {self.restart_hint}"

    def unregister(self) -> str:
        path = self.config_path()
        text = self._text()
        new = remove_toml_server(text)
        if new == text:
            return "Codex: not registered."
        backup(path)
        _write_atomic(path, new)
        return "Codex: removed."

    def snippet(self, spec: LaunchSpec) -> str:
        return toml_server_block(spec)

    def guide_path(self) -> Path | None:
        return self._home() / "AGENTS.md"


CLIENTS: list[Client] = [ClaudeCode(), ClaudeDesktop(), Codex()]


def client_by_key(key: str) -> Client:
    for c in CLIENTS:
        if c.key == key:
            return c
    raise KeyError(key)


def install_guide(client: Client) -> str:
    path = client.guide_path()
    if path is None:
        return f"{client.label}: gets the guide from the server itself."
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = install_guide_text(old, GUIDE_FILE.read_text(encoding="utf-8"))
    if new == old:
        return f"{client.label}: guide already current in {path}"
    backup(path)
    _write_atomic(path, new)
    return f"{client.label}: guide added to {path}"


def remove_guide(client: Client) -> str:
    path = client.guide_path()
    if path is None or not path.exists():
        return f"{client.label}: no guide file."
    old = path.read_text(encoding="utf-8")
    new = remove_guide_text(old)
    if new == old:
        return f"{client.label}: no Iris guide in {path}"
    backup(path)
    _write_atomic(path, new)
    return f"{client.label}: guide removed from {path}"


_CMD_META = set('&|<>^%!"')


def _check_shim_args(cli: str, argv: list[str]) -> None:
    """npm installs `claude` as a .cmd shim, so arguments pass through cmd.exe,
    which list2cmdline quoting does not protect from & | < > ^ % ! "."""
    if Path(cli).suffix.lower() not in {".cmd", ".bat"}:
        return
    bad = [a for a in argv[1:] if _CMD_META & set(a)]
    if bad:
        raise ValueError(
            f'{bad[0]!r} contains a character cmd.exe would mangle (& | < > ^ % ! "). '
            "Pick a path without it, or use Copy config and run the command yourself."
        )


def _run(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m iris.configure", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "--client",
        action="append",
        choices=[c.key for c in CLIENTS],
        help="client to configure (repeatable; default: every installed one)",
    )
    ap.add_argument("--python", help="interpreter with Iris's dependencies (default: this one)")
    ap.add_argument("--remove", action="store_true", help="unregister instead of register")
    ap.add_argument(
        "--guide", action="store_true", help="also add the usage guide to CLAUDE.md / AGENTS.md"
    )
    ap.add_argument(
        "--print", dest="dry", action="store_true", help="print the config, change nothing"
    )
    args = ap.parse_args(argv)

    settings = load_settings()
    if args.python:
        settings.python = args.python
    spec = launch_spec(settings)
    clients = (
        [client_by_key(k) for k in args.client]
        if args.client
        else [c for c in CLIENTS if c.installed()]
    )
    if not clients:
        print("No supported client found (Claude Code, Claude Desktop, Codex).")
        return 1
    for c in clients:
        if args.dry:
            print(f"# {c.label}\n{c.snippet(spec)}\n")
            continue
        try:
            print(c.unregister() if args.remove else c.register(spec))
            if args.guide:
                print(remove_guide(c) if args.remove else install_guide(c))
        except Exception as exc:  # report and keep going with the next client
            print(f"{c.label}: FAILED: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
