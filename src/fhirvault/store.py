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
        # All state-changing operations take this lock, so concurrent writes
        # commit in request-reception order and never interleave a version
        # check with another writer's commit.
        self.write_lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
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
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Serialize writers so an If-Match version check and its write execute
        # as one atomic step relative to every other writer.
        with self.write_lock:
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
