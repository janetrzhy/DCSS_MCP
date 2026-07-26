"""DCSS process manager — runs the game in a pseudo-terminal.

Architecture:
  ┌──────────────┐
  │  DCSS MCP    │  sends keys / reads screen via this module
  │  Server      │
  └──────┬───────┘
         │ os.read / os.write
  ┌──────┴───────┐
  │   pty (pyte) │  VT100 terminal emulator → clean 80×24 text
  ├──────────────┤
  │   crawl      │  Dungeon Crawl Stone Soup process
  └──────────────┘

Save/restore life cycle:
   play → save_game() → send S → game exits → collect .cs files → upload PG
   load → download PG → write .cs files → start crawl → auto-resume
"""

import os
import signal
import time
import logging
import shutil
from pathlib import Path
from typing import NoReturn

import pyte

logger = logging.getLogger(__name__)

try:
    import pty
    import select
    import struct
    import fcntl
    import termios

    POSIX_PTY_AVAILABLE = True
    POSIX_PTY_ERROR: Exception | None = None
except (ImportError, ModuleNotFoundError) as exc:
    POSIX_PTY_AVAILABLE = False
    POSIX_PTY_ERROR = exc

DCSS_BINARY = os.environ.get("DCSS_BINARY", "/usr/local/bin/crawl")
SAVE_DIR = Path(os.environ.get("DCSS_SAVE_DIR", "/tmp/dcss-saves"))
HOME_DIR = Path("/tmp/dcss-home")
DCSS_TERM = os.environ.get("DCSS_TERM", "vt100")

# ── PTY size ──────────────────────────────────────────────────────────
COLS, ROWS = 80, 24

# ── timeouts (seconds) ────────────────────────────────────────────────
SCREEN_STABLE_SEC = 0.35
SEND_WAIT_SEC = 0.15


