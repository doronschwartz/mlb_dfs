"""Persistent disk cache. Wraps a function so its return value is written
to {CACHE_DIR}/{hash}.json with a TTL. Survives deploys and restarts.

Bounded growth: enforces a directory-level size cap via LRU eviction on every
write. Without this, the cache grew to 958MB on a 1GB volume and started
ENOSPC-ing other writes (trivia, draft saves) — see the 2026-05-13 incident.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
from typing import Callable

CACHE_DIR = os.environ.get(
    "MLB_DFS_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache"),
)

# Size cap with LRU eviction. 400MB out of a 1GB Fly volume leaves ~500MB
# for drafts/trivia/odds/lost+found, which is >100x what those need.
# Override with MLB_DFS_CACHE_MAX_BYTES if the volume size changes.
CACHE_MAX_BYTES = int(os.environ.get("MLB_DFS_CACHE_MAX_BYTES", 400 * 1024 * 1024))
# Evict in batches so we don't run the directory scan on every single write.
# Once we exceed the cap, we drop oldest files until we're back to 85% capacity.
_EVICT_TARGET_PCT = 0.85
# Throttle expensive size scans — at most once every N seconds across the process.
_EVICT_MIN_INTERVAL = 60
_last_evict_check = 0.0


def _path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _dir_size_and_files() -> tuple[int, list[tuple[float, int, str]]]:
    """Returns (total_bytes, [(mtime, size, path), ...]) for the cache dir."""
    total = 0
    files: list[tuple[float, int, str]] = []
    try:
        with os.scandir(CACHE_DIR) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                except FileNotFoundError:
                    continue
                total += st.st_size
                files.append((st.st_mtime, st.st_size, entry.path))
    except FileNotFoundError:
        return 0, []
    except Exception as e:
        logging.warning("disk_cache scandir failed: %s", e)
    return total, files


def _evict_if_over_cap(force: bool = False) -> None:
    """Delete oldest cache files until we're back under _EVICT_TARGET_PCT of cap."""
    global _last_evict_check
    now = time.time()
    if not force and (now - _last_evict_check) < _EVICT_MIN_INTERVAL:
        return
    _last_evict_check = now
    total, files = _dir_size_and_files()
    if total < CACHE_MAX_BYTES:
        return
    target = int(CACHE_MAX_BYTES * _EVICT_TARGET_PCT)
    # Oldest first.
    files.sort(key=lambda x: x[0])
    freed = 0
    deleted = 0
    for _mtime, size, path in files:
        if total - freed <= target:
            break
        try:
            os.unlink(path)
            freed += size
            deleted += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("disk_cache eviction failed for %s: %s", path, e)
    if deleted:
        logging.info(
            "disk_cache evicted %d files (%.1f MB) — was %.1f MB, target %.1f MB",
            deleted, freed / 1e6, total / 1e6, target / 1e6,
        )


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
            # Run eviction BEFORE the write — if the volume is already full,
            # the tmp file create would fail with ENOSPC and we'd never recover.
            try:
                _evict_if_over_cap()
            except Exception as e:
                logging.warning("disk_cache pre-write eviction failed: %s", e)
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = f"{p}.tmp"
                with open(tmp, "w") as f:
                    json.dump(result, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, p)
            except OSError as e:
                # If we still ran out of space, force-evict aggressively and retry once.
                if e.errno == 28:
                    logging.warning("disk_cache hit ENOSPC, force-evicting and retrying")
                    try:
                        _evict_if_over_cap(force=True)
                        tmp = f"{p}.tmp"
                        with open(tmp, "w") as f:
                            json.dump(result, f)
                        os.replace(tmp, p)
                    except Exception as e2:
                        logging.warning("disk_cache retry after eviction failed: %s", e2)
                else:
                    logging.warning("disk_cache write failed: %s", e)
            except Exception as e:
                logging.warning("disk_cache write failed: %s", e)
            return result
        return wrapper
    return decorator


def gc(force: bool = True) -> dict:
    """Manually trigger a GC pass and return stats. Useful from a one-off
    admin endpoint or REPL after the cache has blown past the cap."""
    before_total, before_files = _dir_size_and_files()
    _evict_if_over_cap(force=force)
    after_total, after_files = _dir_size_and_files()
    return {
        "before_mb": round(before_total / 1e6, 1),
        "after_mb": round(after_total / 1e6, 1),
        "files_before": len(before_files),
        "files_after": len(after_files),
        "cap_mb": round(CACHE_MAX_BYTES / 1e6, 1),
    }
