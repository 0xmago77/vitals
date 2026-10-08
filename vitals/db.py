"""SQLite persistence (jobs, quotes, keeper actions, cursors)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    chain_status INTEGER,
    client TEXT,
    provider TEXT,
    evaluator TEXT,
    hook TEXT,
    budget TEXT,
    token TEXT,
    expired_at INTEGER,
    submitted_at INTEGER,
    description TEXT,
    task TEXT,
    deliverable_hash TEXT,
    deliverable_url TEXT,
    submit_tx TEXT,
    settle_tx TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    negotiation_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    price TEXT NOT NULL,
    task TEXT NOT NULL,
    address TEXT,
    client TEXT
);
CREATE TABLE IF NOT EXISTS keeper_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL,
    amount TEXT,
    tx_hash TEXT,
    status TEXT NOT NULL,
    hf_before TEXT,
    hf_after TEXT,
    dry_run INTEGER NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

JOB_FIELDS = ("state", "chain_status", "client", "provider", "evaluator", "hook", "budget", "token", "expired_at",
              "submitted_at", "description", "task", "deliverable_hash", "deliverable_url", "submit_tx",
              "settle_tx", "attempts", "next_attempt_at", "error")


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- kv

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))

    # -------------------------------------------------------------- jobs

    def job(self, job_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (int(job_id),)).fetchone()
        return dict(row) if row else None

    def jobs(self, states: Iterable[str] | None = None, limit: int = 500) -> list[dict]:
        with self._lock:
            if states:
                st = list(states)
                q = f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(st))}) ORDER BY job_id DESC LIMIT ?"
                rows = self._conn.execute(q, (*st, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY job_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_job(self, job_id: int, **fields: Any) -> dict:
        now = int(time.time())
        bad = set(fields) - set(JOB_FIELDS)
        if bad:
            raise KeyError(f"unknown job fields: {bad}")
        with self._lock:
            existing = self.job(job_id)
            if existing is None:
                cols = ["job_id", "created_at", "updated_at", *fields.keys()]
                vals = [int(job_id), now, now, *fields.values()]
                if "state" not in fields:
                    cols.append("state")
                    vals.append("seen")
                self._conn.execute(f"INSERT INTO jobs({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            elif fields:
                sets = ",".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE job_id=?",
                                   (*fields.values(), now, int(job_id)))
        return self.job(job_id)  # type: ignore[return-value]

    def claim_job(self, job_id: int, from_state: str, to_state: str) -> bool:
        """Compare-and-swap a job's state; only one worker can win."""
        with self._lock:
            cur = self._conn.execute("UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state=?",
                                     (to_state, int(time.time()), int(job_id), from_state))
            return cur.rowcount == 1

    def job_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ------------------------------------------------------------ quotes

    def add_quote(self, negotiation_hash: str, expires_at: int, price: str, task: str, address: str | None,
                  client: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO quotes(negotiation_hash, created_at, expires_at, price, task, address, client) "
                "VALUES(?,?,?,?,?,?,?)",
                (negotiation_hash.lower(), int(time.time()), expires_at, price, task, address, client),
            )

    def quote(self, negotiation_hash: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM quotes WHERE negotiation_hash=?",
                                     (negotiation_hash.lower(),)).fetchone()
        return dict(row) if row else None

    def quote_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])

    # ------------------------------------------------------------ keeper

    def add_keeper_action(self, action: str, *, amount: str | None, tx_hash: str | None, status: str,
                          hf_before: str | None, hf_after: str | None, dry_run: bool, note: str = "",
                          ts: int | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO keeper_actions(ts, action, amount, tx_hash, status, hf_before, hf_after, dry_run, note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts or int(time.time()), action, amount, tx_hash, status, hf_before, hf_after, int(dry_run), note),
            )
            return int(cur.lastrowid)

    def keeper_actions(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM keeper_actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_keeper_tx(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM keeper_actions WHERE tx_hash IS NOT NULL AND status='confirmed' AND dry_run=0 "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ events

    def event(self, kind: str, detail: Any = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
                               (int(time.time()), kind, json.dumps(detail, default=str)[:4000]))

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