class DcssEngine:
    """Manages one DCSS process running in a pty."""

    def __init__(self):
        self.master_fd: int | None = None
        self.child_pid: int | None = None
        self.is_running: bool = False

        # pyte terminal emulator
        self._screen = pyte.Screen(COLS, ROWS)
        self._stream = pyte.Stream(self._screen)
        self._raw_tail = ""

        # ensure directories
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_home_config()

    # ── life cycle ──────────────────────────────────────────────────────

    def start(self, *extra_args: str) -> None:
        """Spawn crawl in a pty."""
        if not POSIX_PTY_AVAILABLE:
            raise RuntimeError(
                "DCSS engine requires a Linux/POSIX pty. "
                f"Run it in Docker or on Linux. Import error: {POSIX_PTY_ERROR}"
            )
        self._validate_binary()

        if self.is_running:
            self.stop()

        self._screen.reset()
        pid, fd = pty.fork()

        if pid == 0:  # child
            self._child_setup(extra_args)
            # never returns
        else:  # parent
            self.child_pid = pid
            self.master_fd = fd
            self._set_pty_size(fd, ROWS, COLS)
            self.is_running = True
            # wait for first screen draw
            self.wait_for_stable(timeout=3.0, require_nonblank=True)
            if self._poll_child_exit():
                screen = self._display_text().strip()
                detail = (
                    f"DCSS exited immediately. DCSS_BINARY={DCSS_BINARY!r}, "
                    f"realpath={os.path.realpath(DCSS_BINARY)!r}."
                )
                if screen:
                    detail += f"\nLast terminal output:\n{screen}"
                raise RuntimeError(detail)
            logger.info("DCSS started (pid=%d).", pid)

    def stop(self) -> None:
        """Kill the DCSS process."""
        if not self.is_running or self.child_pid is None:
            self._reset_state()
            return
        try:
            os.kill(self.child_pid, signal.SIGTERM)
            # give it 5 s to exit gracefully
            for _ in range(50):
                wpid, status = os.waitpid(self.child_pid, os.WNOHANG)
                if wpid == self.child_pid:
                    break
                time.sleep(0.1)
            else:
                os.kill(self.child_pid, signal.SIGKILL)
                os.waitpid(self.child_pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        self._reset_state()
        logger.info("DCSS stopped.")

    # ── I/O ─────────────────────────────────────────────────────────────

    def send_keys(self, keys: str) -> None:
        """Write keystrokes into the pty."""
        if not self.is_running or self.master_fd is None:
            raise RuntimeError("DCSS is not running.")
        raw = keys.encode("utf-8")
        os.write(self.master_fd, raw)
        time.sleep(SEND_WAIT_SEC)
        self._drain_output()

    def read_screen(self) -> str:
        """Return the current emulated terminal screen as plain text."""
        self._drain_output()
        return self._display_text()

    def diagnostics(self) -> dict:
        """Return lightweight runtime diagnostics for MCP status/debug output."""
        binary = Path(DCSS_BINARY)
        screen = self._display_text()
        proc: dict[str, str] = {}
        if self.child_pid is not None:
            proc_dir = Path(f"/proc/{self.child_pid}")
            for name in ("status", "cmdline", "wchan"):
                path = proc_dir / name
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if name == "cmdline":
                        text = text.replace("\x00", " ").strip()
                    proc[name] = text[:1200]
                except OSError as exc:
                    proc[name] = f"<unavailable: {exc}>"
            for name in ("cwd", "exe"):
                path = proc_dir / name
                try:
                    proc[name] = os.readlink(path)
                except OSError as exc:
                    proc[name] = f"<unavailable: {exc}>"
        return {
            "is_running": self.is_running,
            "child_pid": self.child_pid,
            "master_fd": self.master_fd,
            "binary": DCSS_BINARY,
            "binary_realpath": os.path.realpath(DCSS_BINARY),
            "binary_exists": binary.exists(),
            "binary_executable": os.access(binary, os.X_OK),
            "term": DCSS_TERM,
            "rows": ROWS,
            "cols": COLS,
            "home": str(HOME_DIR),
            "save_dir": str(self._resolve_save_dir()),
            "screen_chars": len(screen),
            "screen_nonblank_chars": len(screen.strip()),
            "raw_tail_chars": len(self._raw_tail),
            "raw_tail_preview": self._raw_tail[-600:],
            "proc": proc,
        }

    def wait_for_stable(self, timeout: float = 6.0, require_nonblank: bool = False) -> str:
        """Wait until screen output stabilises (no changes for SCREEN_STABLE_SEC)."""
        last = ""
        stable_for = 0.0
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            self._drain_output()
            current = self._display_text()
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

        logger.warning("Screen did not stabilise within %.1f s.", timeout)
        return last

    # ── save / restore ──────────────────────────────────────────────────

    def collect_save_files(self) -> dict[str, bytes]:
        """Read all files from the save directory.  Returns {name: bytes}."""
        files: dict[str, bytes] = {}
        save_path = self._resolve_save_dir()
        if not save_path.exists():
            return files
        for p in sorted(save_path.iterdir()):
            if p.is_file():
                try:
                    files[p.name] = p.read_bytes()
                    logger.debug("Collected save file: %s (%d B)", p.name, p.stat().st_size)
                except OSError as exc:
                    logger.warning("Cannot read save file %s: %s", p.name, exc)
        return files

    def restore_save_files(self, files: dict[str, bytes]) -> None:
        """Write save files to the correct directory before starting DCSS."""
        save_path = self._resolve_save_dir()
        save_path.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (save_path / name).write_bytes(data)
            logger.debug("Restored save file: %s (%d B)", name, len(data))

    def clean_save_files(self) -> None:
        """Remove all save files (for a brand-new game)."""
        save_path = self._resolve_save_dir()
        if save_path.exists():
            for p in save_path.iterdir():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)

    def wait_for_exit(self, timeout: float = 5.0) -> bool:
        """Wait for the DCSS process to exit.  Returns True if it exited."""
        if self.child_pid is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                wpid, status = os.waitpid(self.child_pid, os.WNOHANG)
                if wpid == self.child_pid:
                    self._reset_state()
                    return True
            except ChildProcessError:
                self._reset_state()
                return True
            time.sleep(0.1)
        return False

    # ── internals ───────────────────────────────────────────────────────

    def _child_setup(self, extra_args: tuple[str, ...]) -> NoReturn:
        """Set up the child process environment and exec crawl."""
        os.chdir(str(HOME_DIR))
        os.environ["HOME"] = str(HOME_DIR)
        os.environ["TERM"] = DCSS_TERM
        os.environ["LINES"] = str(ROWS)
        os.environ["COLUMNS"] = str(COLS)
        os.environ["DCSS_SAVE_DIR"] = str(SAVE_DIR)
        self._set_pty_size(1, ROWS, COLS)

        args = [DCSS_BINARY]
        args.extend(extra_args)
        try:
            os.execv(DCSS_BINARY, args)
        except OSError as exc:
            print(
                f"exec failed for {DCSS_BINARY!r} "
                f"(realpath={os.path.realpath(DCSS_BINARY)!r}): {exc}",
                flush=True,
            )
            os._exit(127)  # noqa

    def _resolve_save_dir(self) -> Path:
        """DCSS saves inside ~/.crawl/saves/ by default."""
        return HOME_DIR / ".crawl" / "saves"

    def _validate_binary(self) -> None:
        binary = Path(DCSS_BINARY)
        if not binary.exists():
            raise FileNotFoundError(
                f"DCSS binary does not exist: {DCSS_BINARY!r} "
                f"(realpath={os.path.realpath(DCSS_BINARY)!r})."
            )
        if not os.access(binary, os.X_OK):
            raise PermissionError(f"DCSS binary is not executable: {DCSS_BINARY!r}.")

    def _ensure_home_config(self) -> None:
        """Write a minimal crawl init for stable ASCII terminal output."""
        crawl_dir = HOME_DIR / ".crawl"
        crawl_dir.mkdir(parents=True, exist_ok=True)
        init_file = crawl_dir / "init.txt"
        if not init_file.exists():
            init_file.write_text(
                "\n".join([
                    "tile_display_mode = ascii",
                    "",
                ]),
                encoding="utf-8",
            )

    def _poll_child_exit(self) -> bool:
        """Return True and reset state if the crawl child has exited."""
        if self.child_pid is None:
            return False
        try:
            wpid, _status = os.waitpid(self.child_pid, os.WNOHANG)
            if wpid == self.child_pid:
                self._reset_state()
                return True
        except ChildProcessError:
            self._reset_state()
            return True
        return False

    def _drain_output(self) -> None:
        """Read all pending bytes from the pty and feed them to pyte."""
        if self.master_fd is None:
            return
        while True:
            r, _, _ = select.select([self.master_fd], [], [], 0)
            if not r:
                break
            try:
                data = os.read(self.master_fd, 8192)
                if not data:
                    break
                decoded = data.decode("utf-8", errors="replace")
                self._raw_tail = (self._raw_tail + decoded)[-4000:]
                self._stream.feed(decoded)
            except (OSError, ValueError):
                break

    def _display_text(self) -> str:
        """Render the emulated screen as newline-separated text."""
        lines = self._screen.display
        return "\n".join(line.rstrip() for line in lines)

    def _set_pty_size(self, fd: int, rows: int, cols: int) -> None:
        """Resize the pty window."""
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)

    def _reset_state(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.is_running = False
        self.master_fd = None
        self.child_pid = None
