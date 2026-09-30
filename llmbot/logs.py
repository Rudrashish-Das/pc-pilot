"""Log files with a hard cap on disk use.

bot.log (and data/jobs.jsonl and data/events.jsonl, the job and activity history when there's no Postgres) roll over
at LOG_FILE_MB into timestamped archives such as bot-20260930-151200.log.gz (gzip unless LOG_COMPRESS=false). After
every rollover, and at startup,
the oldest archives are deleted until everything together fits in LOG_MAX_TOTAL_MB, and archives older than
LOG_KEEP_DAYS are dropped (0 = no age limit). The live files are never deleted.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

MB = 1024 * 1024
PATTERNS = ("bot*.log*", "jobs*.jsonl*", "events*.jsonl*")  # what counts toward the cap, in the data folder
LIVE = {"bot.log", "jobs.jsonl", "events.jsonl"}

total_bytes = 2048 * MB
file_bytes = 50 * MB
compress = True
keep_days = 0.0


def configure(max_total_mb: float, file_mb: float, gzip_archives: bool, max_age_days: float) -> None:
    global total_bytes, file_bytes, compress, keep_days
    total_bytes = int(max(max_total_mb, 1) * MB)
    # a live file can't be deleted, so each one must fit well inside the cap (bot.log + jobs.jsonl + archives)
    file_bytes = int(min(max(file_mb, 0.1) * MB, total_bytes / 4))
    compress, keep_days = gzip_archives, max(max_age_days, 0)


def archive(path: Path) -> Path | None:
    """Move a live log aside as a timestamped archive (gzipped), then enforce the limits."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{int(now % 1 * 1000):03d}"  # sorts by name too
    target = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    n = 1
    while target.exists() or target.with_name(target.name + ".gz").exists():
        n += 1
        target = path.with_name(f"{path.stem}-{stamp}-{n}{path.suffix}")
    os.replace(path, target)
    if compress:
        gz = target.with_name(target.name + ".gz")
        with target.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        target.unlink()
        target = gz
    enforce(path.parent)
    return target


def enforce(folder: Path) -> list[Path]:
    """Delete the oldest archives until they fit in total_bytes minus room for each live file to grow to file_bytes
    (so the total never passes the cap between rollovers); also drop archives older than keep_days."""
    files = {p for pat in PATTERNS for p in folder.glob(pat) if p.is_file() and not p.name.endswith(".tmp")}
    archives = sorted((p for p in files if p.name not in LIVE), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in archives) + len(LIVE) * file_bytes
    removed = []
    cutoff = time.time() - keep_days * 86400 if keep_days else None
    for p in archives:
        too_old = cutoff is not None and p.stat().st_mtime < cutoff
        if total <= total_bytes and not too_old:
            continue
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        removed.append(p)
    return removed


class CappedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that rolls over into timestamped (gzipped) archives and keeps the folder under the cap."""

    def __init__(self, path: Path):
        super().__init__(path, maxBytes=file_bytes, backupCount=1, encoding="utf-8", delay=False)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            archive(Path(self.baseFilename))
        except OSError as e:  # e.g. the file is open elsewhere; keep logging to it and try again next time
            logging.getLogger("llmbot").warning("log rollover failed: %s", e)
        self.stream = self._open()
