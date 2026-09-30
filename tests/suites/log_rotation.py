"""Log rotation with a total cap: rollover into gzipped archives, oldest deleted first, the live file never deleted,
age limit, jobs.jsonl rotated the same way, and the settings read from the environment."""
import gzip, logging, os, time

os.environ.update(LOG_MAX_TOTAL_MB="1", LOG_FILE_MB="0.1", LOG_KEEP_DAYS="0", LOG_COMPRESS="true")
from _setup import B, TMP  # noqa  (first: isolates the bot)
from llmbot import logs, store

MB = logs.MB


def check(c, label):
    assert c, label
    print("  ok", label)


def used(folder):
    return sum(p.stat().st_size for pat in logs.PATTERNS for p in folder.glob(pat) if p.is_file())


print("== settings from .env")
check(B.LOG_MAX_TOTAL_MB == 1 and logs.total_bytes == 1 * MB, "LOG_MAX_TOTAL_MB")
check(logs.file_bytes == int(0.1 * MB) and logs.compress, "LOG_FILE_MB, LOG_COMPRESS")
logs.configure(1, 900, True, 0)
check(logs.file_bytes == MB // 4, "a file size bigger than the cap is clamped to a quarter of it")
logs.configure(1, 0.1, True, 0)

print("== rollover and cap")
folder = TMP / "logs"; folder.mkdir()
(folder / "bot.log.1").write_bytes(b"x" * 1000)  # a backup from the old 2 MB x 3 scheme counts too
old = time.time() - 3600
os.utime(folder / "bot.log.1", (old, old))
h = logs.CappedRotatingFileHandler(folder / "bot.log")
h.setFormatter(logging.Formatter("%(message)s"))
lg = logging.getLogger("rotation-test"); lg.propagate = False; lg.addHandler(h); lg.setLevel(logging.INFO)
line = os.urandom(300).hex()  # random text barely compresses, so the cap is really exercised
for i in range(12000):  # ~7 MB of log text
    lg.info("%d %s", i, os.urandom(300).hex())
h.close()
arch = sorted(folder.glob("bot-*.log.gz"), key=lambda p: p.stat().st_mtime)
check(arch and all(gzip.open(a).read(10) for a in arch), f"archives are gzip files ({len(arch)} kept)")
check(used(folder) <= 1 * MB, f"all logs together stay under 1 MB: {used(folder) / MB:.2f} MB")
check((folder / "bot.log").exists(), "the live bot.log is never deleted")
check(not (folder / "bot.log.1").exists(), "oldest (the old-style backup) went first")
newest = gzip.open(arch[-1]).read().decode().splitlines()
check(newest and int(newest[-1].split()[0]) > 11000, "the newest lines are kept")

print("== age limit")
logs.configure(1, 0.1, True, 1)
stale = folder / "bot-20200101-000000.log.gz"; stale.write_bytes(b"old")
os.utime(stale, (time.time() - 3 * 86400,) * 2)
check(stale in logs.enforce(folder) and not stale.exists(), "LOG_KEEP_DAYS=1 drops a 3-day-old archive")
logs.configure(1, 0.1, False, 0)

print("== jobs.jsonl (file storage) rotates too, uncompressed when LOG_COMPRESS=false")
fs = store.FileStore(folder)
for i in range(600):
    fs.record_job({"job_id": str(i), "prompt": os.urandom(300).hex()})
check(any(folder.glob("jobs-*.jsonl")) and not any(folder.glob("jobs-*.gz")), "jobs archives, not gzipped")
check(fs.jobs_file.exists() and used(folder) <= 1 * MB, f"still under the cap: {used(folder) / MB:.2f} MB")
print("\nALL LOG ROTATION CHECKS PASSED")
