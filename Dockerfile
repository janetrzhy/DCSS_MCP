FROM python:3.12-slim

# Runtime deps and Debian's console DCSS package.
RUN apt-get update && apt-get install -y \
    crawl \
    ncurses-term \
    tmux \
    && rm -rf /var/lib/apt/lists/*

RUN test -x /usr/games/crawl \
    && /usr/games/crawl -version

ENV DCSS_BINARY=/usr/games/crawl
ENV DCSS_TERM=vt100

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# DCSS init file — force ASCII mode for clean AI-readable output
RUN mkdir -p /root/.crawl && echo "tile_display_mode = ascii" > /root/.crawl/init.txt

EXPOSE 8000

CMD ["python", "server.py"]
