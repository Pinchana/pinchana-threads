FROM python:3.13-slim

WORKDIR /workspace/pinchana-threads

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies required by CloakBrowser / Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libxss1 libgtk-3-0 \
    libasound2 libxtst6 libgbm1 libxshmfence1 \
    libxcomposite1 libxdamage1 libxrandr2 libpangocairo-1.0-0 \
    fonts-liberation libcurl4 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy pinchana-core (local path dependency) first
COPY pinchana-core/pyproject.toml pinchana-core/uv.lock pinchana-core/README.md ../pinchana-core/
RUN mkdir -p ../pinchana-core/src
COPY pinchana-core/src ../pinchana-core/src

# Copy scraper package files
COPY pinchana-threads/pyproject.toml pinchana-threads/uv.lock pinchana-threads/README.md ./
RUN uv sync --frozen --no-install-project

# Cache the large fallback-browser layer independently from application source.
RUN .venv/bin/python -c "from cloakbrowser.browser import ensure_binary; ensure_binary()" || true

RUN mkdir -p /app/cache

COPY pinchana-threads/src ./src
RUN uv sync --frozen

ENV CACHE_PATH=/app/cache
ENV CACHE_MAX_SIZE_GB=10.0
ENV CLOAKBROWSER_AUTO_UPDATE=false

EXPOSE 8088
CMD ["uv", "run", "uvicorn", "pinchana_threads.main:app", "--host", "0.0.0.0", "--port", "8088"]
