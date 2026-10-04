from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


class Store:
    def __init__(self, path: str, clock: Callable[[], datetime] | None = None):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        # Serializes database access across handler threads: the service holds
        # this lock for each public operation, so a check-then-write (e.g. an
        # If-Match comparison) is atomic and concurrent updates resolve in a
        # deterministic order.
        self.lock = threading.RLock()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS resources (
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0,
              current_version INTEGER NOT NULL,
              document TEXT NOT NULL,
              last_updated TEXT NOT NULL,
              PRIMARY KEY (type, id)
            );
            CREATE TABLE IF NOT EXISTS versions (
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              version INTEGER NOT NULL,
              document TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              PRIMARY KEY (type, id, version)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY,
              criteria TEXT NOT NULL,
              reason TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
              sequence INTEGER NOT NULL,
              type TEXT NOT NULL,
              payload TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              PRIMARY KEY (subscription_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
              key TEXT PRIMARY KEY,
              operation TEXT NOT NULL,
              response TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_channels (
              subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id),
              endpoint TEXT NOT NULL,
              secret TEXT
            );
            CREATE TABLE IF NOT EXISTS deliveries (
              subscription_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              delivery_id TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              secret TEXT,
              payload TEXT NOT NULL,
              state TEXT NOT NULL,
              attempts TEXT NOT NULL,
              next_attempt_at REAL,
              PRIMARY KEY (subscription_id, sequence)
            );
            -- One row per audited HTTP request. AUTOINCREMENT keeps a
            -- high-water mark outside the row itself, so a sequence is never
            -- reused, even after a restart or a rolled-back transaction.
            CREATE TABLE IF NOT EXISTS audit (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              occurred_at TEXT NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              outcome TEXT NOT NULL,
              status INTEGER NOT NULL,
              resource_type TEXT,
              resource_id TEXT,
              version INTEGER,
              replayed INTEGER NOT NULL,
              changes TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def decode(value: str) -> Any:
        return json.loads(value)

    def now(self) -> str:
        """Current instant as an RFC 3339 UTC timestamp with millisecond precision."""
        return self.clock().astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
