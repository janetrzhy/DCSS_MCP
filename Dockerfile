FROM python:3.12-slim

# Runtime deps and Debian's console DCSS package.
RUN apt-get update && apt-get install -y \
    crawl \
    ncurses-term \
    procps \
    tmux \
    && rm -rf /var/lib/apt/lists/*

RUN test -x /usr/games/crawl \
    && /usr/games/crawl -version \
    && infocmp screen-256color >/dev/null

ENV DCSS_BINARY=/usr/games/crawl
ENV DCSS_TERM=screen-256color
ENV DCSS_HOME=/home/dcss

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

RUN useradd -m -d /home/dcss dcss \
    && chown -R dcss:dcss /app

USER dcss

# DCSS init file — force ASCII mode for clean AI-readable output.
RUN mkdir -p /home/dcss/.crawl \
    && echo "tile_display_mode = ascii" > /home/dcss/.crawl/init.txt

# Crawl performs database and map initialisation before curses/terminal startup.
# Build those caches into the image so a Render cold start does not spend its
# first game launch CPU-bound with an intentionally blank terminal.
RUN HOME=/home/dcss TERM=screen-256color /usr/games/crawl -builddb

# Reproduce the production architecture during image build: a fully detached
# tmux server launches console Crawl with no outside terminal attached. If this
# cannot paint a pane within 60 seconds, fail the image build with tty/process
# diagnostics instead of discovering the same blank screen after deployment.
RUN set -eu; \
    tmux kill-session -t dcss-smoke 2>/dev/null || true; \
    tmux new-session -d -s dcss-smoke -x 80 -y 24 \
      "cd /home/dcss && env HOME=/home/dcss TERM=screen-256color LINES=24 COLUMNS=80 /usr/games/crawl; code=\$?; printf '\nDCSS_SMOKE_EXIT code=%s\n' \"\$code\"; sleep 60"; \
    i=0; \
    while [ "$i" -lt 60 ]; do \
      out="$(tmux capture-pane -t dcss-smoke -p -S -23 2>/dev/null || true)"; \
      visible="$(printf '%s' "$out" | tr -d '[:space:]')"; \
      if [ -n "$visible" ]; then \
        echo "DCSS detached tmux smoke test passed after ${i}s"; \
        printf '%s\n' "$out"; \
        tmux kill-session -t dcss-smoke 2>/dev/null || true; \
        exit 0; \
      fi; \
      sleep 1; \
      i=$((i + 1)); \
    done; \
    echo "DCSS detached tmux smoke test FAILED: pane stayed blank for 60s"; \
    tmux display-message -p -t dcss-smoke 'pane_pid=#{pane_pid} pane_tty=#{pane_tty} pane_command=#{pane_current_command} pane_dead=#{pane_dead} pane_dead_status=#{pane_dead_status}' || true; \
    pane_tty="$(tmux display-message -p -t dcss-smoke '#{pane_tty}' 2>/dev/null || true)"; \
    if [ -n "$pane_tty" ]; then stty -a < "$pane_tty" || true; fi; \
    ps -eo pid,ppid,pgid,sid,tpgid,tty,stat,wchan:24,comm,args || true; \
    tmux capture-pane -t dcss-smoke -p -S -23 || true; \
    tmux kill-session -t dcss-smoke 2>/dev/null || true; \
    exit 1

EXPOSE 8000

CMD ["python", "server.py"]
