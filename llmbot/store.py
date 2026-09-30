"""Where the bot keeps its bookkeeping: per-chat settings, scheduled tasks, reminders, daily spend, the local model's
chat memory, the pending power action, and a history of Claude Code jobs.

- Files (default, no setup): one JSON document per kind in data/, plus data/jobs.jsonl.
- Postgres (DATABASE_URL set): the same documents in the table llmbot_state (jsonb), and one row per Claude Code
  job in llmbot_jobs. On first use, a document that isn't in the database yet is imported from its data/ file, and
  the file is renamed to *.imported.

The rest of the bot keeps working on in-memory dicts and calls save() after each change, as it did with files.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("llmbot.store")

SCHEMA = """
create table if not exists llmbot_state (
    name        text primary key,
    data        jsonb not null,
    updated_at  timestamptz not null default now()
);
create table if not exists llmbot_jobs (
    id                bigserial primary key,
    finished_at       timestamptz not null default now(),
    job_id            text,
    frontend          text,
    channel_id        bigint,
    user_id           bigint,
    backend           text,
    model             text,
    perm              text,
    outcome           text,
    turns             integer,
    cost_usd          numeric(12, 6),
    session_cost_usd  numeric(12, 6),
    context_tokens    integer,
    seconds           real,
    session_id        text,
    prompt            text
);
create index if not exists llmbot_jobs_finished_at on llmbot_jobs (finished_at);
"""
JOB_COLUMNS = ("job_id", "frontend", "channel_id", "user_id", "backend", "model", "perm", "outcome", "turns",
               "cost_usd", "session_cost_usd", "context_tokens", "seconds", "session_id", "prompt")


class FileStore:
    kind = "files"

    def __init__(self, data_dir: Path):
        self.jobs_file = data_dir / "jobs.jsonl"

    def describe(self) -> str:
        return f"files in {self.jobs_file.parent}"

    def load(self, name: str, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except Exception:
            log.exception("Could not read %s; starting empty", path.name)
            return default

    def save(self, name: str, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, name: str, path: Path) -> None:
        path.unlink(missing_ok=True)

    def record_job(self, row: dict) -> None:
        try:
            with self.jobs_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Could not append to %s", self.jobs_file.name)


class PgStore:
    kind = "postgres"

    def __init__(self, url: str, wait: float = 60):
        import psycopg
        from psycopg.types.json import Jsonb

        self._psycopg, self._jsonb, self.url = psycopg, Jsonb, url
        self._lock = threading.Lock()
        self._conn = None
        deadline = time.monotonic() + wait
        while True:  # at logon the database service may still be starting
            try:
                self._connect()
                break
            except psycopg.OperationalError as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Can't reach Postgres at {self.describe()}: {_no_password(e, url)}") from None
                time.sleep(2)
        with self._lock:
            self._conn.execute(SCHEMA)

    def describe(self) -> str:
        u = urlsplit(self.url)
        return f"postgres {u.hostname or 'localhost'}:{u.port or 5432}{u.path or ''}"

    def _connect(self) -> None:
        self._conn = self._psycopg.connect(self.url, autocommit=True, connect_timeout=5)

    def _run(self, sql: str, params: tuple = ()):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._connect()
                    return self._conn.execute(sql, params)
                except self._psycopg.OperationalError:
                    self._conn = None  # dropped connection (database restarted): reconnect once
                    if attempt == 2:
                        raise

    def load(self, name: str, path: Path | None, default: Any) -> Any:
        row = self._run("select data from llmbot_state where name = %s", (name,)).fetchone()
        if row is not None:
            return row[0]
        if path is not None and path.exists():  # first start on Postgres: bring the old file along
            data = FileStore(path.parent).load(name, path, None)
            if data is not None:
                self.save(name, path, data)
                path.replace(path.with_name(path.name + ".imported"))
                log.info("Imported %s into Postgres (the file is now %s.imported)", path.name, path.name)
                return data
        return default

    def save(self, name: str, path: Path | None, data: Any) -> None:
        try:
            self._run("insert into llmbot_state (name, data) values (%s, %s) on conflict (name) do update "
                      "set data = excluded.data, updated_at = now()", (name, self._jsonb(data)))
        except Exception as e:  # keep running on the in-memory copy; the next save retries
            log.error("Could not save %s to Postgres: %s", name, _no_password(e, self.url))

    def delete(self, name: str, path: Path | None) -> None:
        try:
            self._run("delete from llmbot_state where name = %s", (name,))
        except Exception as e:
            log.error("Could not delete %s in Postgres: %s", name, _no_password(e, self.url))

    def record_job(self, row: dict) -> None:
        cols = [c for c in JOB_COLUMNS if c in row]
        try:
            self._run(f"insert into llmbot_jobs ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                      tuple(row[c] for c in cols))
        except Exception as e:
            log.error("Could not record job in Postgres: %s", _no_password(e, self.url))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()


def _no_password(err: Exception, url: str) -> str:
    text = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
    pw = urlsplit(url).password
    return text.replace(pw, "***") if pw else text


def open_store(database_url: str, data_dir: Path):
    if database_url:
        return PgStore(database_url)
    return FileStore(data_dir)
