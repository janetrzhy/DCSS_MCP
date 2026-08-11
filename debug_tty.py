#!/usr/bin/env python3
"""Collect tmux/DCSS tty diagnostics for Render startup failures."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SESSION = os.environ.get("DCSS_TMUX_SESSION", "dcss")
TMUX = os.environ.get("TMUX_BINARY", "/usr/bin/tmux")


def run(*args: str) -> str:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
        out = (cp.stdout or "") + (cp.stderr or "")
        return out.strip()
    except Exception as exc:
        return f"<error: {exc}>"


def main() -> None:
    pane = run(TMUX, "display-message", "-p", "-t", SESSION,
               "pane_pid=#{pane_pid} pane_tty=#{pane_tty} pane_current_command=#{pane_current_command} pane_current_path=#{pane_current_path}")
    print("[tmux]")
    print(pane)

    tty = ""
    for token in pane.split():
        if token.startswith("pane_tty="):
            tty = token.split("=", 1)[1]
            break

    print("\n[processes]")
    print(run("ps", "-eo", "pid,ppid,pgid,sid,tpgid,tty,stat,wchan:32,comm,args"))

    if tty:
        print("\n[stty]")
        print(run("sh", "-lc", f"stty -a < {tty}"))
        print("\n[tty foreground pgrp]")
        print(run("python", "-c", f"import os; fd=os.open('{tty}', os.O_RDONLY|os.O_NOCTTY); print(os.tcgetpgrp(fd)); os.close(fd)"))

    crawl_pids = run("pgrep", "-x", "crawl").split()
    for pid in crawl_pids:
        proc = Path("/proc") / pid
        print(f"\n[crawl {pid}]")
        for name in ("status", "wchan", "cmdline"):
            try:
                text = (proc / name).read_text(encoding="utf-8", errors="replace")
                if name == "cmdline":
                    text = text.replace("\x00", " ")
                print(f"{name}: {text.strip()[:4000]}")
            except Exception as exc:
                print(f"{name}: <error: {exc}>")
        for name in ("fd/0", "fd/1", "fd/2"):
            try:
                print(f"{name}: {os.readlink(proc / name)}")
            except Exception as exc:
                print(f"{name}: <error: {exc}>")


if __name__ == "__main__":
    main()
