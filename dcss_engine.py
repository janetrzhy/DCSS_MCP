"""DCSS process manager backed by tmux.

The MCP server does not need to own a curses pty.  DCSS runs inside a tmux
session, while tools read with ``capture-pane`` and act with ``send-keys``.
This mirrors the most reliable CLI-agent setup and works well on Render.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

DCSS_BINARY = os.environ.get("DCSS_BINARY", "/usr/games/crawl")
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", "/usr/share/crawl"))
TMUX_BINARY = os.environ.get("TMUX_BINARY", "/usr/bin/tmux")
TMUX_SESSION = os.environ.get("DCSS_TMUX_SESSION", "dcss")
SAVE_DIR = Path(os.environ.get("DCSS_SAVE_DIR", "/tmp/dcss-saves"))
HOME_DIR = Path(os.environ.get("DCSS_HOME", "/tmp/dcss-home"))
DCSS_TERM = os.environ.get("DCSS_TERM", "xterm-256color")

COLS, ROWS = 80, 24
SEND_WAIT_SEC = 0.18
SCREEN_STABLE_SEC = 0.35


class DcssEngine:
    """Manage one DCSS game running inside a tmux session."""

    def __init__(self):
        self.session_name = TMUX_SESSION
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_home_config()

    @property
    def is_running(self) -> bool:
        return self._tmux_ok("has-session", "-t", self.session_name)

    def start(self, *extra_args: str) -> None:
        """Start crawl in a fresh tmux session."""
        self._validate_runtime()
        self.stop()

        command = self._crawl_shell_command(extra_args)
        self._run_tmux(
            "new-session",
            "-d",
            "-s",
            self.session_name,
            "-x",
            str(COLS),
            "-y",
            str(ROWS),
            command,
        )
        self.wait_for_stable(timeout=8.0, require_nonblank=True)
        screen = self.read_screen()
        if "DCSS_PROCESS_EXITED" in screen:
            raise RuntimeError(f"DCSS exited during startup.\n{screen}")
        if not self.is_running:
            raise RuntimeError(f"DCSS tmux session exited immediately. command={command!r}")

    def stop(self) -> None:
        if self.is_running:
            self._run_tmux("kill-session", "-t", self.session_name, check=False)

    def send_keys(self, keys: str) -> None:
        if not self.is_running:
            raise RuntimeError("DCSS is not running.")
        for key in _split_tmux_keys(keys):
            self._run_tmux("send-keys", "-t", self.session_name, key)
        time.sleep(SEND_WAIT_SEC)

    def read_screen(self) -> str:
        if not self.is_running:
            return ""
        result = self._run_tmux(
            "capture-pane",
            "-t",
            self.session_name,
            "-p",
            "-S",
            f"-{ROWS - 1}",
            check=True,
        )
        return _normalise_screen(result.stdout)

    def wait_for_stable(self, timeout: float = 6.0, require_nonblank: bool = False) -> str:
        last = ""
        stable_for = 0.0
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            current = self.read_screen()
            if require_nonblank and not current.strip():
                last = current
                stable_for = 0.0
                time.sleep(0.08)
                continue
            if current == last:
                stable_for += 0.08
                if stable_for >= SCREEN_STABLE_SEC:
                    return current
            else:
                last = current
                stable_for = 0.0
            time.sleep(0.08)
        return last

    def wait_for_exit(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                return True
            time.sleep(0.1)
        return False

    def collect_save_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        save_path = self._resolve_save_dir()
        if not save_path.exists():
            return files
        for p in sorted(save_path.iterdir()):
            if p.is_file():
                files[p.name] = p.read_bytes()
        return files

    def restore_save_files(self, files: dict[str, bytes]) -> None:
        save_path = self._resolve_save_dir()
        save_path.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (save_path / name).write_bytes(data)

    def clean_save_files(self) -> None:
        save_path = self._resolve_save_dir()
        if save_path.exists():
            for p in save_path.iterdir():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)

    def diagnostics(self) -> dict:
        crawl = Path(DCSS_BINARY)
        tmux = Path(TMUX_BINARY)
        screen = self.read_screen()
        return {
            "backend": "tmux",
            "is_running": self.is_running,
            "session": self.session_name,
            "child_pid": None,
            "master_fd": None,
            "binary": DCSS_BINARY,
            "crawl_dir": str(CRAWL_DIR),
            "crawl_dir_exists": CRAWL_DIR.exists(),
            "binary_realpath": os.path.realpath(DCSS_BINARY),
            "binary_exists": crawl.exists(),
            "binary_executable": os.access(crawl, os.X_OK),
            "tmux_binary": TMUX_BINARY,
            "tmux_exists": tmux.exists(),
            "term": DCSS_TERM,
            "terminfo": self._terminfo_status(),
            "use_script": False,
            "script_binary": "",
            "script_exists": False,
            "rows": ROWS,
            "cols": COLS,
            "home": str(HOME_DIR),
            "save_dir": str(self._resolve_save_dir()),
            "screen_chars": len(screen),
            "screen_nonblank_chars": len(screen.strip()),
            "raw_tail_chars": 0,
            "raw_tail_preview": "",
            "proc": self._tmux_process_info(),
        }

    def _crawl_shell_command(self, extra_args: tuple[str, ...]) -> str:
        crawl = (
            f"env HOME={_sh(HOME_DIR)} TERM={_sh(DCSS_TERM)} "
            f"LINES={ROWS} COLUMNS={COLS} DCSS_SAVE_DIR={_sh(SAVE_DIR)} "
            f"{_sh(DCSS_BINARY)} -dir {_sh(CRAWL_DIR)}"
        )
        crawl += "".join(f" {_sh(arg)}" for arg in extra_args)
        parts = [
            f"cd {_sh(HOME_DIR)}",
            f"{crawl}; code=$?; printf '\\nDCSS_PROCESS_EXITED code=%s\\n' \"$code\"; sleep 300",
        ]
        return " && ".join(parts)

    def _validate_runtime(self) -> None:
        if not Path(TMUX_BINARY).exists():
            raise FileNotFoundError(f"tmux binary does not exist: {TMUX_BINARY!r}")
        if not Path(DCSS_BINARY).exists():
            raise FileNotFoundError(f"DCSS binary does not exist: {DCSS_BINARY!r}")
        if not os.access(DCSS_BINARY, os.X_OK):
            raise PermissionError(f"DCSS binary is not executable: {DCSS_BINARY!r}")
        if not CRAWL_DIR.exists():
            raise FileNotFoundError(f"DCSS data directory does not exist: {str(CRAWL_DIR)!r}")

    def _ensure_home_config(self) -> None:
        crawl_dir = HOME_DIR / ".crawl"
        crawl_dir.mkdir(parents=True, exist_ok=True)
        init_file = crawl_dir / "init.txt"
        if not init_file.exists():
            init_file.write_text("tile_display_mode = ascii\n", encoding="utf-8")

    def _resolve_save_dir(self) -> Path:
        return HOME_DIR / ".crawl" / "saves"

    def _run_tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [TMUX_BINARY, *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _tmux_ok(self, *args: str) -> bool:
        try:
            result = self._run_tmux(*args, check=False)
            return result.returncode == 0
        except Exception:
            return False

    def _tmux_process_info(self) -> dict[str, str]:
        if not self.is_running:
            return {}
        try:
            result = self._run_tmux(
                "display-message",
                "-p",
                "-t",
                self.session_name,
                "pid=#{pane_pid} command=#{pane_current_command} tty=#{pane_tty} cwd=#{pane_current_path}",
            )
            return {"tmux_pane": result.stdout.strip()}
        except Exception as exc:
            return {"tmux_pane": f"<unavailable: {exc}>"}

    def _terminfo_status(self) -> str:
        try:
            info = subprocess.run(
                ["infocmp", DCSS_TERM],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return "ok" if info.returncode == 0 else f"missing: {info.stderr.strip() or info.stdout.strip()}"
        except Exception as exc:
            return f"unavailable: {exc}"


def _sh(value: str | Path) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _normalise_screen(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(line.rstrip() for line in lines[-ROWS:])


def _split_tmux_keys(keys: str) -> list[str]:
    if not keys:
        return []
    if keys == "\r":
        return ["Enter"]

    out: list[str] = []
    i = 0
    special = {
        "\r": "Enter",
        "\n": "Enter",
        "\t": "Tab",
        "\x1b": "Escape",
        " ": "Space",
    }
    while i < len(keys):
        ch = keys[i]
        if ch in special:
            out.append(special[ch])
        else:
            out.append(ch)
        i += 1
    return out
