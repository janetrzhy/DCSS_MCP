"""DCSS process manager backed by tmux.

The MCP server does not need to own a curses pty. DCSS runs inside a tmux
session, while tools read with ``capture-pane`` and act with ``send-keys``.
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
STDERR_LOG = Path(os.environ.get("DCSS_STDERR_LOG", "/tmp/dcss-crawl-stderr.log"))

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
        STDERR_LOG.unlink(missing_ok=True)

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
            "process_table": self._process_table(),
        }

    def _crawl_shell_command(self, extra_args: tuple[str, ...]) -> str:
        crawl = (
            f"env HOME={_sh(HOME_DIR)} TERM={_sh(DCSS_TERM)} "
            f"LINES={ROWS} COLUMNS={COLS} "
            f"{_sh(DCSS_BINARY)}"
        )
        crawl += "".join(f" {_sh(arg)}" for arg in extra_args)
        parts = [
            f"cd {_sh(HOME_DIR)}",
            (
                f"{crawl} 2>{_sh(STDERR_LOG)}; code=$?; "
                "printf '\\nDCSS_PROCESS_EXITED code=%s\\n' \"$code\"; sleep 300"
            ),
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

    def _tty_diagnostics(self) -> str:
        """Inspect pane tty plus Crawl process state without ptrace."""
        if not self.is_running:
            return "<no tmux session>"
        try:
            pane = self._run_tmux(
                "display-message",
                "-p",
                "-t",
                self.session_name,
                "#{pane_pid}|#{pane_tty}|#{pane_current_command}|#{pane_current_path}",
            ).stdout.strip()
            fields = pane.split("|", 3)
            while len(fields) < 4:
                fields.append("")
            pane_pid, pane_tty, pane_cmd, pane_cwd = fields
            chunks = [
                f"pane_pid={pane_pid} pane_tty={pane_tty} "
                f"pane_command={pane_cmd} pane_cwd={pane_cwd}"
            ]

            if pane_tty:
                stty = subprocess.run(
                    ["sh", "-lc", f"stty -a < {_sh(pane_tty)}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                chunks.append("stty=" + ((stty.stdout or stty.stderr).strip() or "<blank>"))

            pgrep = subprocess.run(
                ["pgrep", "-x", "crawl"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            for pid in pgrep.stdout.split():
                proc = Path("/proc") / pid
                details = [f"crawl_pid={pid}"]
                for name in ("wchan", "status", "cmdline", "syscall"):
                    try:
                        text = (proc / name).read_text(encoding="utf-8", errors="replace")
                        if name == "cmdline":
                            text = text.replace("\x00", " ")
                        details.append(f"{name}={text.strip()[:1800]}")
                    except Exception as exc:
                        details.append(f"{name}=<error: {exc}>")

                try:
                    stat1 = (proc / "stat").read_text(encoding="utf-8", errors="replace")
                    fields1 = stat1.split()
                    utime1, stime1 = int(fields1[13]), int(fields1[14])
                    time.sleep(0.25)
                    stat2 = (proc / "stat").read_text(encoding="utf-8", errors="replace")
                    fields2 = stat2.split()
                    utime2, stime2 = int(fields2[13]), int(fields2[14])
                    details.append(
                        "cpu_ticks_250ms="
                        f"user:+{utime2 - utime1} system:+{stime2 - stime1} "
                        f"total_before={utime1 + stime1} total_after={utime2 + stime2}"
                    )
                except Exception as exc:
                    details.append(f"cpu_ticks_250ms=<error: {exc}>")

                try:
                    env = (proc / "environ").read_bytes().replace(b"\x00", b"\n").decode(
                        "utf-8", errors="replace"
                    )
                    keep = [
                        line
                        for line in env.splitlines()
                        if line.startswith(("HOME=", "TERM=", "LINES=", "COLUMNS=", "LANG=", "LC_"))
                    ]
                    details.append("environment=" + " ".join(keep))
                except Exception as exc:
                    details.append(f"environment=<error: {exc}>")

                for fdname in ("fd/0", "fd/1", "fd/2"):
                    try:
                        details.append(f"{fdname}={os.readlink(proc / fdname)}")
                    except Exception as exc:
                        details.append(f"{fdname}=<error: {exc}>")
                chunks.append("\n".join(details))

            chunks.append("startup_stderr=" + self._read_startup_stderr())
            return "\n".join(chunks)
        except Exception as exc:
            return f"<unavailable: {exc}>"

    def _read_startup_stderr(self) -> str:
        try:
            if not STDERR_LOG.exists():
                return "<file not created>"
            text = STDERR_LOG.read_text(encoding="utf-8", errors="replace")
            return text[-4000:] if text else "<empty>"
        except Exception as exc:
            return f"<unavailable: {exc}>"

    def _process_table(self) -> str:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,ppid,pgid,sid,tpgid,tty,stat,wchan:24,comm,args"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            lines = [
                line
                for line in result.stdout.splitlines()
                if any(token in line for token in ("tmux", "crawl", "sh", "python"))
            ]
            return "\n".join(lines[:40]) + "\nTTY diagnostics:\n" + self._tty_diagnostics()
        except Exception as exc:
            return f"<unavailable: {exc}>"

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
    special = {
        "\r": "Enter",
        "\n": "Enter",
        "\t": "Tab",
        "\x1b": "Escape",
        " ": "Space",
    }
    for ch in keys:
        out.append(special.get(ch, ch))
    return out
