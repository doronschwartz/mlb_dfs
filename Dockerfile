FROM python:3.12-slim

WORKDIR /app

# Install minimal build deps + cleanup
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY mlb_dfs ./mlb_dfs
RUN pip install --no-cache-dir -e .

# Drafts persist on a mounted volume in production; default to /data.
ENV MLB_DFS_DRAFT_DIR=/data/drafts
ENV MLB_DFS_ODDS_DIR=/data/odds
ENV MLB_DFS_CACHE_DIR=/data/cache
RUN mkdir -p /data/drafts /data/odds /data/cache

EXPOSE 8000
CMD ["sh", "-c", "uvicorn mlb_dfs.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
