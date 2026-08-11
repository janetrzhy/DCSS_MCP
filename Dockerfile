FROM python:3.12-slim-trixie

# Runtime deps plus a current console DCSS package. Debian stable still ships
# Crawl 0.28.0; trixie-backports provides 0.34.1.
RUN echo "deb http://deb.debian.org/debian trixie-backports main" > /etc/apt/sources.list.d/trixie-backports.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ncurses-term \
        procps \
        tmux \
    && apt-get install -y --no-install-recommends -t trixie-backports crawl \
    && rm -rf /var/lib/apt/lists/*

RUN test -x /usr/games/crawl \
    && /usr/games/crawl -version | tee /tmp/crawl-version.txt \
    && grep -q "0.34.1" /tmp/crawl-version.txt \
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

# Prebuild caches at image-build time, under the same user/HOME used at runtime.
RUN HOME=/home/dcss TERM=screen-256color /usr/games/crawl -builddb

# Build-time smoke test for the exact detached-tmux architecture used at runtime.
# Require a real DCSS menu marker, not merely any nonblank output.
RUN set -eu; \
    tmux kill-session -t dcss-smoke 2>/dev/null || true; \
    tmux new-session -d -s dcss-smoke -x 80 -y 24 \
      "cd /home/dcss && env HOME=/home/dcss TERM=screen-256color LINES=24 COLUMNS=80 /usr/games/crawl; code=\$?; printf '\nDCSS_SMOKE_EXIT code=%s\n' \"\$code\"; sleep 60"; \
    i=0; \
    while [ "$i" -lt 60 ]; do \
      out="$(tmux capture-pane -t dcss-smoke -p -S -23 2>/dev/null || true)"; \
      if printf '%s\n' "$out" | grep -Eq 'Dungeon Crawl|Stone Soup|New Game'; then \
        echo "DCSS detached tmux smoke test passed after ${i}s"; \
        printf '%s\n' "$out"; \
        tmux kill-session -t dcss-smoke 2>/dev/null || true; \
        exit 0; \
      fi; \
      sleep 1; \
      i=$((i + 1)); \
    done; \
    echo "DCSS detached tmux smoke test FAILED: no DCSS menu marker after 60s"; \
    tmux display-message -p -t dcss-smoke 'pane_pid=#{pane_pid} pane_tty=#{pane_tty} pane_command=#{pane_current_command} pane_dead=#{pane_dead} pane_dead_status=#{pane_dead_status}' || true; \
    pane_tty="$(tmux display-message -p -t dcss-smoke '#{pane_tty}' 2>/dev/null || true)"; \
    if [ -n "$pane_tty" ]; then stty -a < "$pane_tty" || true; fi; \
    ps -eo pid,ppid,pgid,sid,tpgid,tty,stat,wchan:24,comm,args || true; \
    tmux capture-pane -t dcss-smoke -p -S -23 || true; \
    tmux kill-session -t dcss-smoke 2>/dev/null || true; \
    exit 1

EXPOSE 8000

CMD ["python", "server.py"]
