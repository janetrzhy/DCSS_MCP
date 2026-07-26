FROM python:3.12-slim

ARG DCSS_VERSION=0.34.1

# Runtime deps: libfuse2 for AppImage extract, ncurses/lua/sqlite for DCSS
RUN apt-get update && apt-get install -y \
    curl \
    fuse \
    libncursesw6 \
    ncurses-term \
    liblua5.1-0 \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Download & extract DCSS console AppImage (--appimage-extract doesn't need FUSE)
RUN curl -sL -o /tmp/dcss.AppImage \
    "https://github.com/crawl/crawl/releases/download/${DCSS_VERSION}/dcss-${DCSS_VERSION}-linux-console.x86_64.AppImage" \
    && chmod +x /tmp/dcss.AppImage \
    && cd /tmp \
    && ./dcss.AppImage --appimage-extract > /dev/null 2>&1 \
    && mv squashfs-root /opt/dcss \
    && test -x /opt/dcss/usr/bin/crawl \
    && printf '#!/bin/sh\nexec /opt/dcss/usr/bin/crawl "$@"\n' > /usr/local/bin/crawl \
    && chmod +x /usr/local/bin/crawl \
    && /usr/local/bin/crawl -version \
    && rm -f /tmp/dcss.AppImage

ENV DCSS_BINARY=/opt/dcss/usr/bin/crawl
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
