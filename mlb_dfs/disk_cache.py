"""Persistent disk cache. Wraps a function so its return value is written
to /data/cache/{hash}.json with a TTL. Survives deploys and restarts."""
from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from typing import Callable

CACHE_DIR = os.environ.get(
    "MLB_DFS_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache"),
)


def _path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def cached_disk(ttl_seconds: int, *, namespace: str | None = None):
    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ns = namespace or fn.__name__
            blob = f"{ns}:{repr(args)}:{repr(sorted(kwargs.items()))}"
            key = hashlib.md5(blob.encode()).hexdigest()
            p = _path(key)
            try:
                if os.path.exists(p):
                    age = time.time() - os.path.getmtime(p)
                    if age < ttl_seconds:
                        with open(p) as f:
                            return json.load(f)
            except Exception:
                pass
            result = fn(*args, **kwargs)
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = f"{p}.tmp"
                with open(tmp, "w") as f:
                    json.dump(result, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, p)
            except Exception:
                pass
            return result
        return wrapper
    return decorator
